"""D#7 soft join-ledger tools (extracted from control.py to keep it under the module
size gate). The parent's explicit, structured (P5 — not parsed-from-prose) controls for
not orphaning spawned subagent children:

  - peek_task: inspect a child's status / latest beacons / result tail (a PURE READ —
    makes no finalization decision and does not alter the change-based handoff reminder).
  - discard_child_result: explicitly abandon a child's result (stamps a durable
    parent_decision the pre-finalization reminder honors), lineage-gated to OWN children.

The shared lineage/ledger helpers (_status_drive_root, _is_own_child,
_record_child_decision_beacon) and the cancel_task handler (moved here from control.py,
upgraded with a recorded reason + lineage gate) live here too.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict

from ouroboros.task_results import validate_task_id, write_task_result
from ouroboros.task_status import load_effective_task_result
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger("ouroboros.tools.join_ledger")


def _record_child_decision_beacon(ctx: ToolContext, task_id: str, text: str) -> None:
    """Record a parent coordination decision about a child on the task-tree ledger
    (D#7) so the decision is durable + visible across the tree. Best-effort."""
    try:
        from ouroboros.tools.task_tree import tree_root_id

        rid = tree_root_id(ctx)
        if not rid:
            return
        from ouroboros.task_tree_ledger import tree_ledger_append

        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        role = str(meta.get("role") or getattr(ctx, "role", "") or "")
        tree_ledger_append(rid, "decision", text, task_id=str(task_id or ""), role=role)
    except Exception:
        log.debug("Failed to record child decision beacon for %s", task_id, exc_info=True)


def _status_drive_root(ctx: ToolContext) -> Path:
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    return Path(str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root))


def _is_own_child(ctx: ToolContext, status_drive_root: Path, tid: str) -> bool:
    """True if ``tid`` is a DIRECT child of the CURRENT task (D#7 safety): a parent
    decision (discard/cancel parent_decision stamp) may only touch the caller's OWN
    children, never an unrelated parent's join ledger. Fail-CLOSED — any error returns
    False so the stamp is withheld rather than wrongly applied."""
    try:
        from ouroboros.task_status import find_child_tasks

        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        my_id = str(getattr(ctx, "task_id", "") or meta.get("task_id") or "")
        if not my_id or not tid:
            return False
        children = find_child_tasks(
            Path(status_drive_root), parent_task_id=my_id, root_task_id="", exclude_task_id=my_id
        )
        return any(str(c.get("task_id") or c.get("id") or "") == tid for c in children)
    except Exception:
        return False


def _clip(text: object, limit: int, *, tail: bool = False) -> str:
    """Truncate to ``limit`` chars with an EXPLICIT omission marker so a peek never
    silently drops cognitive content (P1 — no silent horizon cut; the agent can then
    get_task_result the full body if it needs the omitted part)."""
    s = str(text or "")
    if len(s) <= limit:
        return s
    omitted = len(s) - limit
    if tail:
        return f"…(+{omitted} earlier chars omitted — get_task_result for the full body)\n{s[-limit:]}"
    return f"{s[:limit]}…(+{omitted} more chars omitted)"


def _peek_task(ctx: ToolContext, task_id: str, view: str = "summary") -> str:
    """Read a child's CURRENT status + latest coordination beacons + result tail (D#7 — the
    parent's 'see intermediate findings' right). A PURE READ: it changes no state. The
    pre-finalization handoff reminder is CHANGE-BASED (it re-surfaces whenever a child's
    status/result changes and is suppressed only by an explicit discard_child_result /
    cancel_task, or by being unchanged since last shown) — peeking neither suppresses nor
    re-triggers it. view: summary | partials | tail."""
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (peek_task): {exc}"
    v = str(view or "summary").strip().lower()
    status_drive_root = _status_drive_root(ctx)
    data = load_effective_task_result(status_drive_root, tid) or {}
    status = str(data.get("status") or "unknown")
    cost = data.get("cost_usd", 0) or 0
    parts = [f"Task {tid} [{status}] cost=${float(cost):.2f} (peek — NOT absorbed)"]
    # Latest beacons this child posted to the shared ledger (partial_finding / blocker /
    # question / milestone), newest last.
    try:
        from ouroboros.tools.task_tree import tree_root_id
        from ouroboros.task_tree_ledger import tree_ledger_rows

        rid = tree_root_id(ctx)
        if rid:
            rows = [r for r in tree_ledger_rows(rid) if str(r.get("task_id") or "") == tid]
            if v in ("partials", "summary"):
                beacons = [r for r in rows if str(r.get("kind")) in ("partial_finding", "blocker", "question", "milestone", "interface_contract")]
                if len(beacons) > 8:
                    parts.append(f"  …(+{len(beacons) - 8} older beacon(s) omitted; showing newest 8)")
                for r in beacons[-8:]:
                    parts.append(f"  • [{r.get('kind')}] {_clip(r.get('text'), 400)}")
    except Exception:
        log.debug("peek_task ledger read failed for %s", tid, exc_info=True)
    if v in ("tail", "summary"):
        result = str(data.get("result") or "")
        if result:
            parts.append(f"[PEEK_RESULT_TAIL]\n{_clip(result, 1200, tail=True)}\n[/PEEK_RESULT_TAIL]")
    trace = str(data.get("trace_summary") or "")
    if trace and v == "tail":
        parts.append(f"[PEEK_TRACE]\n{_clip(trace, 800)}\n[/PEEK_TRACE]")
    return "\n".join(parts)


