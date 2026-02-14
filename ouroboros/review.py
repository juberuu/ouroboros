"""
Уроборос — Deep Review (стратегическая рефлексия).

Собирает код и состояние, вычисляет метрики сложности,
отправляет чанки на стратегический анализ LLM, синтезирует отчёт.

Контракт: run_review(task) -> (report_text, usage_total, llm_trace).
"""

from __future__ import annotations

import os
import pathlib
import re
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


# ---------------------------------------------------------------------------
# Complexity metrics
# ---------------------------------------------------------------------------

def compute_complexity_metrics(sections: List[Tuple[str, str]]) -> Dict[str, Any]:
    """Compute codebase complexity metrics from collected sections."""
    total_lines = 0
    total_functions = 0
    function_lengths: List[int] = []
    total_files = len(sections)
    py_files = 0

    for path, content in sections:
        lines = content.splitlines()
        total_lines += len(lines)

        if not path.endswith(".py"):
            continue
        py_files += 1

        # Count functions/methods and their lengths
        func_starts: List[int] = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                func_starts.append(i)
                total_functions += 1

        # Compute function lengths (lines between consecutive def's)
        for j, start in enumerate(func_starts):
            end = func_starts[j + 1] if j + 1 < len(func_starts) else len(lines)
            function_lengths.append(end - start)

    avg_func_len = round(sum(function_lengths) / max(1, len(function_lengths)), 1)
    max_func_len = max(function_lengths) if function_lengths else 0

    return {
        "total_files": total_files,
        "py_files": py_files,
        "total_lines": total_lines,
        "total_functions": total_functions,
        "avg_function_length": avg_func_len,
        "max_function_length": max_func_len,
    }


def format_metrics(metrics: Dict[str, Any]) -> str:
    """Format metrics as a readable string for the report."""
    return (
        f"Complexity metrics:\n"
        f"  Files: {metrics['total_files']} (Python: {metrics['py_files']})\n"
        f"  Lines of code: {metrics['total_lines']}\n"
        f"  Functions/methods: {metrics['total_functions']}\n"
        f"  Avg function length: {metrics['avg_function_length']} lines\n"
        f"  Max function length: {metrics['max_function_length']} lines"
    )


# ---------------------------------------------------------------------------
# File collection
# ---------------------------------------------------------------------------

