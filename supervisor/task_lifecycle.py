"""Queue-owned acceptance, cancellation, and replay-safe resume transitions.

This module is a code boundary only: ``supervisor.queue`` remains the single
state authority and every mutation still runs under its existing process lock.
Imports of the queue are intentionally lazy so the public queue API can re-export
these helpers without creating an import cycle.
"""

from __future__ import annotations

import pathlib
import threading
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import utc_now_iso


_PROJECT_DELETE_WORKERS_LOCK = threading.Lock()
_PROJECT_DELETE_WORKERS: set[tuple[str, str]] = set()
BUDGET_ROOT_FENCES: Dict[str, Dict[str, Any]] = {}


def apply_budget_root_admission_fence(task: Dict[str, Any], root_task_id: str) -> bool:
    """Reject new work while a root is explicitly budget-paused.

    The monetary authority remains the physical-attempt ledger.  This marker is
    only an admission latch, preventing a budget increase from silently resuming
    a root after one of its dispatches was refused.
    """
    fence = BUDGET_ROOT_FENCES.get(str(root_task_id or ""))
    if not isinstance(fence, dict) or str(fence.get("status") or "") not in {
        "active", "paused",
    }:
        return False
    task["_admission_blocked"] = "root_budget_fence"
    task["_budget_root_task_id"] = root_task_id
    task["_budget_fence_id"] = str(fence.get("fence_id") or "")
    return True


def restore_queue_fences(
    raw_acceptance: Any, raw_budget: Any,
) -> tuple[set[str], bool, bool]:
    """Validate snapshot fences and restore the small root-budget admission map."""
    malformed_acceptance = not isinstance(raw_acceptance, list)
    fenced_roots: set[str] = set()
    if not malformed_acceptance:
        for fence in raw_acceptance:
            if not isinstance(fence, dict):
                malformed_acceptance = True
                break
            status = str(fence.get("status") or "")
            root_id = str(fence.get("root_task_id") or "")
            if status in {"active", "sealed"}:
                if not root_id:
                    malformed_acceptance = True
                    break
                fenced_roots.add(root_id)
    malformed_budget = not isinstance(raw_budget, list)
    restored: Dict[str, Dict[str, Any]] = {}
    if not malformed_budget:
        for fence in raw_budget:
            if not isinstance(fence, dict):
                malformed_budget = True
                break
            root_id = str(fence.get("root_task_id") or "").strip()
            fence_id = str(fence.get("fence_id") or "").strip()
            status = str(fence.get("status") or "")
            if status in {"active", "paused"}:
                if not root_id or not fence_id:
                    malformed_budget = True
                    break
                # Read old v6.64 candidates, but deliberately discard their
                # synchronized subtree lists and replay classification.  One
                # durable marker is the complete admission state.
                restored[root_id] = {
                    "status": "paused",
                    "scope": "root",
                    "root_task_id": root_id,
                    "fence_id": fence_id,
                    "auto_resume": False,
                    "paused_at": str(fence.get("paused_at") or utc_now_iso()),
                }
    if not malformed_budget:
        BUDGET_ROOT_FENCES.clear()
        BUDGET_ROOT_FENCES.update(restored)
    return fenced_roots, malformed_acceptance, malformed_budget


def _queue_module():
    from supervisor import queue

    return queue


def record_scheduled_admission(
    task: Dict[str, Any], admitted: Any, record: Dict[str, Any],
) -> None:
    """Project a cron dispatch refusal into terminal task/schedule state."""
    q = _queue_module()
    block = (
        str(admitted.get("_admission_blocked") or "")
        if isinstance(admitted, dict)
        else ""
    )
    if not block:
        record["failure_count"] = int(record.get("failure_count") or 0)
        record["last_error"] = ""
        return
    detail = f"Scheduled task was not queued: {block}."
    try:
        from ouroboros.task_results import STATUS_FAILED, write_task_result

        write_task_result(
            q.DRIVE_ROOT,
            str(task["id"]),
            STATUS_FAILED,
            result=detail,
            reason_code=block,
            cost_usd=0.0,
        )
    except Exception:
        q.log.warning(
            "Failed to terminalize admission-blocked scheduled task %s",
            task.get("id"),
            exc_info=True,
        )
    record["failure_count"] = int(record.get("failure_count") or 0) + 1
    record["last_error"] = detail


