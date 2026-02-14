"""
Supervisor — Worker and queue management.

Multiprocessing workers, task queue, timeouts, evolution/review scheduling.
"""

from __future__ import annotations

import datetime
import json
import multiprocessing as mp
import os
import pathlib
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from supervisor.state import (
    load_state, save_state, append_jsonl, atomic_write_text,
    QUEUE_SNAPSHOT_PATH,
)
from supervisor.telegram import send_with_budget, log_chat
from supervisor import git_ops


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
REPO_DIR: pathlib.Path = pathlib.Path("/content/ouroboros_repo")
DRIVE_ROOT: pathlib.Path = pathlib.Path("/content/drive/MyDrive/Ouroboros")
MAX_WORKERS: int = 5
SOFT_TIMEOUT_SEC: int = 600
HARD_TIMEOUT_SEC: int = 1800
HEARTBEAT_STALE_SEC: int = 120
QUEUE_MAX_RETRIES: int = 1
TOTAL_BUDGET_LIMIT: float = 0.0
BRANCH_DEV: str = "ouroboros"
BRANCH_STABLE: str = "ouroboros-stable"

CTX = mp.get_context("fork")


def init(repo_dir: pathlib.Path, drive_root: pathlib.Path, max_workers: int,
         soft_timeout: int, hard_timeout: int, total_budget_limit: float,
         branch_dev: str = "ouroboros", branch_stable: str = "ouroboros-stable") -> None:
    global REPO_DIR, DRIVE_ROOT, MAX_WORKERS, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC
    global TOTAL_BUDGET_LIMIT, BRANCH_DEV, BRANCH_STABLE
    REPO_DIR = repo_dir
    DRIVE_ROOT = drive_root
    MAX_WORKERS = max_workers
    SOFT_TIMEOUT_SEC = soft_timeout
    HARD_TIMEOUT_SEC = hard_timeout
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BRANCH_DEV = branch_dev
    BRANCH_STABLE = branch_stable


# ---------------------------------------------------------------------------
# Worker data structures
# ---------------------------------------------------------------------------

@dataclass
class Worker:
    wid: int
    proc: mp.Process
    in_q: Any
    busy_task_id: Optional[str] = None


EVENT_Q = CTX.Queue()
WORKERS: Dict[int, Worker] = {}
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
CRASH_TS: List[float] = []
QUEUE_SEQ_COUNTER = 0


# ---------------------------------------------------------------------------
# Chat agent (direct mode)
# ---------------------------------------------------------------------------
_chat_agent = None


def _get_chat_agent():
    global _chat_agent
    if _chat_agent is None:
        sys.path.insert(0, str(REPO_DIR))
        from ouroboros.agent import make_agent
        _chat_agent = make_agent(
            repo_dir=str(REPO_DIR),
            drive_root=str(DRIVE_ROOT),
            event_queue=EVENT_Q,
        )
    return _chat_agent


def reset_chat_agent() -> None:
    global _chat_agent
    _chat_agent = None


def handle_chat_direct(chat_id: int, text: str) -> None:
    try:
        agent = _get_chat_agent()
        task = {
            "id": uuid.uuid4().hex[:8],
            "type": "task",
            "chat_id": chat_id,
            "text": text,
        }
        events = agent.handle_task(task)
        for e in events:
            EVENT_Q.put(e)
    except Exception as e:
        import traceback
        err_msg = f"⚠️ Ошибка: {type(e).__name__}: {e}"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "direct_chat_error",
                "error": repr(e),
                "traceback": str(traceback.format_exc())[:2000],
            },
        )
        try:
            from supervisor.telegram import get_tg
            get_tg().send_message(chat_id, err_msg)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Queue priority
# ---------------------------------------------------------------------------

def _task_priority(task_type: str) -> int:
    t = str(task_type or "").strip().lower()
    if t in ("task", "review"):
        return 0
    if t == "evolution":
        return 1
    return 2


def _queue_sort_key(task: Dict[str, Any]) -> Tuple[int, int]:
    pr = int(task.get("priority") or _task_priority(str(task.get("type") or "")))
    seq = int(task.get("_queue_seq") or 0)
    return pr, seq