def _discard_child_result(ctx: ToolContext, task_id: str, reason: str) -> str:
    """Explicitly decide to ABANDON a child's result (D#7). This is the EXPLICIT,
    structured signal (P5 — not a parsed-from-prose phrase) that lets the parent finalize
    without that child: it stamps ``parent_decision="discarded"`` on the child result so
    the pre-finalization handoff reminder stops surfacing it, and records the reason on the
    shared task-tree ledger. A reason is REQUIRED so the abandon is an auditable judgment,
    not a silent loss. Lineage-gated to the caller's OWN children."""
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (discard_child_result): {exc}"
    reason_text = _clip(" ".join(str(reason or "").split()), 500)
    if not reason_text:
        return "⚠️ TOOL_ARG_ERROR (discard_child_result): a non-empty reason is required."
    status_drive_root = _status_drive_root(ctx)
    # D#7 safety: a parent may abandon only its OWN child's result — never stamp
    # parent_decision on an unrelated task and hide it from its real parent's reminder.
    if not _is_own_child(ctx, status_drive_root, tid):
        return f"⚠️ discard_child_result: {tid} is not a child of this task — refusing to discard."
    data = load_effective_task_result(status_drive_root, tid)
    if not data:
        return f"Task {tid}: unknown or not yet registered — nothing to discard."
    try:
        # Merge-write preserving the child's current status (write_task_result's monotonic
        # guard keeps a terminal/cancel status), adding the explicit parent decision.
        write_task_result(
            status_drive_root, tid, str(data.get("status") or "running"),
            parent_decision="discarded",
            parent_decision_reason=reason_text,
        )
    except Exception:
        log.debug("Failed to stamp discard decision on %s", tid, exc_info=True)
        return f"⚠️ discard_child_result: failed to record decision for {tid}."
    _record_child_decision_beacon(ctx, tid, f"discarded result of child {tid}: {reason_text}")
    return f"Discarded child result {tid} (reason: {reason_text}). It will not block finalization."


def _override_delegation_constraint(ctx: ToolContext, constraint_id: str, reason: str) -> str:
    """Explicitly override an unresolved delegation constraint in this task tree."""

    cid = " ".join(str(constraint_id or "").split())
    if not cid:
        return "⚠️ TOOL_ARG_ERROR (override_delegation_constraint): constraint_id is required."
    reason_text = _clip(" ".join(str(reason or "").split()), 500)
    if not reason_text:
        return "⚠️ TOOL_ARG_ERROR (override_delegation_constraint): a non-empty reason is required."
    try:
        from ouroboros.tools.task_tree import tree_root_id
        from ouroboros.task_tree_ledger import open_delegation_constraints, tree_ledger_append

        rid = tree_root_id(ctx)
        if not rid:
            return "⚠️ override_delegation_constraint: no task-tree scope."
        open_rows = open_delegation_constraints(rid)
        target_row = next((
            row for row in open_rows
            if isinstance(row.get("payload"), dict)
            and str(row["payload"].get("constraint_id") or "") == cid
        ), None)
        if target_row is None:
            return f"⚠️ override_delegation_constraint: constraint {cid!r} is not open in this task tree."
        emitter_task_id = str(target_row.get("task_id") or "").strip()
        if emitter_task_id:
            status_drive_root = _status_drive_root(ctx)
            if not _is_own_child(ctx, status_drive_root, emitter_task_id):
                return (
                    "⚠️ override_delegation_constraint: only the parent of the task that raised "
                    f"constraint {cid!r} may override it."
                )
        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        role = str(meta.get("role") or getattr(ctx, "role", "") or "")
        return tree_ledger_append(
            rid,
            "decision",
            f"overrode delegation constraint {cid}: {reason_text}",
            task_id=str(getattr(ctx, "task_id", "") or ""),
            role=role,
            allow_constraint_override=True,
            payload={
                "constraint_id": cid,
                "decision": "overridden",
                "reason": reason_text,
                "parent_task_id": str(getattr(ctx, "task_id", "") or ""),
            },
        )
    except Exception:
        log.debug("Failed to override delegation constraint %s", cid, exc_info=True)
        return f"⚠️ override_delegation_constraint: failed to record override for {cid}."