def transition_acceptance_fence(
    *, action: str, token: str, root_task_id: str = "", task_id: str = "", outcome: str = "",
    expected_generation: Optional[int] = None,
) -> Dict[str, Any]:
    """Atomically open, inspect, release, or seal a root admission fence."""
    q = _queue_module()
    action = str(action or "").strip().lower()
    token = str(token or "").strip()
    root_task_id = str(root_task_id or task_id or "").strip()
    if not token or action not in {"begin", "inspect", "end"}:
        return {"ok": False, "status": "error", "error": "invalid acceptance fence event"}
    with q._queue_lock:
        if action == "begin":
            if not root_task_id:
                return {"ok": False, "status": "error", "error": "missing root_task_id"}
            existing = q.ACCEPTANCE_FENCES.get(root_task_id)
            if isinstance(existing, dict) and str(existing.get("token") or "") != token:
                return {
                    "ok": False,
                    "status": "error",
                    "error": f"acceptance fence already active for root {root_task_id}",
                }
            if isinstance(existing, dict):
                row = existing
            else:
                row = q.ACCEPTANCE_FENCES[root_task_id] = {
                    "token": token,
                    "root_task_id": root_task_id,
                    "task_id": str(task_id or root_task_id),
                    "status": "active",
                    "opened_at": utc_now_iso(),
                    "owner_message_generation": 0,
                }
            result = {
                "ok": True,
                "status": "active",
                "root_task_id": root_task_id,
                "token": token,
                "owner_message_generation": int(row.get("owner_message_generation") or 0),
                "queue_descendants": _live_descendants_locked(
                    q, root_task_id, exclude_task_id=str(task_id or root_task_id),
                ),
            }
        else:
            matched_root = next(
                (rid for rid, row in q.ACCEPTANCE_FENCES.items() if str(row.get("token") or "") == token),
                "",
            )
            if not matched_root:
                return {"ok": False, "status": "error", "error": "unknown acceptance fence token"}
            row = q.ACCEPTANCE_FENCES[matched_root]
            if action == "inspect":
                return {
                    "ok": True,
                    "status": str(row.get("status") or "active"),
                    "root_task_id": matched_root,
                    "token": token,
                    "owner_message_generation": int(row.get("owner_message_generation") or 0),
                    "queue_descendants": _live_descendants_locked(
                        q, matched_root, exclude_task_id=str(row.get("task_id") or matched_root),
                    ),
                }
            normalized_outcome = str(outcome or "").strip().lower()
            if normalized_outcome == "revision":
                q.ACCEPTANCE_FENCES.pop(matched_root, None)
                result = {
                    "ok": True,
                    "status": "released",
                    "root_task_id": matched_root,
                    "token": token,
                }
            elif (
                expected_generation is not None
                and int(row.get("owner_message_generation") or 0) != int(expected_generation)
            ):
                current_generation = int(row.get("owner_message_generation") or 0)
                q.ACCEPTANCE_FENCES.pop(matched_root, None)
                result = {
                    "ok": True,
                    "status": "released",
                    "root_task_id": matched_root,
                    "token": token,
                    "generation_mismatch": True,
                    "expected_generation": int(expected_generation),
                    "owner_message_generation": current_generation,
                }
            else:
                row["status"] = "sealed"
                row["outcome"] = normalized_outcome or "terminal"
                row["sealed_at"] = utc_now_iso()
                result = {
                    "ok": True,
                    "status": "sealed",
                    "root_task_id": matched_root,
                    "token": token,
                }
    q.persist_queue_snapshot(reason=f"acceptance_fence_{result['status']}")
    return result


