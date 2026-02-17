"""
Supervisor event dispatcher.

Maps event types from worker EVENT_Q to handler functions.
Extracted from colab_launcher.py main loop to keep it under 500 lines.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import time
import uuid
from typing import Any, Dict

# Lazy imports to avoid circular dependencies — everything comes through ctx

log = logging.getLogger(__name__)


def _handle_llm_usage(evt: Dict[str, Any], ctx: Any) -> None:
    usage = evt.get("usage") or {}
    ctx.update_budget_from_usage(usage)

    # Log to events.jsonl for audit trail
    from ouroboros.utils import utc_now_iso, append_jsonl
    try:
        append_jsonl(ctx.DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": evt.get("ts", utc_now_iso()),
            "type": "llm_usage",
            "task_id": evt.get("task_id", ""),
            "category": evt.get("category", "other"),
            "model": evt.get("model", ""),
            "cost": usage.get("cost", 0),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
        })
    except Exception:
        log.warning("Failed to log llm_usage event to events.jsonl", exc_info=True)
        pass


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
        log.debug("Failed to send typing action to chat", exc_info=True)
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
    task_type = str(evt.get("task_type") or "")
    wid = evt.get("worker_id")

    # Track evolution task success/failure for circuit breaker
    if task_type == "evolution":
        st = ctx.load_state()
        # Check if task produced meaningful output (successful evolution)
        # A successful evolution should have:
        # - Reasonable cost (not near-zero, indicating actual work)
        # - Multiple rounds (not just 1 retry)
        cost = float(evt.get("cost_usd") or 0)
        rounds = int(evt.get("total_rounds") or 0)

        # Heuristic: if cost > $0.10 and rounds >= 1, consider it successful
        # Empty responses typically cost < $0.01 and have 0-1 rounds
        if cost > 0.10 and rounds >= 1:
            # Success: reset failure counter
            st["evolution_consecutive_failures"] = 0
            ctx.save_state(st)
        else:
            # Likely failure (empty response or minimal work)
            failures = int(st.get("evolution_consecutive_failures") or 0) + 1
            st["evolution_consecutive_failures"] = failures
            ctx.save_state(st)
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "evolution_task_failure_tracked",
                    "task_id": task_id,
                    "consecutive_failures": failures,
                    "cost_usd": cost,
                    "rounds": rounds,
                },
            )

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


def _handle_toggle_evolution(evt: Dict[str, Any], ctx: Any) -> None:
    """Toggle evolution mode from LLM tool call."""
    enabled = bool(evt.get("enabled"))
    st = ctx.load_state()
    st["evolution_mode_enabled"] = enabled
    ctx.save_state(st)
    if not enabled:
        ctx.PENDING[:] = [t for t in ctx.PENDING if str(t.get("type")) != "evolution"]
        ctx.sort_pending()
        ctx.persist_queue_snapshot(reason="evolve_off_via_tool")
    if st.get("owner_chat_id"):
        state_str = "ON" if enabled else "OFF"
        ctx.send_with_budget(int(st["owner_chat_id"]), f"🧬 Evolution: {state_str} (via agent tool)")


def _handle_toggle_consciousness(evt: Dict[str, Any], ctx: Any) -> None:
    """Toggle background consciousness from LLM tool call."""
    action = str(evt.get("action") or "status")
    if action in ("start", "on"):
        result = ctx.consciousness.start()
    elif action in ("stop", "off"):
        result = ctx.consciousness.stop()
    else:
        status = "running" if ctx.consciousness.is_running else "stopped"
        result = f"Background consciousness: {status}"
    st = ctx.load_state()
    if st.get("owner_chat_id"):
        ctx.send_with_budget(int(st["owner_chat_id"]), f"🧠 {result}")


def _handle_send_photo(evt: Dict[str, Any], ctx: Any) -> None:
    """Send a photo (base64 PNG) to a Telegram chat."""
    import base64 as b64mod
    try:
        chat_id = int(evt.get("chat_id") or 0)
        image_b64 = str(evt.get("image_base64") or "")
        caption = str(evt.get("caption") or "")
        if not chat_id or not image_b64:
            return
        photo_bytes = b64mod.b64decode(image_b64)
        ok, err = ctx.TG.send_photo(chat_id, photo_bytes, caption=caption)
        if not ok:
            ctx.append_jsonl(
                ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "send_photo_error",
                    "chat_id": chat_id, "error": err,
                },
            )
    except Exception as e:
        ctx.append_jsonl(
            ctx.DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "send_photo_event_error", "error": repr(e),
            },
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
    "send_photo": _handle_send_photo,
    "toggle_evolution": _handle_toggle_evolution,
    "toggle_consciousness": _handle_toggle_consciousness,
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
