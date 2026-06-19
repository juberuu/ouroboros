"""Variant A: off-loop worker reaper (extracted from supervisor/queue.py for module size).

The supervisor loop must stay responsive (<100ms ticks), so a timed-out task's heaviest
teardown — process kill + join (up to ~5s) + service-log archive + worker respawn (process
spawn) — runs on a single-owner background reaper thread instead of inline under the queue
lock. The loop only marks the worker ``reaping`` (so assign_tasks/crash-detector skip the
slot) and hands a fully-decided job here; ``supervisor.queue`` re-exports the thin names
(``_reap_queue`` / ``_ensure_reaper_started`` / ``_reap_timed_out_task``).
"""

from __future__ import annotations

import logging
import pathlib
import queue as _stdqueue
import threading
from typing import Any, Dict, Optional

from ouroboros.outcomes import EXECUTION_INFRA_FAILED, terminal_outcome_axes
from ouroboros.utils import append_jsonl, truncate_review_artifact, utc_now_iso
from supervisor.message_bus import send_with_budget

log = logging.getLogger(__name__)

reap_queue: "_stdqueue.Queue[Dict[str, Any]]" = _stdqueue.Queue()
_reaper_thread: "Optional[threading.Thread]" = None
_reaper_start_lock = threading.Lock()


def reaper_loop() -> None:
    while True:
        try:
            job = reap_queue.get()
        except Exception:
            continue
        try:
            reap_timed_out_task(job)
        except Exception:
            log.error("Reaper failed for task %s", (job or {}).get("task_id"), exc_info=True)
            # Self-heal: an escape BEFORE the guarded teardown (e.g. the top-of-function
            # imports / variable extraction) must not strand the slot at reaping=True forever —
            # the crash detector skips reaping slots, so it would be unrecoverable until restart.
            # Clear reaping (the same conservative recovery step 5 uses) so a later tick reclaims
            # it; do NOT respawn here (an early escape may have left the original worker alive).
            try:
                from supervisor import workers as _w_mod
                from supervisor.queue import _queue_lock as _ql

                _wid_raw = (job or {}).get("worker_id")
                if _wid_raw is not None:
                    with _ql:
                        _w = _w_mod.WORKERS.get(int(_wid_raw))
                        if _w is not None:
                            _w.reaping = False
            except Exception:
                log.debug("Reaper: self-heal reaping-clear failed", exc_info=True)
        finally:
            try:
                reap_queue.task_done()
            except Exception:
                pass


def ensure_reaper_started() -> None:
    """Start the reaper thread, or RESTART it if it ever died — otherwise a dead reaper
    would strand every ``reaping=True`` slot forever (assign skips it, no one respawns it)."""
    global _reaper_thread
    t = _reaper_thread
    if t is not None and t.is_alive():
        return
    with _reaper_start_lock:
        t = _reaper_thread
        if t is not None and t.is_alive():
            return
        if t is not None:
            log.warning("Task reaper thread had died; restarting it.")
        _reaper_thread = threading.Thread(target=reaper_loop, name="task-reaper", daemon=True)
        _reaper_thread.start()


