"""
Supervisor — Worker lifecycle management.

Multiprocessing workers, worker health, direct chat handling.
Queue operations moved to supervisor.queue.
"""

from __future__ import annotations

import datetime
import multiprocessing as mp
import pathlib
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from supervisor.state import load_state, append_jsonl
from supervisor import git_ops
from supervisor.telegram import send_with_budget


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

CTX = mp.get_context("spawn")


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

    # Initialize queue module
    from supervisor import queue
    queue.init(drive_root, soft_timeout, hard_timeout)
    queue.init_queue_refs(PENDING, RUNNING, QUEUE_SEQ_COUNTER_REF)


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
QUEUE_SEQ_COUNTER_REF: Dict[str, int] = {"value": 0}


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
    # Purge cached ouroboros modules to force fresh import
    mods_to_remove = [k for k in sys.modules if k == 'ouroboros' or k.startswith('ouroboros.')]
    for k in mods_to_remove:
        del sys.modules[k]


def handle_chat_direct(chat_id: int, text: str, image_data: Optional[Tuple[str, str]] = None) -> None:
    try:
        agent = _get_chat_agent()
        task = {
            "id": uuid.uuid4().hex[:8],
            "type": "task",
            "chat_id": chat_id,
            "text": text or "(image attached)",
        }
        if image_data:
            task["image_base64"] = image_data[0]
            task["image_mime"] = image_data[1]
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
    from supervisor import queue
    cleared_running = len(RUNNING)
    for w in WORKERS.values():
        if w.proc.is_alive():
            w.proc.terminate()
    for w in WORKERS.values():
        w.proc.join(timeout=5)
    WORKERS.clear()
    RUNNING.clear()
    queue.persist_queue_snapshot(reason="kill_workers")
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
    from supervisor import queue
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
            queue.persist_queue_snapshot(reason="assign_task")


# ---------------------------------------------------------------------------
# Health + crash storm
# ---------------------------------------------------------------------------

def ensure_workers_healthy() -> None:
    from supervisor import queue
    for wid, w in list(WORKERS.items()):
        if not w.proc.is_alive():
            CRASH_TS.append(time.time())
            if w.busy_task_id and w.busy_task_id in RUNNING:
                meta = RUNNING.pop(w.busy_task_id) or {}
                task = meta.get("task") if isinstance(meta, dict) else None
                if isinstance(task, dict):
                    queue.enqueue_task(task, front=True)
            respawn_worker(wid)
            queue.persist_queue_snapshot(reason="worker_respawn_after_crash")

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


