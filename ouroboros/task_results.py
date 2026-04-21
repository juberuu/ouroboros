"""Helpers for durable task result/status files."""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso

STATUS_REQUESTED = "requested"
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_REJECTED_DUPLICATE = "rejected_duplicate"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"


def task_results_dir(drive_root: Any) -> pathlib.Path:
    path = pathlib.Path(drive_root) / "task_results"
    path.mkdir(parents=True, exist_ok=True)
    return path


def task_result_path(drive_root: Any, task_id: str) -> pathlib.Path:
    return task_results_dir(drive_root) / f"{task_id}.json"


def load_task_result(drive_root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    path = task_result_path(drive_root, task_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_task_results(
    drive_root: Any,
    *,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    wanted = {str(item) for item in list(statuses or []) if str(item).strip()}
    results: List[Dict[str, Any]] = []
    for path in sorted(task_results_dir(drive_root).glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if wanted and str(data.get("status") or "") not in wanted:
            continue
        results.append(data)
    return results


def write_task_result(
    drive_root: Any,
    task_id: str,
    status: str,
    **fields: Any,
) -> Dict[str, Any]:
    path = task_result_path(drive_root, task_id)
    existing = load_task_result(drive_root, task_id) or {}

    ts = str(fields.pop("ts", "") or existing.get("ts") or utc_now_iso())
    payload = {
        **existing,
        **fields,
        "task_id": task_id,
        "status": status,
        "ts": ts,
    }

    tmp_path = path.parent / f"{task_id}.json.tmp"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return payload