def reap_timed_out_task(job: Dict[str, Any]) -> None:
    """Full teardown for a timed-out task, run OFF the supervisor loop (Variant A).

    Order is load-bearing for correctness: kill+join the worker process FIRST, then decide
    the terminal write + retry. Because the original process is provably dead before the
    retry is enqueued, a still-alive worker can never race a concurrently-assigned retry
    (which, for a subagent, reuses the same task id/drive). A POST-KILL already-terminal
    re-check honors a worker that self-finalized at the idle boundary instead of clobbering
    its result or running a duplicate. The loop already popped RUNNING/cleared busy_task_id
    and marked the slot ``reaping`` under the lock; respawn_worker installs a fresh
    reaping=False Worker, re-opening the slot.
    """
    from supervisor import queue as _q
    from supervisor import workers as workers_mod

    worker_id = int(job.get("worker_id")) if job.get("worker_id") is not None else -1
    proc = job.get("proc")
    task_id = str(job.get("task_id") or "")
    task = job.get("task") if isinstance(job.get("task"), dict) else {}
    task_type = str(job.get("task_type") or "")
    terminal_reason = str(job.get("terminal_reason") or "idle_timeout")
    attempt = int(job.get("attempt") or 1)
    owner_chat_id = int(job.get("owner_chat_id") or 0)
    runtime_sec = float(job.get("runtime_sec") or 0.0)
    hb_lag_sec = float(job.get("hb_lag_sec") or 0.0)
    hb_stale = bool(job.get("hb_stale"))
    deadline_reached = bool(job.get("deadline_reached"))
    ceiling_reached = bool(job.get("ceiling_reached"))
    orchestrator = bool(job.get("orchestrator"))
    will_retry = bool(job.get("will_retry"))
    retry_task_id = str(job.get("retry_task_id") or "")

    # 1. Kill + join the worker process FIRST (off-lock).
    try:
        from ouroboros.platform_layer import kill_pid_tree

        # Spare deliberately-kept services so a timeout kill leaves verifier-facing
        # services alive; they reparent to init and the custody reaper governs them.
        _keep = _q._kept_service_pids()
        if proc is not None:
            if getattr(proc, "pid", None):
                kill_pid_tree(proc.pid, exclude_pids=_keep)
            elif proc.is_alive():
                proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive() and getattr(proc, "pid", None):
                kill_pid_tree(proc.pid, exclude_pids=_keep)
                proc.join(timeout=2)
    except Exception:
        log.warning("Reaper: failed to terminate worker %d for task %s", worker_id, task_id, exc_info=True)
    try:
        from ouroboros.tools.services import archive_task_service_logs

        archive_task_service_logs(pathlib.Path(_q.DRIVE_ROOT), task_id, task)
    except Exception:
        log.debug("Reaper: failed to archive service logs for %s", task_id, exc_info=True)

    from ouroboros.task_results import (
        STATUS_FAILED,
        STATUS_INTERRUPTED,
        STATUS_SCHEDULED,
        _TRULY_TERMINAL_STATUSES,
        load_task_result,
        write_task_result,
    )

    # 2. POST-KILL already-terminal re-check: the worker may have self-finalized right at
    #    the boundary. The process is dead now, so this decision is final.
    self_status = ""
    _existing = None
    try:
        _existing = load_task_result(_q.DRIVE_ROOT, task_id)
        if _existing and str(_existing.get("status") or "") in _TRULY_TERMINAL_STATUSES:
            self_status = str(_existing.get("status") or "")
    except Exception:
        log.debug("Reaper: post-kill terminal re-check failed for %s", task_id, exc_info=True)
    # Forked/workspace/subagent tasks self-finalize on the CHILD drive and are copied back
    # only on task_done; a worker that died after writing its child result but before
    # copy-back would be missed by the parent-drive check above. Mirror the child result
    # back and honor it (no interrupted/failed clobber, no duplicate retry).
    if not self_status:
        try:
            from ouroboros.headless import copy_child_task_result

            _child = copy_child_task_result(pathlib.Path(_q.DRIVE_ROOT), task)
            if _child and str(_child.get("status") or "") in _TRULY_TERMINAL_STATUSES:
                _existing = _child
                self_status = str(_child.get("status") or "")
        except Exception:
            log.debug("Reaper: child-drive terminal re-check failed for %s", task_id, exc_info=True)

    if self_status:
        # A mirrored child result (copy_child_task_result above sets artifact_status to
        # 'finalizing' for workspace tasks) still needs the artifact finalization the normal
        # task_done path runs in _handle_task_done. The reaper already terminalized the task,
        # so it is no longer in RUNNING and that path finds no task to finalize — complete it
        # here. Rescue ONLY a stuck non-terminal artifact state: re-running finalize on an
        # already-terminal result can regress it to FAILED (e.g. the workspace was cleaned
        # up). Readonly subagents have no durable artifacts and are skipped (shared gate).
        try:
            from ouroboros.headless import (
                ARTIFACT_STATUS_FINALIZING,
                ARTIFACT_STATUS_PENDING,
                finalize_task_artifacts,
                task_is_readonly_subagent,
            )

            _art = str((_existing or {}).get("artifact_status") or "").strip().lower()
            if _art in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING} and not task_is_readonly_subagent(task):
                finalize_task_artifacts(pathlib.Path(_q.DRIVE_ROOT), task)
        except Exception:
            log.debug("Reaper: artifact finalize for self-finalized %s failed", task_id, exc_info=True)

        # Honor the worker's own terminal result — do NOT clobber it or enqueue a retry.
        # The worker may have died before emitting its task_done (and the crash detector
        # now skips reaping slots), so emit an idempotent task_done so the UI card resolves.
        try:
            done_chat_id = int(task.get("chat_id") or 0) if isinstance(task, dict) else 0
            if done_chat_id:
                workers_mod.get_event_q().put({
                    "type": "task_done", "task_id": task_id, "task_type": task_type,
                    "chat_id": done_chat_id, "status": self_status,
                    "reason_code": str((_existing or {}).get("reason_code") or ""),
                })
        except Exception:
            log.debug("Reaper: failed to emit task_done for self-finalized %s", task_id, exc_info=True)
    else:
        # 3. Reconstruct real cost/rounds from durable llm_usage (the killed worker never
        #    finalized; the event would otherwise carry zeros and understate metrics).
        #    Guarded like every other sub-step so a failure here can never abort the reaper
        #    before step 5 (respawn) and strand the slot at reaping=True.
        try:
            recon_cost, recon_rounds, recon_prompt, recon_completion = _q.reconstruct_task_cost(task_id)
        except Exception:
            log.debug("Reaper: reconstruct_task_cost failed for %s", task_id, exc_info=True)
            recon_cost, recon_rounds, recon_prompt, recon_completion = 0.0, 0, 0, 0

        # Salvage the last persisted assistant text so a hard kill surfaces real progress.
        salvage_note = ""
        try:
            from ouroboros.observability import latest_llm_response_text
            salvaged = latest_llm_response_text(_q._task_drive_for_task(task, task_id), task_id)
            if salvaged:
                salvage_note = ("\n\nLast agent output (salvaged best-effort, unreviewed):\n"
                                + truncate_review_artifact(salvaged, 4000))
        except Exception:
            log.debug("Reaper: failed to salvage last LLM response for %s", task_id, exc_info=True)

        # A killed worker never reaches the loop's mailbox cleanup — remove the finalize_now
        # control so a subagent retry (same id/drive) is not instantly force-finalized.
        try:
            from ouroboros.owner_mailbox import cleanup_task_mailbox
            cleanup_task_mailbox(_q._task_drive_for_task(task, task_id), task_id)
        except Exception:
            log.debug("Reaper: failed to clean owner mailbox for killed task %s", task_id, exc_info=True)

        try:
            write_task_result(
                _q.DRIVE_ROOT, task_id,
                STATUS_INTERRUPTED if will_retry else STATUS_FAILED,
                reason_code=f"{terminal_reason}_retry" if will_retry else terminal_reason,
                outcome_axes=terminal_outcome_axes(
                    lifecycle=STATUS_INTERRUPTED if will_retry else STATUS_FAILED,
                    execution=EXECUTION_INFRA_FAILED,
                    reason_code=f"{terminal_reason}_retry" if will_retry else terminal_reason,
                    review_trigger="supervisor_terminal",
                ),
                superseded_by=retry_task_id if retry_task_id and retry_task_id != task_id else "",
                retry_task_id=retry_task_id if retry_task_id else "",
                cost_usd=recon_cost, total_rounds=recon_rounds,
                prompt_tokens=recon_prompt, completion_tokens=recon_completion,
                result=(
                    f"Task killed by {terminal_reason} after {int(runtime_sec)}s. Retrying."
                    if will_retry
                    else f"Task killed by {terminal_reason} after {int(runtime_sec)}s.{salvage_note}"
                ),
            )
            if will_retry and retry_task_id and retry_task_id != task_id:
                write_task_result(
                    _q.DRIVE_ROOT, retry_task_id, STATUS_SCHEDULED,
                    reason_code=f"{terminal_reason}_retry_scheduled",
                    outcome_axes=terminal_outcome_axes(
                        lifecycle=STATUS_SCHEDULED, execution="pending",
                        reason_code=f"{terminal_reason}_retry_scheduled",
                        review_trigger="supervisor_terminal",
                    ),
                    supersedes_task_id=task_id, original_task_id=task_id,
                    result=f"Retry scheduled after {terminal_reason}.",
                    parent_task_id=task.get("parent_task_id"),
                    root_task_id=task.get("root_task_id") or task_id,
                    description=task.get("description"), context=task.get("context"),
                    workspace_root=task.get("workspace_root"), workspace_mode=task.get("workspace_mode"),
                    memory_mode=task.get("memory_mode"),
                    metadata=task.get("metadata") if isinstance(task.get("metadata"), dict) else {},
                )
        except Exception:
            log.debug("Reaper: failed to write terminal result for %s", task_id, exc_info=True)

        # 4. Enqueue the retry ONLY now (original is dead) — no concurrent execution.
        #    Guarded so an enqueue failure cannot abort the reaper before respawn.
        requeued = False
        new_attempt = attempt
        if will_retry:
            try:
                retried = dict(task)
                retried["original_task_id"] = task_id
                retried["id"] = retry_task_id or task_id
                retried["_attempt"] = attempt + 1
                retried["timeout_retry_from"] = task_id
                retried["timeout_retry_at"] = utc_now_iso()
                _q.enqueue_task(retried, front=True)
                requeued = True
                new_attempt = attempt + 1
            except Exception:
                log.warning("Reaper: failed to enqueue retry for %s", task_id, exc_info=True)

        try:
            append_jsonl(
                _q.DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": utc_now_iso(), "type": "task_terminal_timeout",
                    "task_id": task_id, "task_type": task_type, "reason": terminal_reason,
                    "worker_id": worker_id, "runtime_sec": round(runtime_sec, 2),
                    "heartbeat_lag_sec": round(hb_lag_sec, 2), "heartbeat_stale": hb_stale,
                    "attempt": attempt, "requeued": requeued, "new_attempt": new_attempt,
                    "max_retries": _q.QUEUE_MAX_RETRIES, "reaped_off_loop": True,
                },
            )
        except Exception:
            log.debug("Reaper: failed to log task_terminal_timeout for %s", task_id, exc_info=True)

        # Guarded: a notification failure (e.g. a torn-down bus during shutdown) must NOT
        # abort the reaper before respawn, or the slot would stay reaping=True forever.
        if owner_chat_id:
            try:
                if requeued:
                    send_with_budget(owner_chat_id, (
                        f"🛑 {terminal_reason}: task {task_id} killed after {int(runtime_sec)}s.\n"
                        f"Worker {worker_id} restarted. Task queued for retry attempt={new_attempt}."
                    ))
                else:
                    if ceiling_reached:
                        stop_detail = "Absolute ceiling reached; task stopped."
                    elif deadline_reached:
                        stop_detail = "Absolute deadline reached; task stopped."
                    elif orchestrator:
                        stop_detail = ("Idle with live children (orchestrator); stopped without a "
                                       "blind retry to avoid replaying the subtree.")
                    else:
                        stop_detail = "Retry limit exhausted, task stopped."
                    send_with_budget(owner_chat_id, (
                        f"🛑 {terminal_reason}: task {task_id} killed after {int(runtime_sec)}s.\n"
                        f"Worker {worker_id} restarted. {stop_detail}"
                    ))
            except Exception:
                log.debug("Reaper: failed to send owner notification for %s", task_id, exc_info=True)

        if not requeued:
            try:
                done_chat_id = int(task.get("chat_id") or 0) if isinstance(task, dict) else 0
                if done_chat_id:
                    workers_mod.get_event_q().put({
                        "type": "task_done", "task_id": task_id, "task_type": task_type,
                        "chat_id": done_chat_id, "status": "failed", "reason_code": terminal_reason,
                        "outcome_axes": terminal_outcome_axes(lifecycle="failed", execution=EXECUTION_INFRA_FAILED, reason_code=terminal_reason, review_trigger="supervisor_terminal"),
                        "cost_usd": recon_cost, "total_rounds": recon_rounds,
                        "prompt_tokens": recon_prompt, "completion_tokens": recon_completion,
                        "metadata": task.get("metadata") if isinstance(task.get("metadata"), dict) else {},
                    })
            except Exception:
                log.debug("Reaper: failed to emit task_done for %s", task_id, exc_info=True)

    # 5. Respawn a fresh worker for the slot; on failure, CLEAR reaping so the crash detector
    #    can recover the slot on a later tick instead of stranding it permanently.
    #    Hold _queue_lock across the membership check AND the respawn so it is mutually
    #    exclusive with kill_workers (which clears WORKERS under the same lock at shutdown).
    #    Otherwise the reaper could pass the check, start a replacement process, and insert it
    #    into WORKERS only AFTER shutdown cleanup already cleared the pool — an orphan worker
    #    surviving shutdown. _queue_lock is an RLock and respawn_worker re-acquires it
    #    internally, so taking it here is safe (and a cleared pool makes the check fail closed).
    try:
        with _q._queue_lock:
            if worker_id in workers_mod.WORKERS:
                workers_mod.respawn_worker(worker_id)
    except Exception:
        log.warning("Reaper: respawn failed for worker %d; clearing reaping for recovery", worker_id, exc_info=True)
        try:
            with _q._queue_lock:
                _w = workers_mod.WORKERS.get(worker_id)
                if _w is not None:
                    _w.reaping = False
        except Exception:
            pass
    try:
        _q.persist_queue_snapshot(reason="worker_respawn_after_reap")
    except Exception:
        log.debug("Reaper: failed to persist queue snapshot after respawn", exc_info=True)