def _cancel_task(ctx: ToolContext, task_id: str, reason: str = "") -> str:
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (cancel_task): {exc}"
    reason_text = _clip(" ".join(str(reason or "").split()), 500)
    status_drive_root = _status_drive_root(ctx)
    # Only stamp the join-ledger parent_decision (+ post to the tree ledger) when the
    # target is THIS task's own child — a cancel must not rewrite an unrelated task's
    # parent_decision and hide it from its real parent's reminder (D#7 safety).
    own = _is_own_child(ctx, status_drive_root, tid)
    # Subagent isolation: a CONSTRAINED caller (workspace/subagent task that can schedule
    # children) may cancel ONLY its own children — never an arbitrary task id. The
    # owner-level orchestrator (self_modification / operator_control) keeps general cancel.
    if not own:
        try:
            from ouroboros.tool_access import active_tool_profile

            if active_tool_profile(ctx) in (
                "workspace_task", "external_workspace_task", "acting_subagent", "local_readonly_subagent",
            ):
                return f"⚠️ cancel_task: {tid} is not a child of this task — a constrained task may only cancel its own children."
        except Exception:
            log.debug("cancel_task lineage profile check failed for %s", tid, exc_info=True)
    # Latch a cancel-intent status so the parent's find_child_tasks view treats the child
    # as terminal immediately (stops the handoff reminder re-injecting "still scheduled").
    try:
        from ouroboros.task_results import STATUS_CANCEL_REQUESTED

        fields: Dict[str, Any] = {
            "result": f"Cancellation requested by agent; awaiting supervisor teardown.{(' Reason: ' + reason_text) if reason_text else ''}",
        }
        if own:
            fields["parent_decision"] = "cancelled"
            fields["parent_decision_reason"] = reason_text
        write_task_result(status_drive_root, tid, STATUS_CANCEL_REQUESTED, **fields)
    except Exception:
        log.debug("Failed to latch cancel_requested status for %s", tid, exc_info=True)
    if own:
        _record_child_decision_beacon(ctx, tid, f"cancelled child {tid}" + (f": {reason_text}" if reason_text else ""))
    # Emit live so the supervisor processes the cancellation within one loop tick.
    from ouroboros.tools.control import _emit_control_event

    emitted = _emit_control_event(ctx, {"type": "cancel_task", "task_id": tid, "reason": reason_text, "ts": utc_now_iso()})
    note = " (live)" if emitted == "live" else " (deferred to round end)"
    return f"Cancel requested: {tid}{(' — ' + reason_text) if reason_text else ''}{note}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry("cancel_task", {
            "name": "cancel_task",
            "description": "Stop a running/scheduled child task by ID. Give a short reason — it is "
                           "recorded on the shared task-tree ledger and the child's result, so a "
                           "stopped child is an auditable decision, not a silent disappearance.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "default": "", "description": "Why you are stopping it (recorded for the tree + review)."},
            }, "required": ["task_id"]},
        }, _cancel_task),
        ToolEntry("peek_task", {
            "name": "peek_task",
            "description": "Look at a child task's CURRENT status, its latest coordination beacons "
                           "(partial_finding/blocker/question/milestone) and a tail of its result — "
                           "a PURE READ. Use this to check intermediate findings or decide whether to keep "
                           "waiting / steer / cancel, without committing to a finalization decision. It "
                           "changes no state: the pre-finalization reminder is change-based and is cleared "
                           "only by discard_child_result / cancel_task, not by reading.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "view": {"type": "string", "enum": ["summary", "partials", "tail"], "default": "summary",
                         "description": "summary = status+beacons+result tail; partials = beacons only; tail = result+trace tail."},
            }, "required": ["task_id"]},
        }, _peek_task),
        ToolEntry("discard_child_result", {
            "name": "discard_child_result",
            "description": "Explicitly decide to finalize WITHOUT a child's result (abandon it on purpose). "
                           "Requires a reason. This is the structured way to drop a child you no longer need "
                           "so it stops being flagged before you finalize — use it instead of just ignoring "
                           "the child, so the abandon is a logged judgment rather than a silent loss.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why this child's result is not needed."},
            }, "required": ["task_id", "reason"]},
        }, _discard_child_result),
        ToolEntry("override_delegation_constraint", {
            "name": "override_delegation_constraint",
            "description": "Explicitly override an unresolved delegation_constraint in this task tree. Requires a reason; records an append-only decision row so a future schedule_subagent call may proceed audibly.",
            "parameters": {"type": "object", "properties": {
                "constraint_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why overriding this constraint is correct."},
            }, "required": ["constraint_id", "reason"]},
        }, _override_delegation_constraint),
    ]