def _sort_pending() -> None:
    PENDING.sort(key=_queue_sort_key)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def enqueue_task(task: Dict[str, Any], front: bool = False) -> Dict[str, Any]:
    global QUEUE_SEQ_COUNTER
    t = dict(task)
    QUEUE_SEQ_COUNTER += 1
    t.setdefault("priority", _task_priority(str(t.get("type") or "")))
    t.setdefault("_attempt", int(t.get("_attempt") or 1))
    t["_queue_seq"] = -QUEUE_SEQ_COUNTER if front else QUEUE_SEQ_COUNTER
    t["queued_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    PENDING.append(t)
    _sort_pending()
    return t


def queue_has_task_type(task_type: str) -> bool:
    tt = str(task_type or "")
    if any(str(t.get("type") or "") == tt for t in PENDING):
        return True
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        if isinstance(task, dict) and str(task.get("type") or "") == tt:
            return True
    return False


def persist_queue_snapshot(reason: str = "") -> None:
    pending_rows = []
    for t in PENDING:
        pending_rows.append({
            "id": t.get("id"), "type": t.get("type"), "priority": t.get("priority"),
            "attempt": t.get("_attempt"), "queued_at": t.get("queued_at"),
            "queue_seq": t.get("_queue_seq"),
            "task": {
                "id": t.get("id"), "type": t.get("type"), "chat_id": t.get("chat_id"),
                "text": t.get("text"), "priority": t.get("priority"),
                "_attempt": t.get("_attempt"), "review_reason": t.get("review_reason"),
                "review_source_task_id": t.get("review_source_task_id"),
            },
        })
    running_rows = []
    now = time.time()
    for task_id, meta in RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) else {}
        started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
        hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
        running_rows.append({
            "id": task_id, "type": task.get("type"), "priority": task.get("priority"),
            "attempt": meta.get("attempt"), "worker_id": meta.get("worker_id"),
            "runtime_sec": round(max(0.0, now - started), 2) if started > 0 else 0.0,
            "heartbeat_lag_sec": round(max(0.0, now - hb), 2) if hb > 0 else None,
            "soft_sent": bool(meta.get("soft_sent")), "task": task,
        })
    payload = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": reason,
        "pending_count": len(PENDING), "running_count": len(RUNNING),
        "pending": pending_rows, "running": running_rows,
    }
    try:
        atomic_write_text(QUEUE_SNAPSHOT_PATH, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass


def restore_pending_from_snapshot(max_age_sec: int = 900) -> int:
    if PENDING:
        return 0
    try:
        if not QUEUE_SNAPSHOT_PATH.exists():
            return 0
        snap = json.loads(QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return 0
        ts = str(snap.get("ts") or "")
        ts_unix = parse_iso_to_ts(ts)
        if ts_unix is None:
            return 0
        if (time.time() - ts_unix) > max_age_sec:
            return 0
        restored = 0
        for row in (snap.get("pending") or []):
            task = row.get("task") if isinstance(row, dict) else None
            if not isinstance(task, dict):
                continue
            if not task.get("id") or not task.get("chat_id"):
                continue
            enqueue_task(task)
            restored += 1
        if restored > 0:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "queue_restored_from_snapshot",
                    "restored_pending": restored,
                },
            )
            persist_queue_snapshot(reason="queue_restored")
        return restored
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Worker process
# ---------------------------------------------------------------------------

def worker_main(wid: int, in_q: Any, out_q: Any, repo_dir: str, drive_root: str) -> None:
    import sys as _sys
    _sys.path.insert(0, repo_dir)
    from ouroboros.agent import make_agent
    agent = make_agent(repo_dir=repo_dir, drive_root=drive_root, event_queue=out_q)
    while True:
        task = in_q.get()
        if task is None or task.get("type") == "shutdown":
            break
        events = agent.handle_task(task)
        for e in events:
            e2 = dict(e)
            e2["worker_id"] = wid
            out_q.put(e2)


def spawn_workers(n: int = 0) -> None:
    count = n or MAX_WORKERS
    WORKERS.clear()
    for i in range(count):
        in_q = CTX.Queue()
        proc = CTX.Process(target=worker_main,
                           args=(i, in_q, EVENT_Q, str(REPO_DIR), str(DRIVE_ROOT)))
        proc.daemon = True
        proc.start()
        WORKERS[i] = Worker(wid=i, proc=proc, in_q=in_q, busy_task_id=None)


