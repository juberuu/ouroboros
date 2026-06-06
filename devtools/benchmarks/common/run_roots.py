"""Run-root helpers for benchmark devtools."""

from __future__ import annotations

import os
import pathlib
import re
import time


DEFAULT_BENCH_RUNS_ROOT = pathlib.Path("/Users/anton/Ouroboros/bench_runs")
_SAFE_BENCHMARK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def timestamp_run_id(prefix: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in prefix).strip("-_")
    return f"{safe or 'run'}_{time.strftime('%Y%m%d_%H%M%S')}"


def run_root(benchmark: str, run_id: str = "") -> pathlib.Path:
    root = pathlib.Path(os.environ.get("OUROBOROS_BENCH_RUNS_ROOT") or DEFAULT_BENCH_RUNS_ROOT)
    bench = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in benchmark).strip("-_")
    rid = run_id or timestamp_run_id(bench)
    return (root / bench / rid).resolve(strict=False)


def ensure_outside_repo(path: pathlib.Path, repo_dir: pathlib.Path) -> pathlib.Path:
    resolved = pathlib.Path(path).expanduser().resolve(strict=False)
    repo = pathlib.Path(repo_dir).resolve(strict=False)
    try:
        resolved.relative_to(repo)
    except ValueError:
        resolved.mkdir(parents=True, exist_ok=True)
        return resolved
    raise ValueError(f"benchmark run output must not be under repo/: {resolved}")


def safe_benchmark_id(value: str, *, field: str = "instance_id") -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or pathlib.PurePath(text).is_absolute()
        or not _SAFE_BENCHMARK_ID_RE.fullmatch(text)
    ):
        raise ValueError(f"{field} must be a single safe path component")
    return text


def safe_join_under(root: pathlib.Path, *parts: str) -> pathlib.Path:
    base = pathlib.Path(root).expanduser().resolve(strict=False)
    resolved = base.joinpath(*[str(part or "") for part in parts]).resolve(strict=False)
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"benchmark output path escapes run root: {resolved}") from exc
    return resolved


def ensure_file_output_outside_repo(path: pathlib.Path, repo_dir: pathlib.Path) -> pathlib.Path:
    resolved = pathlib.Path(path).expanduser().resolve(strict=False)
    ensure_outside_repo(resolved.parent, repo_dir)
    return resolved


def repo_root_from_devtools() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parents[3]
