"""Append-only checkpoints for evolution progress and later eval curves."""

from __future__ import annotations

import hashlib
import pathlib
import subprocess
from typing import Any, Dict

from ouroboros.utils import append_jsonl, utc_now_iso


CHECKPOINTS_REL = pathlib.Path("state") / "evolution_checkpoints.jsonl"


def _sha_file(path: pathlib.Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def _git_value(repo_dir: pathlib.Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True, text=True, timeout=5)
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def append_evolution_checkpoint(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    task_id: str,
    campaign: Dict[str, Any] | None = None,
    result_status: str = "",
    cost_usd: float = 0.0,
    rounds: int = 0,
) -> None:
    """Persist a lightweight checkpoint after an evolution cycle."""
    memory = pathlib.Path(drive_root) / "memory"
    entry = {
        "schema_version": 1,
        "ts": utc_now_iso(),
        "task_id": str(task_id or ""),
        "campaign_id": str((campaign or {}).get("id") or ""),
        "campaign_objective": str((campaign or {}).get("objective") or ""),
        "git_sha": _git_value(pathlib.Path(repo_dir), ["rev-parse", "HEAD"]),
        "git_branch": _git_value(pathlib.Path(repo_dir), ["rev-parse", "--abbrev-ref", "HEAD"]),
        "identity_sha256": _sha_file(memory / "identity.md"),
        "scratchpad_sha256": _sha_file(memory / "scratchpad.md"),
        "knowledge_index_sha256": _sha_file(memory / "knowledge" / "index-full.md"),
        "result_status": str(result_status or ""),
        "cost_usd": float(cost_usd or 0.0),
        "rounds": int(rounds or 0),
    }
    append_jsonl(pathlib.Path(drive_root) / CHECKPOINTS_REL, entry)