def _live_descendants_locked(
    q: Any, root_task_id: str, *, exclude_task_id: str = "",
) -> List[Dict[str, str]]:
    """Return a compact descendant snapshot while the queue lock is held."""
    rows: List[Dict[str, str]] = []
    for task in q.PENDING:
        task_id = str(task.get("id") or "") if isinstance(task, dict) else ""
        if task_id and task_id != exclude_task_id and q._is_descendant_of(task, root_task_id):
            rows.append({"task_id": task_id, "status": "pending", "source": "supervisor_queue"})
    for task_id, meta in q.RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) else None
        if (
            task_id
            and str(task_id) != exclude_task_id
            and isinstance(task, dict)
            and q._is_descendant_of(task, root_task_id)
        ):
            rows.append({"task_id": str(task_id), "status": "running", "source": "supervisor_queue"})
    return rows


def clear_acceptance_fence_for_root(root_task_id: str) -> bool:
    """Release a terminal root's fence after its task_done is queue-visible."""
    q = _queue_module()
    root_task_id = str(root_task_id or "").strip()
    if not root_task_id:
        return False
    with q._queue_lock:
        return q.ACCEPTANCE_FENCES.pop(root_task_id, None) is not None


def cancel_task_by_id(task_id: str, *, cascade: bool = False) -> bool:
    """Cancel a task and, when requested, its atomically captured live subtree."""
    q = _queue_module()
    task_id = str(task_id or "").strip()
    if not task_id:
        return False
    if not cascade:
        return q._cancel_task_by_id_single(task_id)
    with q._queue_lock:
        live: Dict[str, Dict[str, Any]] = {
            str(task["id"]): task
            for task in q.PENDING
            if isinstance(task, dict) and str(task.get("id") or "")
        }
        live.update({
            str(running_id): meta["task"]
            for running_id, meta in q.RUNNING.items()
            if isinstance(meta, dict) and isinstance(meta.get("task"), dict)
        })
        descendants: List[Tuple[int, str]] = []
        for live_id, task in live.items():
            if live_id == task_id:
                continue
            root_id = str(task.get("root_task_id") or "")
            current = task
            distance = 0
            seen: set[str] = set()
            reaches_target = root_id == task_id
            while isinstance(current, dict) and distance < 100:
                parent_id = str(current.get("parent_task_id") or "")
                if not parent_id or parent_id in seen:
                    break
                distance += 1
                if parent_id == task_id:
                    reaches_target = True
                    break
                seen.add(parent_id)
                current = live.get(parent_id)
            if reaches_target:
                try:
                    distance = max(distance, int(task.get("depth") or 0))
                except (TypeError, ValueError):
                    pass
                descendants.append((distance, live_id))
        cancel_order = [item[1] for item in sorted(descendants, reverse=True)] + [task_id]
    q.append_jsonl(
        pathlib.Path(q.DRIVE_ROOT) / "logs" / "supervisor.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "task_cancel_subtree_snapshot",
            "root_task_id": task_id,
            "descendant_task_ids": cancel_order[:-1],
            "descendant_count": len(cancel_order) - 1,
        },
    )
    cancelled = False
    for live_id in cancel_order:
        cancelled = q._cancel_task_by_id_single(live_id) or cancelled
    return cancelled


