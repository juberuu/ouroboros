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


def init(drive_root: pathlib.Path, total_budget_limit: float = 0.0) -> None:
    global DRIVE_ROOT, STATE_PATH, STATE_LAST_GOOD_PATH, STATE_LOCK_PATH, QUEUE_SNAPSHOT_PATH
    DRIVE_ROOT = drive_root
    STATE_PATH = drive_root / "state" / "state.json"
    STATE_LAST_GOOD_PATH = drive_root / "state" / "state.last_good.json"
    STATE_LOCK_PATH = drive_root / "locks" / "state.lock"
    QUEUE_SNAPSHOT_PATH = drive_root / "state" / "queue_snapshot.json"
    set_budget_limit(total_budget_limit)


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
    st.setdefault("spent_tokens_cached", 0)
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
        "spent_tokens_cached": 0,
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


# ---------------------------------------------------------------------------
# Budget tracking (moved from workers.py)
# ---------------------------------------------------------------------------
TOTAL_BUDGET_LIMIT: float = 0.0


def set_budget_limit(limit: float) -> None:
    """Set total budget limit for budget_pct calculation."""
    global TOTAL_BUDGET_LIMIT
    TOTAL_BUDGET_LIMIT = limit


def budget_pct(st: Dict[str, Any]) -> float:
    """Calculate budget percentage used."""
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    if total <= 0:
        return 0.0
    return (spent / total) * 100.0


def update_budget_from_usage(usage: Dict[str, Any]) -> None:
    """Update state with LLM usage costs and tokens."""
    def _to_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    def _to_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return default

    st = load_state()
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if cost is None:
        cost = 0.0
    st["spent_usd"] = _to_float(st.get("spent_usd") or 0.0) + _to_float(cost)
    # Count actual LLM rounds, not tasks
    rounds = _to_int(usage.get("rounds") if isinstance(usage, dict) else 0, default=1)
    st["spent_calls"] = int(st.get("spent_calls") or 0) + rounds
    st["spent_tokens_prompt"] = _to_int(st.get("spent_tokens_prompt") or 0) + _to_int(
        usage.get("prompt_tokens") if isinstance(usage, dict) else 0)
    st["spent_tokens_completion"] = _to_int(st.get("spent_tokens_completion") or 0) + _to_int(
        usage.get("completion_tokens") if isinstance(usage, dict) else 0)
    st["spent_tokens_cached"] = _to_int(st.get("spent_tokens_cached") or 0) + _to_int(
        usage.get("cached_tokens") if isinstance(usage, dict) else 0)
    save_state(st)


# ---------------------------------------------------------------------------
# Status text (moved from workers.py)
# ---------------------------------------------------------------------------

def status_text(workers_dict: Dict[int, Any], pending_list: list, running_dict: Dict[str, Dict[str, Any]],
                soft_timeout_sec: int, hard_timeout_sec: int) -> str:
    """Build status text from worker and queue state."""
    st = load_state()
    now = time.time()
    lines = []
    lines.append(f"owner_id: {st.get('owner_id')}")
    lines.append(f"session_id: {st.get('session_id')}")
    lines.append(f"version: {st.get('current_branch')}@{(st.get('current_sha') or '')[:8]}")
    busy_count = sum(1 for w in workers_dict.values() if getattr(w, 'busy_task_id', None) is not None)
    lines.append(f"workers: {len(workers_dict)} (busy: {busy_count})")
    lines.append(f"pending: {len(pending_list)}")
    lines.append(f"running: {len(running_dict)}")
    if pending_list:
        preview = []
        for t in pending_list[:10]:
            preview.append(
                f"{t.get('id')}:{t.get('type')}:pr{t.get('priority')}:a{int(t.get('_attempt') or 1)}")
        lines.append("pending_queue: " + ", ".join(preview))
    if running_dict:
        lines.append("running_ids: " + ", ".join(list(running_dict.keys())[:10]))
    busy = [f"{getattr(w, 'wid', '?')}:{getattr(w, 'busy_task_id', '?')}"
            for w in workers_dict.values() if getattr(w, 'busy_task_id', None)]
    if busy:
        lines.append("busy: " + ", ".join(busy))
    if running_dict:
        details = []
        for task_id, meta in list(running_dict.items())[:10]:
            task = meta.get("task") if isinstance(meta, dict) else {}
            started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
            hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
            runtime_sec = int(max(0.0, now - started)) if started > 0 else 0
            hb_lag_sec = int(max(0.0, now - hb)) if hb > 0 else -1
            details.append(
                f"{task_id}:type={task.get('type')} pr={task.get('priority')} "
                f"attempt={meta.get('attempt')} runtime={runtime_sec}s hb_lag={hb_lag_sec}s")
        if details:
            lines.append("running_details:")
            lines.extend([f"  - {d}" for d in details])
    if running_dict and busy_count == 0:
        lines.append("queue_warning: running>0 while busy=0")
    spent = float(st.get("spent_usd") or 0.0)
    pct = budget_pct(st)
    if pct > 0:
        lines.append(f"spent_usd: ${spent:.2f} ({pct:.1f}% of budget)")
    else:
        lines.append(f"spent_usd: ${spent:.2f}")
    lines.append(f"spent_calls: {st.get('spent_calls')}")
    lines.append(f"prompt_tokens: {st.get('spent_tokens_prompt')}, completion_tokens: {st.get('spent_tokens_completion')}, cached_tokens: {st.get('spent_tokens_cached')}")
    lines.append(
        "evolution: "
        + f"enabled={int(bool(st.get('evolution_mode_enabled')))}, "
        + f"cycle={int(st.get('evolution_cycle') or 0)}")
    lines.append(f"last_owner_message_at: {st.get('last_owner_message_at') or '-'}")
    lines.append(f"timeouts: soft={soft_timeout_sec}s, hard={hard_timeout_sec}s")
    return "\n".join(lines)


def rotate_chat_log_if_needed(drive_root: pathlib.Path, max_bytes: int = 800_000) -> None:
    """Rotate chat log if it exceeds max_bytes."""
    chat = drive_root / "logs" / "chat.jsonl"
    if not chat.exists():
        return
    if chat.stat().st_size < max_bytes:
        return
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_path = drive_root / "archive" / f"chat_{ts}.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(chat.read_bytes())
    chat.write_text("", encoding="utf-8")
