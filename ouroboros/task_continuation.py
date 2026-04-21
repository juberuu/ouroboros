"""Typed durable review-continuation payloads for blocked/interrupted tasks."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import utc_now_iso

_CONTINUATION_DIR_RELPATH = "state/review_continuations"
_CORRUPT_DIR_NAME = "corrupt"


class ContinuationCorruptError(ValueError):
    """Raised when a continuation file exists but is malformed/corrupt."""


@dataclass
class ReviewContinuation:
    task_id: str
    source: str
    stage: str
    repo_key: str = ""
    tool_name: str = ""
    attempt: int = 0
    commit_message: str = ""
    block_reason: str = ""
    block_details: str = ""
    critical_findings: List[Dict[str, Any]] = field(default_factory=list)
    advisory_findings: List[Dict[str, Any]] = field(default_factory=list)
    obligation_ids: List[str] = field(default_factory=list)
    open_obligations: List[Dict[str, Any]] = field(default_factory=list)
    readiness_warnings: List[str] = field(default_factory=list)
    degraded_reasons: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    task_type: str = ""
    parent_task_id: str = ""
    created_ts: str = ""
    updated_ts: str = ""


def continuation_dir(drive_root: Any) -> pathlib.Path:
    path = pathlib.Path(drive_root) / _CONTINUATION_DIR_RELPATH
    path.mkdir(parents=True, exist_ok=True)
    return path


def corrupt_continuation_dir(drive_root: Any) -> pathlib.Path:
    path = continuation_dir(drive_root) / _CORRUPT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def continuation_path(drive_root: Any, task_id: str) -> pathlib.Path:
    return continuation_dir(drive_root) / f"{task_id}.json"


def load_review_continuation(drive_root: Any, task_id: str) -> Optional[ReviewContinuation]:
    path = continuation_path(drive_root, task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ContinuationCorruptError(f"{path.name}: invalid JSON: {exc}") from exc
    return _continuation_from_dict(data, expected_task_id=task_id)


def list_review_continuations(drive_root: Any) -> Tuple[List[ReviewContinuation], List[str]]:
    continuations: List[ReviewContinuation] = []
    corrupt: List[str] = []
    for path in sorted(continuation_dir(drive_root).glob("*.json")):
        task_id = path.stem
        try:
            item = load_review_continuation(drive_root, task_id)
        except ContinuationCorruptError as exc:
            corrupt.append(str(exc))
            continue
        if item is not None:
            continuations.append(item)
    corrupt.extend(_list_quarantined_corrupt_messages(drive_root))
    return continuations, corrupt


def save_review_continuation(
    drive_root: Any,
    continuation: ReviewContinuation,
    *,
    expect_task_id: str = "",
) -> ReviewContinuation:
    task_id = str(continuation.task_id or "").strip()
    if not task_id:
        raise ValueError("ReviewContinuation.task_id is required")
    if expect_task_id and task_id != str(expect_task_id).strip():
        raise ValueError(
            f"Continuation ownership mismatch: payload task_id={task_id!r}, expected={expect_task_id!r}"
        )

    path = continuation_path(drive_root, task_id)
    existing = None
    if path.exists():
        try:
            existing = load_review_continuation(drive_root, task_id)
        except ContinuationCorruptError as exc:
            _quarantine_corrupt_continuation(drive_root, task_id, reason=str(exc))
            existing = None
    now_ts = utc_now_iso()
    if not continuation.created_ts:
        continuation.created_ts = existing.created_ts if existing else now_ts
    continuation.updated_ts = now_ts

    payload = asdict(continuation)
    tmp_path = path.parent / f"{task_id}.json.tmp"
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)
    return continuation


def clear_review_continuation(drive_root: Any, task_id: str) -> bool:
    path = continuation_path(drive_root, task_id)
    try:
        if path.exists():
            path.unlink()
            return True
    except Exception:
        return False
    return False


def build_review_continuation(
    task: Dict[str, Any],
    attempt: Any,
    open_obligations: List[Any],
    *,
    source: str,
    warning: str = "",
) -> Optional[ReviewContinuation]:
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return None

    warnings = [str(item) for item in (getattr(attempt, "readiness_warnings", []) or []) if str(item).strip()]
    if warning:
        warnings.append(str(warning))
    if attempt is None and not open_obligations:
        if source != "task_exception" or not warnings:
            return None
        return ReviewContinuation(
            task_id=task_id,
            source=source,
            stage=source,
            readiness_warnings=warnings,
            warnings=warnings,
            task_type=str(task.get("type") or ""),
            parent_task_id=str(task.get("parent_task_id") or ""),
        )

    return ReviewContinuation(
        task_id=task_id,
        source=source,
        stage=str(getattr(attempt, "phase", "") or source),
        repo_key=str(getattr(attempt, "repo_key", "") or ""),
        tool_name=str(getattr(attempt, "tool_name", "") or ""),
        attempt=int(getattr(attempt, "attempt", 0) or 0),
        commit_message=str(getattr(attempt, "commit_message", "") or ""),
        block_reason=str(getattr(attempt, "block_reason", "") or ""),
        block_details=str(getattr(attempt, "block_details", "") or ""),
        critical_findings=list(getattr(attempt, "critical_findings", []) or []),
        advisory_findings=list(getattr(attempt, "advisory_findings", []) or []),
        obligation_ids=list(getattr(attempt, "obligation_ids", []) or []),
        open_obligations=[_obligation_to_dict(item) for item in open_obligations],
        readiness_warnings=warnings,
        degraded_reasons=list(getattr(attempt, "degraded_reasons", []) or []),
        warnings=[str(item) for item in warnings if str(item).strip()],
        task_type=str(task.get("type") or ""),
        parent_task_id=str(task.get("parent_task_id") or ""),
    )


def capture_review_continuation_from_state(
    drive_root: Any,
    task: Dict[str, Any],
    *,
    source: str,
    warning: str = "",
    repo_dir: Any = None,
) -> Optional[ReviewContinuation]:
    from ouroboros.review_state import load_state, make_repo_key

    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return None

    state = load_state(pathlib.Path(drive_root))
    repo_key = make_repo_key(pathlib.Path(repo_dir)) if repo_dir else ""
    attempt = (
        state.latest_attempt_for(task_id=task_id, repo_key=repo_key or None)
        if repo_key
        else state.latest_attempt_for(task_id=task_id)
    )
    obligation_repo_key = str(getattr(attempt, "repo_key", "") or repo_key or "")
    continuation = build_review_continuation(
        task,
        attempt,
        state.get_open_obligations(repo_key=obligation_repo_key or None),
        source=source,
        warning=warning,
    )
    if continuation is None:
        return None
    return save_review_continuation(drive_root, continuation, expect_task_id=task_id)


def _continuation_from_dict(data: Dict[str, Any], *, expected_task_id: str = "") -> ReviewContinuation:
    if not isinstance(data, dict):
        raise ContinuationCorruptError("Continuation payload must be a JSON object")
    task_id = str(data.get("task_id", "") or "").strip()
    if not task_id:
        raise ContinuationCorruptError("Continuation missing task_id")
    if expected_task_id and task_id != expected_task_id:
        raise ContinuationCorruptError(
            f"Continuation task_id mismatch: {task_id!r} != {expected_task_id!r}"
        )
    source = str(data.get("source", "") or "").strip()
    stage = str(data.get("stage", "") or "").strip()
    if not source or not stage:
        raise ContinuationCorruptError("Continuation missing source/stage")

    return ReviewContinuation(
        task_id=task_id,
        source=source,
        stage=stage,
        repo_key=str(data.get("repo_key", "") or ""),
        tool_name=str(data.get("tool_name", "") or ""),
        attempt=int(data.get("attempt", 0) or 0),
        commit_message=str(data.get("commit_message", "") or ""),
        block_reason=str(data.get("block_reason", "") or ""),
        block_details=str(data.get("block_details", "") or ""),
        critical_findings=list(data.get("critical_findings") or []),
        advisory_findings=list(data.get("advisory_findings") or []),
        obligation_ids=[str(x) for x in (data.get("obligation_ids") or [])],
        open_obligations=[
            item if isinstance(item, dict) else {"value": str(item)}
            for item in (data.get("open_obligations") or [])
        ],
        readiness_warnings=[str(x) for x in (data.get("readiness_warnings") or [])],
        degraded_reasons=[str(x) for x in (data.get("degraded_reasons") or [])],
        warnings=[str(x) for x in (data.get("warnings") or [])],
        task_type=str(data.get("task_type", "") or ""),
        parent_task_id=str(data.get("parent_task_id", "") or ""),
        created_ts=str(data.get("created_ts", "") or ""),
        updated_ts=str(data.get("updated_ts", "") or ""),
    )


def _obligation_to_dict(item: Any) -> Dict[str, Any]:
    if isinstance(item, dict):
        return item
    return {
        "obligation_id": str(getattr(item, "obligation_id", "") or ""),
        "fingerprint": str(getattr(item, "fingerprint", "") or ""),
        "item": str(getattr(item, "item", "") or ""),
        "severity": str(getattr(item, "severity", "") or ""),
        "reason": str(getattr(item, "reason", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "source_attempt_ts": str(getattr(item, "source_attempt_ts", "") or ""),
        "source_attempt_msg": str(getattr(item, "source_attempt_msg", "") or ""),
        "created_ts": str(getattr(item, "created_ts", "") or ""),
        "updated_ts": str(getattr(item, "updated_ts", "") or ""),
    }


def _quarantine_corrupt_continuation(drive_root: Any, task_id: str, *, reason: str) -> None:
    src = continuation_path(drive_root, task_id)
    if not src.exists():
        return
    stamp = _safe_ts_token(utc_now_iso())
    corrupt_dir = corrupt_continuation_dir(drive_root)
    archived = corrupt_dir / f"{task_id}.{stamp}.json"
    note = archived.with_suffix(".txt")
    os.replace(src, archived)
    note.write_text(reason.strip() or "corrupt continuation quarantined", encoding="utf-8")


def _list_quarantined_corrupt_messages(drive_root: Any) -> List[str]:
    corrupt_dir = continuation_dir(drive_root) / _CORRUPT_DIR_NAME
    if not corrupt_dir.exists():
        return []
    messages: List[str] = []
    for path in sorted(corrupt_dir.glob("*.json")):
        note = path.with_suffix(".txt")
        reason = ""
        if note.exists():
            try:
                reason = note.read_text(encoding="utf-8").strip()
            except Exception:
                reason = ""
        msg = f"{path.name}: quarantined corrupt continuation"
        if reason:
            msg = f"{msg}: {reason}"
        messages.append(msg)
    return messages


def _safe_ts_token(ts: str) -> str:
    token = str(ts or "").strip()
    if not token:
        return "unknown"
    return token.replace(":", "").replace("-", "").replace("+", "_")