def resume_budget_paused_task(task_id: str) -> Dict[str, Any]:
    """Explicitly resume one zero-dispatch task and, if needed, its root latch."""
    q = _queue_module()
    task_id = str(task_id or "").strip()
    if not task_id:
        return {"ok": False, "error": "missing_task_id"}
    with q._queue_lock:
        task = next((item for item in q.PENDING if str(item.get("id") or "") == task_id), None)
        if task is None:
            return {"ok": False, "error": "task_not_pending"}
        pause = task.get("_budget_pause") if isinstance(task.get("_budget_pause"), dict) else None
        if not pause:
            # A root marker blocks every already-pending sibling without
            # copying pause state onto each task.  An explicit resume request
            # may nominate any genuinely zero-dispatch member of that root.
            candidate_root = str(task.get("root_task_id") or task_id).strip()
            candidate_fence = q.BUDGET_ROOT_FENCES.get(candidate_root)
            if not isinstance(candidate_fence, dict):
                return {"ok": False, "error": "task_not_budget_paused"}
            pause = {
                **candidate_fence,
                "status": "paused_before_dispatch",
                "physical_calls": 0,
                "replay_safe": True,
                "resume_policy": "manual_same_generation",
            }
        root_scope = str(pause.get("scope") or "") == "root"
        root_task_id = str(pause.get("root_task_id") or "").strip()
        fence = q.BUDGET_ROOT_FENCES.get(root_task_id) if root_scope and root_task_id else None
        if root_scope and not isinstance(fence, dict):
            return {"ok": False, "error": "root_budget_fence_missing", "action": "cancel_or_new_run"}
        if root_scope and str(pause.get("fence_id") or "") != str(fence.get("fence_id") or ""):
            return {"ok": False, "error": "replay_unsafe", "action": "cancel_or_new_run"}
        def _pending_member_is_replay_safe(member: Dict[str, Any]) -> tuple[bool, str]:
            member_id = str(member.get("id") or "")
            cost_fields = q.reconstruct_task_cost(
                member_id,
                fields=True,
                drive_root=pathlib.Path(member.get("budget_drive_root") or q.DRIVE_ROOT),
            )
            if cost_fields.get("cost_accounting_status") != "available":
                return False, "accounting_unavailable"
            retry_lineage = bool(
                int(member.get("_attempt") or 1) > 1
                or member.get("original_task_id") or member.get("timeout_retry_from")
            )
            return bool(
                int(cost_fields.get("total_rounds") or 0) == 0
                and not bool(cost_fields.get("ledger_integrity_degraded"))
                and not retry_lineage
            ), "replay_unsafe"

        nominated_safe, nominated_error = _pending_member_is_replay_safe(task)
        nominated_safe = bool(
            nominated_safe
            and pause.get("replay_safe")
            and pause.get("physical_calls") == 0
        )
        if not nominated_safe:
            return {
                "ok": False,
                "error": nominated_error,
                "action": "cancel_or_new_run",
            }
        if root_scope:
            # Clearing one root latch makes every pending member assignable. Check
            # those members together under the existing queue lock; completed
            # historical siblings are deliberately irrelevant.
            unsafe_members: list[str] = []
            for member in q.PENDING:
                member_id = str(member.get("id") or "")
                member_root = str(member.get("root_task_id") or member_id)
                if member_root != root_task_id or member_id == task_id:
                    continue
                member_safe, _member_error = _pending_member_is_replay_safe(member)
                if not member_safe:
                    unsafe_members.append(member_id)
            if unsafe_members:
                return {
                    "ok": False,
                    "error": "root_replay_unsafe",
                    "unsafe_task_ids": unsafe_members,
                    "action": "cancel_or_new_run",
                }

        resumed_at = utc_now_iso()
        prior_pause = dict(pause)
        task.pop("_budget_pause", None)
        task["budget_resumed_at"] = resumed_at
        if root_scope:
            q.BUDGET_ROOT_FENCES.pop(root_task_id, None)
        q.persist_queue_snapshot(
            reason="budget_root_explicit_resume" if root_scope else "budget_pause_explicit_resume",
        )
    try:
        from ouroboros.task_results import STATUS_SCHEDULED, write_task_result

        write_task_result(
            pathlib.Path(task.get("budget_drive_root") or q.DRIVE_ROOT),
            task_id,
            STATUS_SCHEDULED,
            reason_code="",
            resource_limit={
                **prior_pause,
                "status": "resumed",
                "resumed_at": resumed_at,
                "auto_resume": False,
            },
        )
    except Exception:
        q.log.debug("Failed to project explicit budget resume for %s", task_id, exc_info=True)
    q.append_jsonl(
        q.DRIVE_ROOT / "logs" / "events.jsonl",
        {
            "ts": utc_now_iso(),
            "type": "budget_task_explicitly_resumed",
            "task_id": task_id,
            "root_task_id": root_task_id if root_scope else "",
            "same_generation": True,
        },
    )
    return {"ok": True, "task_id": task_id, "same_generation": True}