def collect_sections(
    repo_dir: pathlib.Path,
    drive_root: pathlib.Path,
    max_file_chars: int = 300_000,
    max_total_chars: int = 4_000_000,
) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
    """Walk repo and drive, collect text files as (path, content) pairs."""
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

    _walk(repo_dir, "repo",
          {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"})
    _walk(drive_root, "drive", {"archive", "locks", "downloads", "screenshots"})

    stats = {"files": len(sections), "chars": total_chars,
             "truncated": truncated, "dropped": dropped}
    return sections, stats


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def chunk_sections(sections: List[Tuple[str, str]], chunk_token_cap: int = 70_000) -> List[str]:
    """Split sections into chunks that fit within token budget."""
    cap = max(20_000, min(chunk_token_cap, 120_000))
    chunks: List[str] = []
    current_parts: List[str] = []
    current_tokens = 0

    for path, content in sections:
        if not content:
            continue
        part = f"\n## FILE: {path}\n{content}\n"
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


# ---------------------------------------------------------------------------
# Review engine
# ---------------------------------------------------------------------------

# Prompts
CHUNK_SYSTEM_PROMPT = (
    "You are a strategic reviewer for Ouroboros, a self-modifying AI agent. "
    "Your job is NOT to find bugs — it is to assess the health and direction of the system.\n\n"
    "Analyze the provided code snapshot and assess:\n"
    "1) **Architecture quality**: Is the code clean, modular, minimal? Any God Methods or bloat?\n"
    "2) **Bible compliance**: Does it follow Ouroboros philosophy (LLM-first, minimalism, boldness)?\n"
    "3) **Evolution direction**: Are changes bold and impactful, or timid micro-fixes?\n"
    "4) **Highest-leverage next move**: What single change would have maximum impact?\n\n"
    "You MUST return a substantive response. Even if you see no issues, explain WHY the code is good.\n"
    "Be concise but thorough. Focus on strategy, not nitpicks."
)

SYNTHESIS_SYSTEM_PROMPT = (
    "You are consolidating a multi-chunk strategic review of Ouroboros, a self-modifying AI agent.\n\n"
    "Produce a single coherent report with these sections:\n"
    "1) **Architecture Assessment** — is the codebase getting simpler or more complex?\n"
    "2) **Bible Compliance** — where does the code violate its own philosophy?\n"
    "3) **Evolution Direction** — bold or timid? What's the trend?\n"
    "4) **Top 3 Highest-Leverage Moves** — what should be done next, ranked by impact.\n"
    "5) **Risks** — what could go wrong if current direction continues?\n\n"
    "Be direct and actionable. No fluff."
)


class ReviewEngine:
    """Deep review — стратегическая рефлексия.

    Собирает код, вычисляет метрики, делает multi-pass review, синтезирует.
    """

    def __init__(self, llm: LLMClient, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self.llm = llm
        self.repo_dir = repo_dir
        self.drive_root = drive_root

    def run_review(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        """Full strategic review. Returns: (report_text, usage_total, llm_trace)."""
        reason = str(task.get("text") or "manual_review")
        profile = self.llm.model_profile("deep_review")
        model = profile["model"]
        effort = profile["effort"]

        # Collect files
        sections, stats = collect_sections(self.repo_dir, self.drive_root)
        metrics = compute_complexity_metrics(sections)
        chunks = chunk_sections(sections)
        total_tokens_est = sum(estimate_tokens(c) for c in chunks)

        append_jsonl(
            self.drive_root / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(), "type": "review_started",
                "task_id": task.get("id"), "tokens_est": total_tokens_est,
                "chunks": len(chunks), "files": stats["files"],
                "model": model, "effort": effort,
                "metrics": metrics,
            },
        )

        # Process chunks
        usage_total: Dict[str, Any] = {}
        chunk_reports: List[str] = []
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
        empty_chunks = 0

        for idx, chunk_text in enumerate(chunks, start=1):
            user_prompt = (
                f"Review reason: {truncate_for_log(reason, 300)}\n"
                f"Chunk {idx}/{len(chunks)}\n"
                f"{format_metrics(metrics)}\n\n"
                "Analyze the code below and provide your strategic assessment.\n\n"
                + chunk_text
            )
            try:
                msg, usage = self.llm.chat(
                    [{"role": "system", "content": CHUNK_SYSTEM_PROMPT},
                     {"role": "user", "content": user_prompt}],
                    model=model, reasoning_effort=effort, max_tokens=4000,
                )
                text = (msg.get("content") or "").strip()
                if text:
                    chunk_reports.append(f"=== Chunk {idx}/{len(chunks)} ===\n{text}")
                else:
                    empty_chunks += 1
                    append_jsonl(
                        self.drive_root / "logs" / "events.jsonl",
                        {"ts": utc_now_iso(), "type": "review_chunk_empty",
                         "task_id": task.get("id"), "chunk": idx, "chunks": len(chunks)},
                    )
                self._add_usage(usage_total, usage)
            except Exception as e:
                chunk_reports.append(f"=== Chunk {idx} ERROR: {e} ===")

        # Synthesize
        if len(chunk_reports) > 1:
            synthesis_prompt = (
                f"{format_metrics(metrics)}\n\n"
                "Consolidate the chunk reviews below into one strategic report.\n\n"
                + "\n\n".join(chunk_reports)
            )
            try:
                msg, usage = self.llm.chat(
                    [{"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
                     {"role": "user", "content": synthesis_prompt}],
                    model=model, reasoning_effort=effort, max_tokens=4000,
                )
                final_report = (msg.get("content") or "").strip()
                if not final_report:
                    final_report = "Synthesis returned empty. Raw chunk reports:\n\n" + "\n\n".join(chunk_reports)
                self._add_usage(usage_total, usage)
            except Exception as e:
                final_report = f"Synthesis failed: {e}\n\n" + "\n\n".join(chunk_reports)
        elif chunk_reports:
            final_report = chunk_reports[0]
        else:
            final_report = "(empty review — no chunks produced output)"

        # Prepend metrics
        final_report = (
            f"{format_metrics(metrics)}\n"
            f"Review coverage: {stats['files']} files, {stats['chars']} chars, "
            f"{len(chunks)} chunks, {empty_chunks} empty\n\n"
            + final_report
        )

        cost = usage_total.get("cost", 0)
        final_report += f"\n\n---\nReview cost: ~${cost:.4f}, tokens: {usage_total.get('total_tokens', 0)}"

        append_jsonl(
            self.drive_root / "logs" / "events.jsonl",
            {
                "ts": utc_now_iso(), "type": "review_completed",
                "task_id": task.get("id"), "chunks": len(chunks),
                "empty_chunks": empty_chunks, "usage": usage_total,
                "metrics": metrics,
            },
        )
        return final_report, usage_total, llm_trace

    @staticmethod
    def _add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
        if usage.get("cost"):
            total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])
