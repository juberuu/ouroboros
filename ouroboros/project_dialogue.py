"""Small read model for canonical Project dialogue and routing annotations.

Project conversion stores a reference to the original owner row on the immutable
task binding. A Project room projects that row instead of copying it into
``chat.jsonl``. ``chat_annotations.jsonl`` is presentation-only: it never owns a
routing decision or Project state.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import uuid
from typing import Any, Dict, Iterable, List

from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.utils import iter_jsonl_objects, jsonl_append_lock_path, utc_now_iso

_ANNOTATIONS_NAME = "chat_annotations.jsonl"
_COMPACT_AT_BYTES = 800_000
_RETAINED_ARCHIVES = 3


def _chat_paths(drive_root: Any) -> List[pathlib.Path]:
    root = pathlib.Path(drive_root)
    archives = sorted(
        (root / "archive").glob("chat_*.jsonl"),
        key=lambda path: path.name,
        reverse=True,
    )[:_RETAINED_ARCHIVES]
    return [*reversed(archives), root / "logs" / "chat.jsonl"]


def _text_sha256(value: Any) -> str:
    normalized = " ".join(str(value or "").split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def find_owner_message_ref(
    drive_root: Any,
    text: str,
    *,
    source_chat_id: int = 0,
    client_message_id: str = "",
    not_after: str = "",
) -> Dict[str, Any]:
    """Find the latest matching canonical inbound row in retained chat history."""
    wanted_hash = _text_sha256(text)
    if not str(text or "").strip():
        return {}
    chosen: Dict[str, Any] = {}
    bound = str(not_after or "").strip()
    wanted_client_id = str(client_message_id or "").strip()
    for path in _chat_paths(drive_root):
        for row in iter_jsonl_objects(path):
            if str(row.get("direction") or "") != "in":
                continue
            try:
                row_chat_id = int(row.get("chat_id", 1) or 1)
            except (TypeError, ValueError):
                row_chat_id = 1
            if source_chat_id and row_chat_id != int(source_chat_id):
                continue
            if wanted_client_id and str(row.get("client_message_id") or "") != wanted_client_id:
                continue
            ts = str(row.get("ts") or "")
            if not ts or (bound and ts > bound) or _text_sha256(row.get("text")) != wanted_hash:
                continue
            if not chosen or ts >= str(chosen.get("ts") or ""):
                chosen = {
                    "chat_id": row_chat_id,
                    "ts": ts,
                    "text_sha256": wanted_hash,
                    **(
                        {"client_message_id": str(row.get("client_message_id"))}
                        if row.get("client_message_id") else {}
                    ),
                }
    return chosen


def source_refs_for_project(drive_root: Any, project_chat_id: int) -> List[Dict[str, Any]]:
    """Canonical owner-row references held by bindings for one Project lens."""
    from ouroboros.projects_registry import project_task_bindings

    refs: List[Dict[str, Any]] = []
    for row in project_task_bindings(drive_root).values():
        try:
            same_chat = int(row.get("project_chat_id") or 0) == int(project_chat_id or 0)
        except (TypeError, ValueError):
            same_chat = False
        ref = row.get("source_ref")
        if same_chat and isinstance(ref, dict) and ref:
            refs.append(dict(ref))
    return refs


def entry_matches_source_ref(entry: Dict[str, Any], refs: Iterable[Dict[str, Any]]) -> bool:
    """Whether ``entry`` is the original row identified by one binding ref."""
    if str(entry.get("direction") or "") != "in":
        return False
    try:
        entry_chat_id = int(entry.get("chat_id", 1) or 1)
    except (TypeError, ValueError):
        entry_chat_id = 1
    entry_client_id = str(entry.get("client_message_id") or "")
    entry_ts = str(entry.get("ts") or "")
    entry_hash = _text_sha256(entry.get("text"))
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        try:
            if int(ref.get("chat_id") or 0) != entry_chat_id:
                continue
        except (TypeError, ValueError):
            continue
        client_id = str(ref.get("client_message_id") or "")
        if client_id and client_id != entry_client_id:
            continue
        if str(ref.get("ts") or "") and str(ref.get("ts")) != entry_ts:
            continue
        if str(ref.get("text_sha256") or "") != entry_hash:
            continue
        return True
    return False


def _latest_annotations(path: pathlib.Path) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    for row in iter_jsonl_objects(path):
        message_id = str(row.get("client_message_id") or "")
        if message_id and row.get("type") == "chat_annotation":
            latest[message_id] = dict(row)
    return latest


def latest_chat_annotations(drive_root: Any) -> Dict[str, Dict[str, Any]]:
    """Latest presentation annotation per message; a torn tail is ignored."""
    path = pathlib.Path(drive_root) / "logs" / _ANNOTATIONS_NAME
    return _latest_annotations(path)


def _compact_annotations_locked(drive_root: Any, path: pathlib.Path) -> None:
    if not path.is_file() or path.stat().st_size < _COMPACT_AT_BYTES:
        return
    retained_ids = {
        str(row.get("client_message_id") or "")
        for chat_path in _chat_paths(drive_root)
        for row in iter_jsonl_objects(chat_path)
        if row.get("client_message_id")
    }
    rows = [
        row for message_id, row in _latest_annotations(path).items()
        if message_id in retained_ids
    ]
    rows.sort(key=lambda row: str(row.get("ts") or ""))
    tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
    data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def append_chat_annotation(
    drive_root: Any,
    client_message_id: str,
    *,
    action: str,
    target: str = "",
    status: str,
) -> bool:
    """Append one compact UI annotation; no semantic routing state is stored."""
    message_id = str(client_message_id or "").strip()
    if not message_id:
        return False
    row = {
        "ts": utc_now_iso(),
        "type": "chat_annotation",
        "client_message_id": message_id[:200],
        "action": str(action or "")[:80],
        "target": str(target or "")[:200],
        "status": str(status or "")[:80],
    }
    path = pathlib.Path(drive_root) / "logs" / _ANNOTATIONS_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = jsonl_append_lock_path(path)
    lock_fd = acquire_exclusive_file_lock(lock_path, timeout_sec=2.0, stale_sec=10.0)
    if lock_fd is None:
        return False
    try:
        data = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        _compact_annotations_locked(drive_root, path)
        return True
    finally:
        release_exclusive_file_lock(lock_path, lock_fd)


__all__ = [
    "append_chat_annotation",
    "entry_matches_source_ref",
    "find_owner_message_ref",
    "latest_chat_annotations",
    "source_refs_for_project",
]
