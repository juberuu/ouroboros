"""
Supervisor event dispatcher.

Maps event types from worker EVENT_Q to handler functions.
Extracted from colab_launcher.py main loop to keep it under 500 lines.
"""

from __future__ import annotations

import datetime
import os
import sys
import time
import uuid
from typing import Any, Dict

# Lazy imports to avoid circular dependencies — everything comes through ctx


def _handle_llm_usage(evt: Dict[str, Any], ctx: Any) -> None:
    ctx.update_budget_from_usage(evt.get("usage") or {})


def _handle_task_heartbeat(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "")
    if task_id and task_id in ctx.RUNNING:
        meta = ctx.RUNNING.get(task_id) or {}
        meta["last_heartbeat_at"] = time.time()
        phase = str(evt.get("phase") or "")
        if phase:
            meta["heartbeat_phase"] = phase
        ctx.RUNNING[task_id] = meta


def _handle_typing_start(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        chat_id = int(evt.get("chat_id") or 0)
        if chat_id:
            ctx.TG.send_chat_action(chat_id, "typing")
    except Exception:
        pass


def _handle_send_message(evt: Dict[str, Any], ctx: Any) -> None:
    try:
        log_text = evt.get("log_text")
        fmt = str(evt.get("format") or "")
        is_progress = bool(evt.get("is_progress"))
        ctx.send_with_budget(
            int(evt["chat_id"]),
            str(evt.get("text") or ""),
            log_text=(str(log_text) if isinstance(log_text, str) else None),
            fmt=fmt,
            is_progress=is_progress,
        )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "send_message_event_error", "error": repr(e),
            },
        )


def _handle_task_done(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = evt.get("task_id")
    wid = evt.get("worker_id")
    if task_id:
        ctx.RUNNING.pop(str(task_id), None)
    if wid in ctx.WORKERS and ctx.WORKERS[wid].busy_task_id == task_id:
        ctx.WORKERS[wid].busy_task_id = None
    ctx.persist_queue_snapshot(reason="task_done")


def _handle_task_metrics(evt: Dict[str, Any], ctx: Any) -> None:
    ctx.append_jsonl(
        ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "task_metrics_event",
            "task_id": str(evt.get("task_id") or ""),
            "task_type": str(evt.get("task_type") or ""),
            "duration_sec": round(float(evt.get("duration_sec") or 0.0), 3),
            "tool_calls": int(evt.get("tool_calls") or 0),
            "tool_errors": int(evt.get("tool_errors") or 0),
        },
    )


def _handle_review_request(evt: Dict[str, Any], ctx: Any) -> None:
    ctx.queue_review_task(
        reason=str(evt.get("reason") or "agent_review_request"), force=False
    )


def _handle_restart_request(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(
            int(st["owner_chat_id"]),
            f"♻️ Restart requested by agent: {evt.get('reason')}",
        )
    ok, msg = ctx.safe_restart(
        reason="agent_restart_request", unsynced_policy="rescue_and_reset"
    )
    if not ok:
        if st.get("owner_chat_id"):
            ctx.send_with_budget(int(st["owner_chat_id"]), f"⚠️ Restart пропущен: {msg}")
        return
    ctx.kill_workers()
    # Persist tg_offset/session_id before execv to avoid duplicate Telegram updates.
    st2 = ctx.load_state()
    st2["session_id"] = uuid.uuid4().hex
    st2["tg_offset"] = int(st2.get("tg_offset") or st.get("tg_offset") or 0)
    ctx.save_state(st2)
    ctx.persist_queue_snapshot(reason="pre_restart_exit")
    # Replace current process with fresh Python — loads all modules from scratch
    launcher = os.path.join(os.getcwd(), "colab_launcher.py")
    os.execv(sys.executable, [sys.executable, launcher])


def _handle_promote_to_stable(evt: Dict[str, Any], ctx: Any) -> None:
    import subprocess as sp
    try:
        sp.run(["git", "fetch", "origin"], cwd=str(ctx.REPO_DIR), check=True)
        sp.run(
            ["git", "push", "origin", f"{ctx.BRANCH_DEV}:{ctx.BRANCH_STABLE}"],
            cwd=str(ctx.REPO_DIR), check=True,
        )
        new_sha = sp.run(
            ["git", "rev-parse", f"origin/{ctx.BRANCH_STABLE}"],
            cwd=str(ctx.REPO_DIR), capture_output=True, text=True, check=True,
        ).stdout.strip()
        st = ctx.load_state()
        if st.get("owner_chat_id"):
            ctx.send_with_budget(
                int(st["owner_chat_id"]),
                f"✅ Промоут: {ctx.BRANCH_DEV} → {ctx.BRANCH_STABLE} ({new_sha[:8]})",
            )
    except Exception as e:
        st = ctx.load_state()
        if st.get("owner_chat_id"):
            ctx.send_with_budget(
                int(st["owner_chat_id"]),
                f"❌ Ошибка промоута в stable: {e}",
            )


def _handle_schedule_task(evt: Dict[str, Any], ctx: Any) -> None:
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    desc = str(evt.get("description") or "").strip()
    if owner_chat_id and desc:
        tid = uuid.uuid4().hex[:8]
        ctx.enqueue_task(
            {"id": tid, "type": "task", "chat_id": int(owner_chat_id), "text": desc}
        )
        ctx.send_with_budget(int(owner_chat_id), f"🗓️ Запланировал задачу {tid}: {desc}")
        ctx.persist_queue_snapshot(reason="schedule_task_event")


def _handle_cancel_task(evt: Dict[str, Any], ctx: Any) -> None:
    task_id = str(evt.get("task_id") or "").strip()
    st = ctx.load_state()
    owner_chat_id = st.get("owner_chat_id")
    ok = ctx.cancel_task_by_id(task_id) if task_id else False
    if owner_chat_id:
        ctx.send_with_budget(
            int(owner_chat_id),
            f"{'✅' if ok else '❌'} cancel {task_id or '?'} (event)",
        )


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------
EVENT_HANDLERS = {
    "llm_usage": _handle_llm_usage,
    "task_heartbeat": _handle_task_heartbeat,
    "typing_start": _handle_typing_start,
    "send_message": _handle_send_message,
    "task_done": _handle_task_done,
    "task_metrics": _handle_task_metrics,
    "review_request": _handle_review_request,
    "restart_request": _handle_restart_request,
    "promote_to_stable": _handle_promote_to_stable,
    "schedule_task": _handle_schedule_task,
    "cancel_task": _handle_cancel_task,
}


def dispatch_event(evt: Dict[str, Any], ctx: Any) -> None:
    """Dispatch a single worker event to its handler."""
    if not isinstance(evt, dict):
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "invalid_worker_event",
                "error": "event is not dict",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    event_type = str(evt.get("type") or "").strip()
    if not event_type:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "invalid_worker_event",
                "error": "missing event.type",
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    handler = EVENT_HANDLERS.get(event_type)
    if handler is None:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "unknown_worker_event",
                "event_type": event_type,
                "event_repr": repr(evt)[:1000],
            },
        )
        return

    try:
        handler(evt, ctx)
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "worker_event_handler_error",
                "event_type": event_type,
                "error": repr(e),
            },
        )
