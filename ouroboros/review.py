"""
Уроборос — Review utilities.

Утилиты для сбора кода, вычисления метрик сложности.
Review-задачи проходят через стандартный tool loop агента (LLM-first).
"""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict, List, Tuple

from ouroboros.utils import clip_text, estimate_tokens


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
    function_lengths: List[Tuple[str, int, int]] = []  # (path, start_line, length)
    file_sizes: List[Tuple[str, int]] = []  # (path, lines)
    total_files = len(sections)
    py_files = 0

    for path, content in sections:
        lines = content.splitlines()
        line_count = len(lines)
        total_lines += line_count
        file_sizes.append((path, line_count))

        if not path.endswith(".py"):
            continue
        py_files += 1

        func_starts: List[int] = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("def ") or stripped.startswith("async def "):
                func_starts.append(i)
                total_functions += 1

        for j, start in enumerate(func_starts):
            end = func_starts[j + 1] if j + 1 < len(func_starts) else len(lines)
            length = end - start
            function_lengths.append((path, start, length))

    # Compute aggregates
    func_lens = [length for _, _, length in function_lengths]
    avg_func_len = round(sum(func_lens) / max(1, len(func_lens)), 1) if func_lens else 0
    max_func_len = max(func_lens) if func_lens else 0

    # Sort for reporting
    largest_files = sorted(file_sizes, key=lambda x: x[1], reverse=True)[:10]
    longest_functions = sorted(function_lengths, key=lambda x: x[2], reverse=True)[:10]
    oversized_functions = [(p, start, length) for p, start, length in function_lengths if length > 150]
    oversized_modules = [(p, lines) for p, lines in file_sizes if p.endswith(".py") and lines > 1000]

    return {
        "total_files": total_files,
        "py_files": py_files,
        "total_lines": total_lines,
        "total_functions": total_functions,
        "avg_function_length": avg_func_len,
        "max_function_length": max_func_len,
        "largest_files": largest_files,
        "longest_functions": longest_functions,
        "oversized_functions": oversized_functions,
        "oversized_modules": oversized_modules,
    }


def format_metrics(metrics: Dict[str, Any]) -> str:
    """Format metrics as a readable string."""
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
        try:
            root_resolved = root.resolve()
            if not root_resolved.exists():
                return
        except Exception:
            return

        for dirpath, dirnames, filenames in os.walk(str(root_resolved)):
            dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
            for fn in sorted(filenames):
                try:
                    p = pathlib.Path(dirpath) / fn
                    if not p.is_file() or p.is_symlink():
                        continue
                    if p.suffix.lower() in _SKIP_EXT:
                        continue
                    content = p.read_text(encoding="utf-8", errors="replace")
                    if not content.strip():
                        continue
                    rel = p.relative_to(root_resolved).as_posix()
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
                except Exception:
                    continue

    _walk(repo_dir, "repo",
          {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", "node_modules", ".venv"})
    _walk(drive_root, "drive", {"archive", "locks", "downloads", "screenshots"})

    stats = {"files": len(sections), "chars": total_chars,
             "truncated": truncated, "dropped": dropped}
    return sections, stats


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
