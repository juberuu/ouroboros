"""
Supervisor — State management.

Persistent state on Google Drive: load, save, atomic writes, file locks.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib
import time
import uuid
from typing import Any, Dict, Optional


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Ouroboros")
STATE_PATH: pathlib.Path = DRIVE_ROOT / "state" / "state.json"
STATE_LAST_GOOD_PATH: pathlib.Path = DRIVE_ROOT / "state" / "state.last_good.json"
STATE_LOCK_PATH: pathlib.Path = DRIVE_ROOT / "locks" / "state.lock"
QUEUE_SNAPSHOT_PATH: pathlib.Path = DRIVE_ROOT / "state" / "queue_snapshot.json"


def init(drive_root: pathlib.Path) -> None:
    global DRIVE_ROOT, STATE_PATH, STATE_LAST_GOOD_PATH, STATE_LOCK_PATH, QUEUE_SNAPSHOT_PATH
    DRIVE_ROOT = drive_root
    STATE_PATH = drive_root / "state" / "state.json"
    STATE_LAST_GOOD_PATH = drive_root / "state" / "state.last_good.json"
    STATE_LOCK_PATH = drive_root / "locks" / "state.lock"
    QUEUE_SNAPSHOT_PATH = drive_root / "state" / "queue_snapshot.json"


# ---------------------------------------------------------------------------
# Atomic file operations
# ---------------------------------------------------------------------------

def atomic_write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        data = content.encode("utf-8")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))


def json_load_file(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# File locks
# ---------------------------------------------------------------------------

def acquire_file_lock(lock_path: pathlib.Path, timeout_sec: float = 4.0,
                      stale_sec: float = 90.0) -> Optional[int]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while (time.time() - started) < timeout_sec:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"pid={os.getpid()} ts={datetime.datetime.now(datetime.timezone.utc).isoformat()}\n".encode("utf-8"))
            except Exception:
                pass
            return fd
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_sec:
                    lock_path.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.05)
        except Exception:
            break
    return None


def release_file_lock(lock_path: pathlib.Path, lock_fd: Optional[int]) -> None:
    if lock_fd is None:
        return
    try:
        os.close(lock_fd)
    except Exception:
        pass
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# JSONL append (simplified supervisor version, no concurrency)
# ---------------------------------------------------------------------------

def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

def ensure_state_defaults(st: Dict[str, Any]) -> Dict[str, Any]:
    st.setdefault("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
    st.setdefault("owner_id", None)
    st.setdefault("owner_chat_id", None)
    st.setdefault("tg_offset", 0)
    st.setdefault("spent_usd", 0.0)
    st.setdefault("spent_calls", 0)
    st.setdefault("spent_tokens_prompt", 0)
    st.setdefault("spent_tokens_completion", 0)
    st.setdefault("session_id", uuid.uuid4().hex)
    st.setdefault("current_branch", None)
    st.setdefault("current_sha", None)
    st.setdefault("last_owner_message_at", "")
    st.setdefault("last_evolution_task_at", "")
    st.setdefault("budget_messages_since_report", 0)
    st.setdefault("evolution_mode_enabled", False)
    st.setdefault("evolution_cycle", 0)
    for legacy_key in ("approvals", "idle_cursor", "idle_stats", "last_idle_task_at",
                        "last_auto_review_at", "last_review_task_id"):
        st.pop(legacy_key, None)
    return st


def default_state_dict() -> Dict[str, Any]:
    return {
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "owner_id": None,
        "owner_chat_id": None,
        "tg_offset": 0,
        "spent_usd": 0.0,
        "spent_calls": 0,
        "spent_tokens_prompt": 0,
        "spent_tokens_completion": 0,
        "session_id": uuid.uuid4().hex,
        "current_branch": None,
        "current_sha": None,
        "last_owner_message_at": "",
        "last_evolution_task_at": "",
        "budget_messages_since_report": 0,
        "evolution_mode_enabled": False,
        "evolution_cycle": 0,
    }


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def load_state() -> Dict[str, Any]:
    lock_fd = acquire_file_lock(STATE_LOCK_PATH)
    try:
        recovered = False
        st_obj = json_load_file(STATE_PATH)
        if st_obj is None:
            st_obj = json_load_file(STATE_LAST_GOOD_PATH)
            recovered = st_obj is not None

        if st_obj is None:
            st = ensure_state_defaults(default_state_dict())
            payload = json.dumps(st, ensure_ascii=False, indent=2)
            atomic_write_text(STATE_PATH, payload)
            atomic_write_text(STATE_LAST_GOOD_PATH, payload)
            return st

        st = ensure_state_defaults(st_obj)
        if recovered:
            payload = json.dumps(st, ensure_ascii=False, indent=2)
            atomic_write_text(STATE_PATH, payload)
            atomic_write_text(STATE_LAST_GOOD_PATH, payload)
        return st
    finally:
        release_file_lock(STATE_LOCK_PATH, lock_fd)


def save_state(st: Dict[str, Any]) -> None:
    st = ensure_state_defaults(st)
    lock_fd = acquire_file_lock(STATE_LOCK_PATH)
    try:
        payload = json.dumps(st, ensure_ascii=False, indent=2)
        atomic_write_text(STATE_PATH, payload)
        atomic_write_text(STATE_LAST_GOOD_PATH, payload)
    finally:
        release_file_lock(STATE_LOCK_PATH, lock_fd)
