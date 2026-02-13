"""
Уроборос — Deep Review.

Полный review кода, промптов, состояния, логов.
Контракт: run_review(task) -> (text, usage, trace).
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict, List, Tuple

from ouroboros.llm import LLMClient
from ouroboros.utils import (
    utc_now_iso, append_jsonl, truncate_for_log, clip_text, estimate_tokens,
)


_SKIP_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip",
    ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar", ".mp3", ".mp4", ".mov",
    ".avi", ".wav", ".ogg", ".opus", ".woff", ".woff2", ".ttf", ".otf",
    ".class", ".so", ".dylib", ".bin",
}


class ReviewEngine:
    """Deep review система.

    Собирает текстовые файлы из repo и drive, разбивает на чанки,
    делает multi-pass review через LLM, синтезирует результат.
    """

    def __init__(self, llm: LLMClient, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self.llm = llm
        self.repo_dir = repo_dir
        self.drive_root = drive_root

    def collect_sections(self) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
        max_file_chars = 600_000
        max_total_chars = 8_000_000
        sections: List[Tuple[str, str]] = []
        total_chars = 0
        truncated = 0
        dropped = 0

        def _walk(root: pathlib.Path, prefix: str, skip_dirs: set) -> None:
            nonlocal total_chars, truncated, dropped
            for dirpath, dirnames, filenames in os.walk(str(root)):
                dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
                for fn in sorted(filenames):
                    p = pathlib.Path(dirpath) / fn
                    if not p.is_file() or p.is_symlink():
                        continue
                    if p.suffix.lower() in _SKIP_EXT:
                        continue
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if not content.strip():
                        continue
                    rel = p.relative_to(root).as_posix()
                    if len(content) > max_file_chars:
                        content = clip_text(content, max_file_chars)
                        truncated += 1
                    if total_chars >= max_total_chars:
                        dropped += 1
                        continue
                    if (total_chars + len(content)) > max_total_chars:
                        content = clip_text(content, max(2000, max_total_chars - total_chars))
                        truncated += 1
                    sections.append((f"{prefix}/{rel}", content))
                    total_chars += len(content)

        _walk(self.repo_dir, "repo",
              {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"})
        _walk(self.drive_root, "drive", {"archive", "locks"})

        stats = {"files": len(sections), "chars": total_chars,
                 "truncated": truncated, "dropped": dropped}
        return sections, stats

    def chunk_sections(self, sections: List[Tuple[str, str]], chunk_token_cap: int = 140_000) -> List[str]:
        cap = max(20_000, min(chunk_token_cap, 220_000))
        cap_chars = cap * 4
        chunks: List[str] = []
        current_parts: List[str] = []
        current_tokens = 0

        for path, content in sections:
            if not content:
                continue
            header = f"\n## FILE: {path}\n"
            part = header + content + "\n"
            part_tokens = estimate_tokens(part)
            if current_parts and (current_tokens + part_tokens) > cap:
                chunks.append("\n".join(current_parts))
                current_parts = []
                current_tokens = 0
            current_parts.append(part)
            current_tokens += part_tokens

        if current_parts:
            chunks.append("\n".join(current_parts))
        return chunks or ["(No reviewable content found.)"]

    def run_review(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """Полный deep review. Возвращает: (report_text, usage_total, llm_trace)."""
        reason = str(task.get("text") or "manual_review")
        profile = self.llm.model_profile("deep_review")
        model = profile["model"]
        effort = profile["effort"]

        sections, stats = self.collect_sections()
        chunks = self.chunk_sections(sections)
        total_tokens_est = sum(estimate_tokens(c) for c in chunks)

        append_jsonl(
            self.drive_root / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(), "type": "review_started",
                "task_id": task.get("id"), "tokens_est": total_tokens_est,
                "chunks": len(chunks), "files": stats["files"],
                "model": model, "effort": effort,
            },
        )

        chunk_system = (
            "You are principal reliability reviewer for a self-modifying agent. "
            "Analyze the provided snapshot and return concise actionable findings. "
            "Focus on bugs, deadlocks, drift, and improvement opportunities."
        )

        usage_total: Dict[str, Any] = {}
        chunk_reports: List[str] = []
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}

        for idx, chunk_text in enumerate(chunks, start=1):
            user_prompt = (
                f"Review reason: {truncate_for_log(reason, 300)}\n"
                f"Chunk {idx}/{len(chunks)}\n\n"
                "Return: 1) Critical risks 2) High-impact improvements "
                "3) Evidence references 4) Suggested next actions\n\n"
                + chunk_text
            )
            try:
                msg, usage = self.llm.chat(
                    [{"role": "system", "content": chunk_system},
                     {"role": "user", "content": user_prompt}],
                    model=model, reasoning_effort=effort, max_tokens=3800,
                )
                text = msg.get("content", "") or ""
                chunk_reports.append(f"=== Chunk {idx}/{len(chunks)} ===\n{text}")
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage_total[k] = int(usage_total.get(k) or 0) + int(usage.get(k) or 0)
                if usage.get("cost"):
                    usage_total["cost"] = float(usage_total.get("cost") or 0) + float(usage["cost"])
            except Exception as e:
                chunk_reports.append(f"=== Chunk {idx} ERROR: {e} ===")

        if len(chunk_reports) > 1:
            synthesis_prompt = (
                "Consolidate these multi-chunk review results into a single coherent report.\n"
                "Sections: 1) Critical risks 2) Key improvements 3) Action plan\n\n"
                + "\n\n".join(chunk_reports)
            )
            try:
                msg, usage = self.llm.chat(
                    [{"role": "system", "content": "Consolidate review findings."},
                     {"role": "user", "content": synthesis_prompt}],
                    model=model, reasoning_effort=effort, max_tokens=4000,
                )
                final_report = msg.get("content", "") or ""
                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    usage_total[k] = int(usage_total.get(k) or 0) + int(usage.get(k) or 0)
                if usage.get("cost"):
                    usage_total["cost"] = float(usage_total.get("cost") or 0) + float(usage["cost"])
            except Exception as e:
                final_report = f"Synthesis failed: {e}\n\n" + "\n\n".join(chunk_reports)
        else:
            final_report = chunk_reports[0] if chunk_reports else "(empty review)"

        cost = usage_total.get("cost", 0)
        final_report += f"\n\n---\nReview cost: ~${cost:.4f}, tokens: {usage_total.get('total_tokens', 0)}"

        append_jsonl(
            self.drive_root / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(), "type": "review_completed",
                "task_id": task.get("id"), "chunks": len(chunks), "usage": usage_total,
            },
        )
        return final_report, usage_total, llm_trace
