"""Durable root post-task phase and final-cost checkpoint helpers."""

from __future__ import annotations

import logging
import pathlib
import threading
from typing import Any, Dict

from ouroboros.task_results import STATUS_COMPLETED, load_task_result, write_task_result
from ouroboros.utils import append_jsonl, utc_now_iso

log = logging.getLogger(__name__)

POST_TASK_SYNTHESIS_LOCK = threading.Lock()
POST_TASK_SYNTHESIS_INFLIGHT: set[tuple[str, str]] = set()


def is_root_post_task(task: Dict[str, Any]) -> bool:
    """Structural root test for the single global post-task synthesis authority."""
    meta = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    task_id = str(task.get("id") or task.get("task_id") or "")
    if str(task.get("delegation_role") or meta.get("delegation_role") or "").lower() == "subagent":
        return False
    root_id = str(task.get("root_task_id") or meta.get("root_task_id") or "")
    if root_id:
        return bool(task_id and root_id == task_id)
    return not bool(str(task.get("parent_task_id") or meta.get("parent_task_id") or "").strip())


def root_checkpoint_roots(env: Any, task: Dict[str, Any]) -> list[pathlib.Path]:
    """Return the one durable phase authority (compatibility list shape)."""
    raw = task.get("budget_drive_root") or getattr(env, "drive_root", None)
    if not raw:
        return []
    try:
        return [pathlib.Path(raw).resolve(strict=False)]
    except (TypeError, OSError, ValueError):
        return []


def set_root_post_task_checkpoint(env: Any, task: Dict[str, Any], status: str) -> None:
    """Merge the phase marker in the canonical budget-drive task result."""
    if not is_root_post_task(task):
        return
    task_id = str(task.get("id") or task.get("task_id") or "")
    if not task_id:
        return
    requested_status = str(status)
    roots = root_checkpoint_roots(env, task)
    if not roots:
        return
    authority_root = roots[0]
    finalized_event: Dict[str, Any] | None = None
    # The proactive namer can settle concurrently with post-task synthesis. A
    # shared critical section makes its refresh and the final snapshot linear.
    with POST_TASK_SYNTHESIS_LOCK:
        existing = load_task_result(authority_root, task_id) or {}
        checkpoint = existing.get("root_phase_checkpoint")
        saved = str(checkpoint.get("post_task_synthesis") or "") if isinstance(checkpoint, dict) else ""
        effective_status = saved if requested_status == "refresh" and saved else requested_status
        cost_fields: Dict[str, Any] = {"cost_final": False, "cost_with_children_partial": True}
        if effective_status in {"completed", "degraded"}:
            try:
                from ouroboros.usage_accounting import usage_breakdown
                from supervisor.state import reconstruct_task_cost

                cost_fields.update(reconstruct_task_cost(task_id, fields=True, drive_root=authority_root))
                subtree = usage_breakdown(authority_root, root_task_id=task_id)
                subtree_final = bool(subtree.get("cost_final"))
                cost_fields.update({
                    "cost_usd_with_children": round(float(subtree.get("accounted_usd") or 0.0), 6),
                    "cost_with_children_partial": not subtree_final,
                    "cost_final": bool(cost_fields.get("cost_final") and subtree_final),
                })
            except Exception:
                log.error("Failed to refresh final root cost projection for %s", task_id, exc_info=True)
                cost_fields.update({
                    "cost_accounting_status": "unavailable",
                    "cost_accounting_error": "ledger_unavailable",
                    "cost_usd": None,
                    "cost_usd_with_children": None,
                })
        try:
            checkpoint = dict(checkpoint) if isinstance(checkpoint, dict) else {
                "phase": "task_acceptance", "status": "not_required", "pass_index": 0,
            }
            checkpoint["post_task_synthesis"] = effective_status
            write_task_result(
                authority_root,
                task_id,
                str(existing.get("status") or task.get("status") or STATUS_COMPLETED),
                root_task_id=str(task.get("root_task_id") or task_id),
                parent_task_id=task.get("parent_task_id"),
                budget_drive_root=str(authority_root),
                child_drive_root=task.get("child_drive_root") or task.get("drive_root"),
                project_id=str(task.get("project_id") or ""),
                root_phase_checkpoint=checkpoint,
                **cost_fields,
            )
        except Exception:
            log.debug("Failed to update root post-task checkpoint", exc_info=True)
        if effective_status in {"completed", "degraded"}:
            finalized_event = {
                "type": "task_cost_finalized",
                "ts": utc_now_iso(),
                "task_id": task_id,
                "root_task_id": task_id,
                "post_task_status": effective_status,
                **cost_fields,
            }
    if finalized_event is not None:
        try:
            append_jsonl(authority_root / "logs" / "events.jsonl", finalized_event)
            from supervisor.message_bus import get_bridge

            get_bridge().push_log(finalized_event)
        except Exception:
            log.warning("Failed to publish finalized task cost for %s", task_id, exc_info=True)


def root_post_task_already_completed(env: Any, task: Dict[str, Any]) -> bool:
    if not is_root_post_task(task):
        return False
    task_id = str(task.get("id") or task.get("task_id") or "")
    roots = root_checkpoint_roots(env, task)
    existing = load_task_result(roots[0], task_id) if roots and task_id else None
    checkpoint = existing.get("root_phase_checkpoint") if isinstance(existing, dict) else None
    return bool(
        isinstance(checkpoint, dict)
        and checkpoint.get("post_task_synthesis") in {"completed", "degraded"}
    )