def _live_project_task_ids(drive_root: object, project_id: str) -> list[str]:
    """Snapshot queued/running tasks associated with one fenced Project."""
    from ouroboros.projects_registry import project_task_bindings

    q = _queue_module()
    with q._queue_lock:
        rows = [dict(task) for task in q.PENDING if isinstance(task, dict)]
        rows.extend(
            dict(meta.get("task"))
            for meta in q.RUNNING.values()
            if isinstance(meta, dict) and isinstance(meta.get("task"), dict)
        )
    bindings = project_task_bindings(drive_root)
    associated: set[str] = set()
    by_id: dict[str, dict] = {}
    for task in rows:
        task_id = str(task.get("id") or task.get("task_id") or "").strip()
        if not task_id:
            continue
        by_id[task_id] = task
        lineage = (task_id, str(task.get("parent_task_id") or ""), str(task.get("root_task_id") or ""))
        if str(task.get("project_id") or "") == project_id or any(
            isinstance(bindings.get(candidate), dict)
            and str(bindings[candidate].get("project_id") or "") == project_id
            for candidate in lineage
            if candidate
        ):
            associated.add(task_id)
    changed = True
    while changed:
        changed = False
        for task_id, task in by_id.items():
            if task_id in associated:
                continue
            if (
                str(task.get("parent_task_id") or "") in associated
                or str(task.get("root_task_id") or "") in associated
            ):
                associated.add(task_id)
                changed = True
    return sorted(
        associated,
        key=lambda task_id: bool(str(by_id.get(task_id, {}).get("parent_task_id") or "")),
        reverse=True,
    )


def _broadcast_projects_changed(project_id: str, chat_id: Any) -> None:
    try:
        from supervisor.message_bus import get_bridge

        get_bridge().broadcast({"type": "projects_changed", "project_id": project_id, "chat_id": chat_id})
    except Exception:
        _queue_module().log.debug("projects_changed broadcast failed for %s", project_id, exc_info=True)


def run_project_deletion(
    drive_root: object,
    project_id: str,
    chat_id: Any,
    worker_key: tuple[str, str] | None = None,
) -> None:
    """Cancel a fenced Project tree and tombstone only after quiescence."""
    from ouroboros.projects_registry import complete_project_deletion, fail_project_deletion

    q = _queue_module()
    try:
        while True:
            live_ids = _live_project_task_ids(drive_root, project_id)
            if not live_ids:
                complete_project_deletion(drive_root, project_id)
                _broadcast_projects_changed(project_id, chat_id)
                return
            errors: list[str] = []
            for task_id in live_ids:
                try:
                    q.cancel_task_by_id(task_id, cascade=True)
                except Exception as exc:
                    errors.append(f"{task_id}: {type(exc).__name__}: {exc}")
            remaining = _live_project_task_ids(drive_root, project_id)
            if not remaining:
                complete_project_deletion(drive_root, project_id)
                _broadcast_projects_changed(project_id, chat_id)
                return
            if set(remaining) >= set(live_ids):
                detail = "; ".join(errors) if errors else "cancel_task_by_id left tasks live"
                raise RuntimeError(f"Project deletion did not quiesce ({', '.join(remaining)}): {detail}")
    except Exception as exc:
        q.log.exception("Project deletion failed for %s", project_id)
        fail_project_deletion(drive_root, project_id, f"{type(exc).__name__}: {exc}")
        _broadcast_projects_changed(project_id, chat_id)
    finally:
        if worker_key is not None:
            with _PROJECT_DELETE_WORKERS_LOCK:
                _PROJECT_DELETE_WORKERS.discard(worker_key)


def start_project_deletion(drive_root: object, project_id: str, chat_id: Any) -> bool:
    """Start one cancellation worker per Project and server generation."""
    key = (str(drive_root), str(project_id))
    with _PROJECT_DELETE_WORKERS_LOCK:
        if key in _PROJECT_DELETE_WORKERS:
            return False
        _PROJECT_DELETE_WORKERS.add(key)
    threading.Thread(
        target=run_project_deletion,
        args=(drive_root, project_id, chat_id, key),
        name=f"project-delete-{project_id}",
        daemon=True,
    ).start()
    return True


def resume_project_deletions(drive_root: object) -> int:
    """Resume interrupted deletion workers from durable registry state."""
    from ouroboros.projects_registry import PROJECT_DELETING, list_sidebar_projects

    started = 0
    for project in list_sidebar_projects(drive_root):
        if str(project.get("lifecycle") or "") != PROJECT_DELETING:
            continue
        started += int(start_project_deletion(
            drive_root,
            str(project.get("id") or ""),
            project.get("chat_id"),
        ))
    return started