def kill_workers() -> None:
    cleared_running = len(RUNNING)
    for w in WORKERS.values():
        if w.proc.is_alive():
            w.proc.terminate()
    for w in WORKERS.values():
        w.proc.join(timeout=5)
    WORKERS.clear()
    RUNNING.clear()
    persist_queue_snapshot(reason="kill_workers")
    if cleared_running:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "running_cleared_on_kill", "count": cleared_running,
            },
        )


def respawn_worker(wid: int) -> None:
    in_q = CTX.Queue()
    proc = CTX.Process(target=worker_main,
                       args=(wid, in_q, EVENT_Q, str(REPO_DIR), str(DRIVE_ROOT)))
    proc.daemon = True
    proc.start()
    WORKERS[wid] = Worker(wid=wid, proc=proc, in_q=in_q, busy_task_id=None)


def assign_tasks() -> None:
    for w in WORKERS.values():
        if w.busy_task_id is None and PENDING:
            task = PENDING.pop(0)
            w.busy_task_id = task["id"]
            w.in_q.put(task)
            now_ts = time.time()
            RUNNING[task["id"]] = {
                "task": dict(task), "worker_id": w.wid,
                "started_at": now_ts, "last_heartbeat_at": now_ts,
                "soft_sent": False, "attempt": int(task.get("_attempt") or 1),
            }
            task_type = str(task.get("type") or "")
            if task_type in ("evolution", "review"):
                st = load_state()
                if st.get("owner_chat_id"):
                    emoji = '🧬' if task_type == 'evolution' else '🔎'
                    send_with_budget(
                        int(st["owner_chat_id"]),
                        f"{emoji} {task_type.capitalize()} task {task['id']} started.",
                    )
            persist_queue_snapshot(reason="assign_task")


# ---------------------------------------------------------------------------
# Health + crash storm
# ---------------------------------------------------------------------------

def ensure_workers_healthy() -> None:
    for wid, w in list(WORKERS.items()):
        if not w.proc.is_alive():
            CRASH_TS.append(time.time())
            if w.busy_task_id and w.busy_task_id in RUNNING:
                meta = RUNNING.pop(w.busy_task_id) or {}
                task = meta.get("task") if isinstance(meta, dict) else None
                if isinstance(task, dict):
                    enqueue_task(task, front=True)
            respawn_worker(wid)
            persist_queue_snapshot(reason="worker_respawn_after_crash")

    now = time.time()
    CRASH_TS[:] = [t for t in CRASH_TS if (now - t) < 60.0]
    if len(CRASH_TS) >= 3:
        st = load_state()
        if st.get("owner_chat_id"):
            send_with_budget(int(st["owner_chat_id"]),
                             "⚠️ Частые падения воркеров. Переключаюсь на ouroboros-stable и перезапускаюсь.")
        ok_reset, msg_reset = git_ops.checkout_and_reset(
            BRANCH_STABLE, reason="crash_storm_fallback",
            unsynced_policy="rescue_and_reset",
        )
        if not ok_reset:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "crash_storm_reset_blocked", "error": msg_reset,
                },
            )
            if st.get("owner_chat_id"):
                send_with_budget(int(st["owner_chat_id"]),
                                 f"⚠️ Fallback reset в {BRANCH_STABLE} пропущен: {msg_reset}")
            CRASH_TS.clear()
            return
        deps_ok, deps_msg = git_ops.sync_runtime_dependencies(reason="crash_storm_fallback")
        if not deps_ok:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "crash_storm_deps_sync_failed", "error": deps_msg,
                },
            )
            if st.get("owner_chat_id"):
                send_with_budget(int(st["owner_chat_id"]),
                                 f"⚠️ Fallback в {BRANCH_STABLE} применён, но установка зависимостей упала: {deps_msg}")
            CRASH_TS.clear()
            return
        kill_workers()
        spawn_workers()
        CRASH_TS.clear()


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

