"""Benchmark manifest helpers.

These helpers intentionally record reproducibility metadata without storing
secret values or full repository diffs. Official benchmark harnesses remain the
scoring authority; these manifests make Ouroboros-side task coverage and run
provenance auditable.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any


_SUBPROCESS_RUN = subprocess.run

MODEL_SLOT_KEYS = (
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_HEAVY",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_MODEL_VISION",
    "OUROBOROS_MODEL_CONSCIOUSNESS",
    "OUROBOROS_MODEL_FALLBACKS",
    "OUROBOROS_MODEL_DEEP_SELF_REVIEW",
    "CLAUDE_CODE_MODEL",
    "OUROBOROS_WEBSEARCH_MODEL",
    "OUROBOROS_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODEL",
    "OUROBOROS_EFFORT_TASK",
    "OUROBOROS_EFFORT_REVIEW",
    "OUROBOROS_EFFORT_SCOPE_REVIEW",
)


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        loaded = json.loads(line)
        if isinstance(loaded, dict):
            rows.append(loaded)
    return rows


def write_jsonl(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )


def _run(args: list[str], cwd: pathlib.Path, *, timeout: int = 10) -> subprocess.CompletedProcess[str]:
    def _text(value: str | bytes | None, fallback: str = "") -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            return value
        return fallback

    try:
        return _SUBPROCESS_RUN(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(args, 124, stdout=_text(exc.stdout), stderr=_text(exc.stderr, "timeout"))
    except Exception as exc:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=f"{type(exc).__name__}: {exc}")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def repo_provenance(repo_dir: pathlib.Path) -> dict[str, Any]:
    """Return non-secret source provenance for a repo checkout."""
    repo = pathlib.Path(repo_dir).expanduser().resolve(strict=False)
    result: dict[str, Any] = {
        "repo_dir": str(repo),
        "python": sys.version.split()[0],
        "schema": "ouroboros.benchmark.repo_provenance.v1",
    }
    version_path = repo / "VERSION"
    if version_path.exists():
        result["version"] = version_path.read_text(encoding="utf-8").strip()

    head = _run(["git", "rev-parse", "HEAD"], repo)
    if head.returncode != 0:
        result["git_available"] = False
        result["git_error"] = (head.stderr or head.stdout or "").strip()
        return result

    result["git_available"] = True
    result["head"] = head.stdout.strip()
    branch = _run(["git", "branch", "--show-current"], repo)
    result["branch"] = branch.stdout.strip() if branch.returncode == 0 else ""
    describe = _run(["git", "describe", "--tags", "--dirty", "--always"], repo)
    result["describe"] = describe.stdout.strip() if describe.returncode == 0 else ""
    status = _run(["git", "status", "--porcelain=v1", "--untracked-files=all"], repo)
    status_text = status.stdout if status.returncode == 0 else ""
    tracked_diff = _run(["git", "diff", "--binary", "HEAD", "--"], repo, timeout=30)
    tracked_diff_text = tracked_diff.stdout if tracked_diff.returncode == 0 else ""
    result.update(
        {
            "dirty": bool(status_text.strip()),
            "status_entries": len([line for line in status_text.splitlines() if line.strip()]),
            "status_sha256": _sha256_text(status_text) if status_text.strip() else "",
            "tracked_diff_sha256": _sha256_text(tracked_diff_text) if tracked_diff_text.strip() else "",
        }
    )
    return result


def model_slot_snapshot(settings_path: pathlib.Path | None = None) -> dict[str, str]:
    """Return configured model/review slots without exposing provider secrets."""
    settings: dict[str, Any] = {}
    if settings_path and settings_path.exists():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                settings = loaded
        except Exception:
            settings = {}
    slots: dict[str, str] = {}
    for key in MODEL_SLOT_KEYS:
        value = os.environ.get(key)
        if value is None:
            value = settings.get(key)
        if value not in (None, ""):
            slots[key] = str(value)
    return slots


def benchmark_run_manifest(
    *,
    benchmark: str,
    run_root: pathlib.Path,
    repo_dir: pathlib.Path,
    requested_task_ids: list[str],
    metadata: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    meta = dict(metadata or {})
    for key, value in overrides.items():
        if value is not None:
            meta[key] = value
    meta_settings_path = meta.get("settings_path")
    if meta_settings_path is not None:
        meta_settings_path = pathlib.Path(meta_settings_path)
    return {
        "schema": "ouroboros.benchmark.run_manifest.v1",
        "benchmark": benchmark,
        "created_at_unix": time.time(),
        "run_root": str(pathlib.Path(run_root).expanduser().resolve(strict=False)),
        "requested_task_ids": [str(task_id) for task_id in requested_task_ids],
        "requested_count": len(requested_task_ids),
        "argv": list(meta["argv"]) if "argv" in meta else list(sys.argv),
        "dataset": str(meta.get("dataset") or ""),
        "harness": meta.get("harness") or {},
        "official_command": meta.get("official_command") or [],
        "timeout_sec": meta.get("timeout_sec"),
        "isolated_data_root": str(meta.get("isolated_data_root") or ""),
        "output_paths": meta.get("output_paths") or {},
        "model_slots": model_slot_snapshot(meta_settings_path),
        "source": repo_provenance(repo_dir),
        "extra": meta.get("extra") or {},
    }
