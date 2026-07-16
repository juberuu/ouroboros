"""Read/write helpers for the existing append-only Skill Review history."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, List, Optional

from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.tools.review_helpers import format_obligation_excerpt
from ouroboros.utils import append_jsonl, iter_jsonl_objects, jsonl_append_lock_path, utc_now_iso

log = logging.getLogger(__name__)


def review_history_path(drive_root: pathlib.Path, skill_name: str) -> pathlib.Path:
    return drive_root / "state" / "skills" / skill_name / "review_history.jsonl"


def finding_signature(findings: List[Dict[str, Any]]) -> List[str]:
    return sorted({
        f"{finding.get('item')}:{finding.get('verdict')}:{finding.get('severity')}"
        for finding in findings
        if isinstance(finding, dict) and str(finding.get("verdict") or "").upper() == "FAIL"
    })


def extract_fail_findings(findings: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for finding in findings:
        if not isinstance(finding, dict) or str(finding.get("verdict") or "").upper() != "FAIL":
            continue
        entry = {
            "item": str(finding.get("item") or "?"),
            "severity": str(finding.get("severity") or ""),
            "reason_excerpt": format_obligation_excerpt(str(finding.get("reason") or "")),
        }
        if finding.get("model"):
            entry["model"] = str(finding["model"])
        out.append(entry)
    return out


def _ordinal(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def normalize_history(entries: List[Dict[str, Any]], skill_name: str) -> List[Dict[str, Any]]:
    """Add read-time ordinals to legacy rows without rewriting the audit log."""
    group_rounds: Dict[str, int] = {}
    snapshot_attempts: Dict[tuple[str, str], int] = {}
    last_hash: Dict[str, str] = {}
    out: List[Dict[str, Any]] = []
    for source in entries:
        entry = dict(source)
        group_id = str(entry.get("group_id") or f"manual:{skill_name}")
        content_hash = str(entry.get("content_hash") or "")
        review_round = max(
            group_rounds.get(group_id, 0) + 1,
            _ordinal(entry.get("review_round")),
        )
        group_rounds[group_id] = review_round
        attempt_key = (group_id, content_hash)
        snapshot_attempt = max(
            snapshot_attempts.get(attempt_key, 0) + 1,
            _ordinal(entry.get("snapshot_attempt")),
        )
        snapshot_attempts[attempt_key] = snapshot_attempt
        revised = bool(last_hash.get(group_id) and last_hash[group_id] != content_hash)
        if content_hash:
            last_hash[group_id] = content_hash
        entry.update(
            group_id=group_id,
            review_round=review_round,
            snapshot_attempt=snapshot_attempt,
            snapshot_revised=bool(entry.get("snapshot_revised", revised)),
        )
        out.append(entry)
    return out


def load_history(
    drive_root: pathlib.Path,
    skill_name: str,
    limit: int = 3,
    *,
    group_id: str = "",
) -> List[Dict[str, Any]]:
    try:
        entries = normalize_history(
            list(iter_jsonl_objects(review_history_path(drive_root, skill_name))),
            skill_name,
        )
    except OSError:
        return []
    if group_id:
        entries = [entry for entry in entries if entry.get("group_id") == group_id]
    return entries[-limit:] if limit > 0 else entries


def allocate_ordinals(
    drive_root: pathlib.Path,
    skill_name: str,
    group_id: str,
    content_hash: str,
) -> tuple[int, int, bool]:
    history = load_history(drive_root, skill_name, limit=0, group_id=group_id)
    review_round = max(
        (_ordinal(row.get("review_round")) for row in history), default=0,
    ) + 1
    snapshot_attempt = max(
        (
            _ordinal(row.get("snapshot_attempt"))
            for row in history
            if str(row.get("content_hash") or "") == content_hash
        ),
        default=0,
    ) + 1
    previous_hash = str(history[-1].get("content_hash") or "") if history else ""
    return review_round, snapshot_attempt, bool(previous_hash and previous_hash != content_hash)


def count_attempts(
    drive_root: pathlib.Path,
    skill_name: str,
    content_hash: str,
    *,
    group_id: str = "",
) -> int:
    history = load_history(drive_root, skill_name, limit=0, group_id=group_id)
    return sum(1 for row in history if str(row.get("content_hash") or "") == content_hash)


def append_history(
    drive_root: pathlib.Path,
    skill_name: str,
    *,
    status: str,
    content_hash: str,
    findings: List[Dict[str, Any]],
    raw_actor_records: Optional[List[Dict[str, Any]]] = None,
    single_reviewer_no_diversity: bool = False,
) -> None:
    try:
        payload: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "status": status,
            "content_hash": content_hash,
            "failure_signature": finding_signature(findings),
            "fail_findings": extract_fail_findings(findings),
        }
        if single_reviewer_no_diversity:
            payload["single_reviewer_no_diversity"] = True
        if raw_actor_records:
            payload["raw_actor_records"] = list(raw_actor_records)
        append_jsonl(review_history_path(drive_root, skill_name), payload)
    except Exception:
        log.debug("skill review history append failed", exc_info=True)


def append_history_once(
    drive_root: pathlib.Path,
    skill_name: str,
    payload: Dict[str, Any],
) -> bool:
    """Append one lifecycle terminal row, idempotently keyed by ``job_id``."""
    job_id = str(payload.get("job_id") or "")
    if not job_id:
        return False
    path = review_history_path(drive_root, skill_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = jsonl_append_lock_path(path)
    lock_fd = acquire_exclusive_file_lock(lock_path, timeout_sec=2.0, stale_sec=10.0)
    if lock_fd is None:
        return False
    try:
        try:
            if any(str(row.get("job_id") or "") == job_id for row in iter_jsonl_objects(path)):
                return True
            data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            return True
        except OSError:
            log.warning("skill review terminal history append failed for %s", skill_name, exc_info=True)
            return False
    finally:
        release_exclusive_file_lock(lock_path, lock_fd)