def enforce_task_timeouts() -> None:
    if not RUNNING:
        return
    now = time.time()
    st = load_state()
    owner_chat_id = int(st.get("owner_chat_id") or 0)

    for task_id, meta in list(RUNNING.items()):
        if not isinstance(meta, dict):
            continue
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        started_at = float(meta.get("started_at") or 0.0)
        if started_at <= 0:
            continue
        last_hb = float(meta.get("last_heartbeat_at") or started_at)
        runtime_sec = max(0.0, now - started_at)
        hb_lag_sec = max(0.0, now - last_hb)
        hb_stale = hb_lag_sec >= HEARTBEAT_STALE_SEC
        worker_id = int(meta.get("worker_id") or -1)
        task_type = str(task.get("type") or "")
        attempt = int(meta.get("attempt") or task.get("_attempt") or 1)

        if runtime_sec >= SOFT_TIMEOUT_SEC and not bool(meta.get("soft_sent")):
            meta["soft_sent"] = True
            if owner_chat_id:
                send_with_budget(
                    owner_chat_id,
                    f"⏱️ Задача {task_id} работает {int(runtime_sec)}с. "
                    f"type={task_type}, heartbeat_lag={int(hb_lag_sec)}с. Продолжаю.",
                )

        if runtime_sec < HARD_TIMEOUT_SEC:
            continue

        RUNNING.pop(task_id, None)
        if worker_id in WORKERS and WORKERS[worker_id].busy_task_id == task_id:
            WORKERS[worker_id].busy_task_id = None

        if worker_id in WORKERS:
            w = WORKERS[worker_id]
            try:
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
            except Exception:
                pass
            respawn_worker(worker_id)

        requeued = False
        new_attempt = attempt
        if attempt <= QUEUE_MAX_RETRIES and isinstance(task, dict):
            retried = dict(task)
            retried["_attempt"] = attempt + 1
            retried["timeout_retry_from"] = task_id
            retried["timeout_retry_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            enqueue_task(retried, front=True)
            requeued = True
            new_attempt = attempt + 1

        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "task_hard_timeout",
                "task_id": task_id, "task_type": task_type,
                "worker_id": worker_id, "runtime_sec": round(runtime_sec, 2),
                "heartbeat_lag_sec": round(hb_lag_sec, 2), "heartbeat_stale": hb_stale,
                "attempt": attempt, "requeued": requeued, "new_attempt": new_attempt,
                "max_retries": QUEUE_MAX_RETRIES,
            },
        )

        if owner_chat_id:
            if requeued:
                send_with_budget(owner_chat_id, (
                    f"🛑 Hard-timeout: задача {task_id} убита после {int(runtime_sec)}с.\n"
                    f"Worker {worker_id} перезапущен. Задача поставлена на retry attempt={new_attempt}."
                ))
            else:
                send_with_budget(owner_chat_id, (
                    f"🛑 Hard-timeout: задача {task_id} убита после {int(runtime_sec)}с.\n"
                    f"Worker {worker_id} перезапущен. Лимит retry исчерпан, задача остановлена."
                ))

        persist_queue_snapshot(reason="task_hard_timeout")


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def parse_iso_to_ts(iso_ts: str) -> Optional[float]:
    txt = str(iso_ts or "").strip()
    if not txt:
        return None
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def budget_pct(st: Dict[str, Any]) -> float:
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    if total <= 0:
        return 0.0
    return (spent / total) * 100.0


def update_budget_from_usage(usage: Dict[str, Any]) -> None:
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
    st["spent_calls"] = int(st.get("spent_calls") or 0) + 1
    st["spent_tokens_prompt"] = _to_int(st.get("spent_tokens_prompt") or 0) + _to_int(
        usage.get("prompt_tokens") if isinstance(usage, dict) else 0)
    st["spent_tokens_completion"] = _to_int(st.get("spent_tokens_completion") or 0) + _to_int(
        usage.get("completion_tokens") if isinstance(usage, dict) else 0)
    save_state(st)


def cancel_task_by_id(task_id: str) -> bool:
    for i, t in enumerate(list(PENDING)):
        if t["id"] == task_id:
            PENDING.pop(i)
            persist_queue_snapshot(reason="cancel_pending")
            return True
    for w in WORKERS.values():
        if w.busy_task_id == task_id:
            RUNNING.pop(task_id, None)
            if w.proc.is_alive():
                w.proc.terminate()
            w.proc.join(timeout=5)
            respawn_worker(w.wid)
            persist_queue_snapshot(reason="cancel_running")
            return True
    return False


def rotate_chat_log_if_needed(max_bytes: int = 800_000) -> None:
    chat = DRIVE_ROOT / "logs" / "chat.jsonl"
    if not chat.exists():
        return
    if chat.stat().st_size < max_bytes:
        return
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_path = DRIVE_ROOT / "archive" / f"chat_{ts}.jsonl"
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    archive_path.write_bytes(chat.read_bytes())
    chat.write_text("", encoding="utf-8")


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def status_text() -> str:
    st = load_state()
    now = time.time()
    lines = []
    lines.append(f"owner_id: {st.get('owner_id')}")
    lines.append(f"session_id: {st.get('session_id')}")
    lines.append(f"version: {st.get('current_branch')}@{(st.get('current_sha') or '')[:8]}")
    busy_count = sum(1 for w in WORKERS.values() if w.busy_task_id is not None)
    lines.append(f"workers: {len(WORKERS)} (busy: {busy_count})")
    lines.append(f"pending: {len(PENDING)}")
    lines.append(f"running: {len(RUNNING)}")
    if PENDING:
        preview = []
        for t in PENDING[:10]:
            preview.append(
                f"{t.get('id')}:{t.get('type')}:pr{t.get('priority')}:a{int(t.get('_attempt') or 1)}")
        lines.append("pending_queue: " + ", ".join(preview))
    if RUNNING:
        lines.append("running_ids: " + ", ".join(list(RUNNING.keys())[:10]))
    busy = [f"{w.wid}:{w.busy_task_id}" for w in WORKERS.values() if w.busy_task_id]
    if busy:
        lines.append("busy: " + ", ".join(busy))
    if RUNNING:
        details: List[str] = []
        for task_id, meta in list(RUNNING.items())[:10]:
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
    if RUNNING and busy_count == 0:
        lines.append("queue_warning: running>0 while busy=0")
    lines.append(f"spent_usd: {st.get('spent_usd')}")
    lines.append(f"spent_calls: {st.get('spent_calls')}")
    lines.append(f"prompt_tokens: {st.get('spent_tokens_prompt')}, completion_tokens: {st.get('spent_tokens_completion')}")
    lines.append(
        "evolution: "
        + f"enabled={int(bool(st.get('evolution_mode_enabled')))}, "
        + f"cycle={int(st.get('evolution_cycle') or 0)}")
    lines.append(f"last_owner_message_at: {st.get('last_owner_message_at') or '-'}")
    lines.append(f"timeouts: soft={SOFT_TIMEOUT_SEC}s, hard={HARD_TIMEOUT_SEC}s")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Evolution + review scheduling
# ---------------------------------------------------------------------------

def build_evolution_task_text(cycle: int) -> str:
    return f"EVOLUTION CYCLE #{cycle}\n\nСледуй инструкциям из prompts/SYSTEM.md, раздел «Режим эволюции»."


def build_review_task_text(reason: str) -> str:
    return f"DEEP REVIEW\n\nПричина: {reason or 'по запросу владельца'}\nScope и глубина — на твоё усмотрение."


def queue_review_task(reason: str, force: bool = False) -> Optional[str]:
    st = load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return None
    if (not force) and queue_has_task_type("review"):
        return None
    tid = uuid.uuid4().hex[:8]
    enqueue_task({
        "id": tid, "type": "review",
        "chat_id": int(owner_chat_id),
        "text": build_review_task_text(reason=reason),
    })
    persist_queue_snapshot(reason="review_enqueued")
    send_with_budget(int(owner_chat_id), f"🔎 Review в очереди: {tid} ({reason})")
    return tid


def enqueue_evolution_task_if_needed() -> None:
    if PENDING or RUNNING:
        return
    st = load_state()
    if not bool(st.get("evolution_mode_enabled")):
        return
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return
    if budget_pct(st) >= 100.0:
        st["evolution_mode_enabled"] = False
        save_state(st)
        send_with_budget(int(owner_chat_id), "💸 Эволюция остановлена: бюджет исчерпан.")
        return
    cycle = int(st.get("evolution_cycle") or 0) + 1
    tid = uuid.uuid4().hex[:8]
    enqueue_task({
        "id": tid, "type": "evolution",
        "chat_id": int(owner_chat_id),
        "text": build_evolution_task_text(cycle),
    })
    st["evolution_cycle"] = cycle
    st["last_evolution_task_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(st)
    send_with_budget(int(owner_chat_id), f"🧬 Evolution #{cycle}: {tid}")
