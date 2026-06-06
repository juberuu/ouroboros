"""Control tools: restart, timeout settings, scheduling, review, chat history, model switching."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.config import apply_settings_to_env, get_max_subagent_depth, load_settings, save_settings
from ouroboros.headless import prepare_task_drive, task_state_dir
from ouroboros.contracts.task_contract import build_task_contract, normalize_allowed_resources
from ouroboros.outcomes import normalize_outcome_axes, public_task_result
from ouroboros.task_results import (
    STATUS_COMPLETED,
    STATUS_REJECTED_DUPLICATE,
    STATUS_REQUESTED,
    validate_task_id,
    write_task_result,
)
from ouroboros.task_status import load_effective_task_result, wait_for_effective_tasks
from ouroboros.subagents import (
    build_subagent_envelope,
    compact_task_group,
    expand_subagent_lane_slots,
    normalize_subagent_model_lane,
)
from ouroboros.tool_capabilities import ACTING_SUBAGENT_MODE, LOCAL_READONLY_SUBAGENT_MODE
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import atomic_write_json, utc_now_iso, run_cmd

log = logging.getLogger(__name__)

VALID_SUBTASK_MEMORY_MODES = frozenset({"forked", "empty"})


def _subtask_outcome_summary(data: Dict[str, Any]) -> str:
    ledger = data.get("verification_ledger") if isinstance(data.get("verification_ledger"), dict) else {}
    summary: Dict[str, Any] = {
        "outcome_axes": normalize_outcome_axes(data),
    }
    if isinstance(data.get("task_contract"), dict):
        summary["task_contract"] = data.get("task_contract")
    if isinstance(data.get("artifact_bundle"), dict):
        summary["artifact_bundle"] = data.get("artifact_bundle")
    if ledger:
        summary["verification_ledger"] = {
            "schema_version": ledger.get("schema_version"),
            "summary": ledger.get("summary") if isinstance(ledger.get("summary"), dict) else {},
            "entry_count": len(ledger.get("entries") or []) if isinstance(ledger.get("entries"), list) else 0,
        }
    return json.dumps(summary, ensure_ascii=False, indent=2, default=str)


def _emit_control_event(ctx: ToolContext, evt: Dict[str, Any]) -> str:
    """Emit a control event live when possible, preserving legacy fallback."""
    event_queue = getattr(ctx, "event_queue", None)
    if event_queue is not None:
        try:
            event_queue.put_nowait(dict(evt))
            return "live"
        except (AttributeError, queue.Full):
            pass
        except Exception:
            log.warning("Live control event emission failed; falling back to pending_events", exc_info=True)
    ctx.pending_events.append(evt)
    return "deferred"


def _evolution_restart_block_reason(ctx: ToolContext) -> str:
    if str(ctx.current_task_type or "") != "evolution":
        return ""
    try:
        status = run_cmd(["git", "status", "--porcelain"], cwd=ctx.repo_dir).strip()
        head = run_cmd(["git", "rev-parse", "HEAD"], cwd=ctx.repo_dir).strip()
    except Exception as exc:
        return f"could not verify local git durability: {exc}"
    reviewed_sha = str(getattr(ctx, "last_reviewed_commit_sha", "") or "").strip()
    if reviewed_sha and reviewed_sha == head and not status:
        return ""
    if not reviewed_sha and not status:
        return ""
    if reviewed_sha and reviewed_sha != head:
        return "HEAD changed after the last reviewed local commit"
    return "commit_reviewed must create a local reviewed commit before evolution restart"


def _request_restart(ctx: ToolContext, reason: str) -> str:
    block_reason = _evolution_restart_block_reason(ctx)
    if block_reason:
        return f"⚠️ RESTART_BLOCKED: in evolution mode, {block_reason}."
    # Persist expected ref for post-restart verification.
    try:
        sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=ctx.repo_dir)
        branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ctx.repo_dir)
        verify_path = ctx.drive_path("state") / "pending_restart_verify.json"
        atomic_write_json(verify_path, {
            "ts": utc_now_iso(), "expected_sha": sha,
            "expected_branch": branch, "reason": reason,
        })
        if str(ctx.current_task_type or "") == "evolution":
            try:
                from supervisor.queue import update_evolution_transaction

                update_evolution_transaction(
                    str(ctx.task_id or ""),
                    restart_decision="requested",
                    restart_required=True,
                    restart_requested_at=utc_now_iso(),
                    restart_expected_sha=str(sha or "").strip(),
                )
            except Exception:
                log.debug("Failed to record evolution restart request", exc_info=True)
    except Exception:
        log.debug("Failed to read VERSION file or git ref for restart verification", exc_info=True)
        pass
    ctx.pending_restart_reason = str(reason or "").strip() or "agent_requested_restart"
    ctx.last_push_succeeded = False
    ctx.last_reviewed_commit_sha = ""
    return f"Restart requested: {reason}"


def _set_tool_timeout(ctx: ToolContext, seconds: int) -> str:
    """Persist timeout while pinning owner-only runtime mode to the live env."""
    try:
        timeout_sec = int(seconds)
    except (TypeError, ValueError):
        return f"⚠️ TOOL_ARG_ERROR (set_tool_timeout): invalid seconds={seconds!r}"
    if timeout_sec < 1:
        return "⚠️ TOOL_ARG_ERROR (set_tool_timeout): seconds must be >= 1"

    settings = load_settings()
    settings["OUROBOROS_TOOL_TIMEOUT_SEC"] = timeout_sec
    settings["OUROBOROS_RUNTIME_MODE"] = os.environ.get("OUROBOROS_RUNTIME_MODE", "advanced")
    save_settings(settings)
    apply_settings_to_env(settings)
    return f"OK: OUROBOROS_TOOL_TIMEOUT_SEC set to {timeout_sec}s and applied immediately."


def _promote_to_stable(ctx: ToolContext, reason: str) -> str:
    ctx.pending_events.append({"type": "promote_to_stable", "reason": reason, "ts": utc_now_iso()})
    return f"Promote to stable requested: {reason}"


def _build_acting_constraint(
    *,
    write_surface: str,
    write_root: str,
    protected_paths_grant: bool,
    external_tool_grants: Any,
    parent_workspace_root: str,
):
    """Validate a mutative-subagent request; return its constraint dict, or an
    error string for the LLM (which can then fall back to a read-only subagent).

    The toggle/surface checks here give the caller immediate feedback. The
    supervisor is the authoritative gate and provisions the self_worktree
    (filling write_root/base_sha) before the child runs.
    """
    from ouroboros.config import get_allow_mutative_subagents
    from ouroboros.contracts.task_constraint import VALID_WRITE_SURFACES

    if write_surface not in VALID_WRITE_SURFACES:
        allowed = ", ".join(sorted(VALID_WRITE_SURFACES))
        return (
            "⚠️ TOOL_ARG_ERROR (schedule_subagent): write_surface must be one of "
            f"{allowed} (or omit it for a read-only subagent)."
        )
    if not get_allow_mutative_subagents():
        return (
            "⚠️ MUTATIVE_SUBAGENTS_DISABLED: acting (mutative) subagents are turned "
            "off in this runtime mode. Schedule a read-only subagent (omit "
            "write_surface), or have the owner enable OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS "
            "(default ON in advanced/pro, OFF in light)."
        )
    grants: List[str] = []
    if isinstance(external_tool_grants, (list, tuple)):
        grants = [str(g).strip() for g in external_tool_grants if str(g).strip()]
    resolved_write_root = str(write_root or "").strip()
    if write_surface == "external_workspace" and not resolved_write_root:
        resolved_write_root = str(parent_workspace_root or "").strip()
    if write_surface == "external_workspace" and not resolved_write_root:
        return (
            "⚠️ TOOL_ARG_ERROR (schedule_subagent): write_surface=external_workspace "
            "requires write_root (the external project directory) or a parent workspace."
        )
    return {
        "mode": ACTING_SUBAGENT_MODE,
        "surface": write_surface,
        "write_root": resolved_write_root,
        "protected_paths_grant": protected_paths_grant,
        "external_tool_grants": grants,
        "parent_only_commit": True,
        "return_kind": "workspace_patch",
        "allow_enable": False,
        "allow_review": False,
    }


def _select_subagent_constraint(write_surface, write_root, protected_paths_grant, external_tool_grants, parent_workspace_root, caller_readonly=False):
    """Read-only default (no surface), a validated acting constraint, or an error string."""
    if not write_surface:
        return {"mode": LOCAL_READONLY_SUBAGENT_MODE, "allow_enable": False, "allow_review": False}
    if caller_readonly:
        # A read-only subagent may delegate read-only children only — never spawn an acting one.
        return (
            "⚠️ MUTATIVE_SUBAGENTS_DISABLED: a read-only subagent cannot spawn a mutative (acting) "
            "child. Only the root agent, workspace tasks, or acting subagents may pass write_surface; "
            "schedule a read-only child instead."
        )
    return _build_acting_constraint(
        write_surface=write_surface,
        write_root=write_root,
        protected_paths_grant=protected_paths_grant,
        external_tool_grants=external_tool_grants,
        parent_workspace_root=parent_workspace_root,
    )


def _schedule_task(
    ctx: ToolContext,
    objective: str = "",
    expected_output: str = "",
    role: str = "",
    context: str = "",
    constraints: str = "",
    memory_mode: str = "forked",
    model_lane: str = "auto",
    write_surface: str = "",
    write_root: str = "",
    protected_paths_grant: bool = False,
    external_tool_grants: Any = None,
    **legacy_or_unknown: Any,
) -> str:
    if legacy_or_unknown:
        bad = ", ".join(sorted(str(key) for key in legacy_or_unknown.keys()))
        return (
            "⚠️ TOOL_ARG_ERROR (schedule_subagent): unsupported argument(s): "
            f"{bad}. Use the v6 strict schema: objective, expected_output, "
            "optional role/context/constraints/memory_mode/model_lane and (for "
            "mutative children) write_surface/write_root/protected_paths_grant/"
            "external_tool_grants."
        )
    objective = str(objective or "").strip()
    expected_output = str(expected_output or "").strip()
    role = str(role or "researcher").strip() or "researcher"
    context = str(context or "").strip()
    constraints = str(constraints or "").strip()
    memory_mode = str(memory_mode or "forked").strip().lower()
    try:
        requested_model_lane = normalize_subagent_model_lane(model_lane)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (schedule_subagent): {exc}."
    if not objective:
        return "⚠️ TOOL_ARG_ERROR (schedule_subagent): objective is required."
    if not expected_output:
        return "⚠️ TOOL_ARG_ERROR (schedule_subagent): expected_output is required."
    if memory_mode not in VALID_SUBTASK_MEMORY_MODES:
        allowed = ", ".join(sorted(VALID_SUBTASK_MEMORY_MODES))
        return (
            f"⚠️ TOOL_ARG_ERROR (schedule_subagent): memory_mode must be one of: {allowed}. "
            "memory_mode=shared is disabled for live local subagents until a sanitized shared-context mode exists."
        )

    try:
        current_depth = int(getattr(ctx, 'task_depth', 0) or 0)
    except (TypeError, ValueError):
        current_depth = 0
    new_depth = current_depth + 1
    max_depth = get_max_subagent_depth()
    if new_depth > max_depth:
        return f"ERROR: Subtask depth limit ({max_depth}) exceeded. Simplify your approach."

    if getattr(ctx, 'is_direct_chat', False):
        from ouroboros.utils import append_jsonl
        try:
            append_jsonl(ctx.drive_logs() / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "schedule_task_from_direct_chat",
                "description": objective[:200],
                "warning": "schedule_subagent called from direct chat context — potential duplicate work",
            })
        except Exception:
            pass

    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    parent_contract = (
        getattr(ctx, "task_contract", {})
        if isinstance(getattr(ctx, "task_contract", {}), dict)
        else metadata.get("task_contract") if isinstance(metadata.get("task_contract"), dict)
        else {}
    )
    current_task_id = str(getattr(ctx, "task_id", "") or "")
    parent_task_id = str(current_task_id or metadata.get("parent_task_id") or "").strip()
    root_task_id_seed = str(metadata.get("root_task_id") or current_task_id or "").strip()
    session_id = str(metadata.get("session_id") or "")
    try:
        current_chat_id = int(getattr(ctx, "current_chat_id", None) or 0)
    except (TypeError, ValueError):
        current_chat_id = 0
    budget_drive_root = str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root)
    status_drive_root = Path(budget_drive_root)
    workspace_root = str(getattr(ctx, "workspace_root", "") or metadata.get("workspace_root") or "").strip()
    workspace_mode = str(getattr(ctx, "workspace_mode", "") or metadata.get("workspace_mode") or "").strip()
    requested_surface = str(write_surface or "").strip().lower()
    from ouroboros.tool_access import active_tool_profile
    task_constraint = _select_subagent_constraint(
        requested_surface, write_root, protected_paths_grant, external_tool_grants, workspace_root,
        caller_readonly=(active_tool_profile(ctx) == "local_readonly_subagent"))
    if isinstance(task_constraint, str):
        return task_constraint
    allowed_resources = normalize_allowed_resources(
        (parent_contract.get("allowed_resources") if isinstance(parent_contract, dict) else {})
        or metadata.get("allowed_resources")
        or {}
    )
    executor_ref = {}
    executor_accessor = getattr(ctx, "workspace_executor_ref", None)
    if callable(executor_accessor):
        try:
            candidate = executor_accessor()
            if isinstance(candidate, dict) and candidate:
                executor_ref = dict(candidate)
        except Exception:
            executor_ref = {}
    lane_slots = expand_subagent_lane_slots(requested_model_lane, depth=new_depth)
    if not lane_slots:
        return "⚠️ SUBTASK_STATUS_ERROR: no subagent lane slots resolved; subagent was not scheduled."
    slot_tasks = [(uuid.uuid4().hex[:8], slot) for slot in lane_slots]
    task_ids: List[str] = [task_id for task_id, _slot in slot_tasks]
    emitted_modes: List[str] = []
    task_group_id = (
        f"subagents-{uuid.uuid4().hex[:8]}"
        if requested_model_lane in {"review", "scope"} or len(lane_slots) > 1
        else ""
    )
    task_group = compact_task_group(
        group_id=task_group_id,
        task_ids=task_ids,
        requested_lane=requested_model_lane,
        parent_task_id=parent_task_id,
        root_task_id=root_task_id_seed,
        role=role,
    ) if task_group_id else {}
    child_drives: Dict[str, Path] = {}
    if memory_mode in {"forked", "empty"}:
        for tid, _slot in slot_tasks:
            try:
                child_drives[tid] = prepare_task_drive(status_drive_root, tid, memory_mode)
            except Exception as exc:
                for child_drive in child_drives.values():
                    shutil.rmtree(child_drive, ignore_errors=True)
                for cleanup_tid in task_ids:
                    shutil.rmtree(task_state_dir(status_drive_root, cleanup_tid), ignore_errors=True)
                log.warning("Failed to prepare child drive for subtask %s", tid, exc_info=True)
                return f"⚠️ SUBTASK_DRIVE_ERROR: failed to prepare {memory_mode} child drive: {exc}"

    events_to_emit: List[Dict[str, Any]] = []
    for tid, slot in slot_tasks:
        root_task_id = root_task_id_seed or tid
        slot_role = role
        if slot.slot_count > 1:
            slot_role = f"{role}:slot-{slot.slot_index + 1}"
        child_drive = child_drives.get(tid)

        child_contract = build_task_contract({
            "id": tid,
            "type": "task",
            "description": objective,
            "objective": objective,
            "expected_output": expected_output,
            "constraints": constraints,
            "workspace_root": workspace_root,
            "workspace_mode": workspace_mode,
            "allowed_resources": allowed_resources,
            "deadline_at": parent_contract.get("deadline_at") if isinstance(parent_contract, dict) else "",
            "parent_task_id": parent_task_id,
            "root_task_id": root_task_id,
            "session_id": session_id,
            "delegation_role": "subagent",
            "metadata": {
                "task_contract": {
                    **parent_contract,
                    "source": "parent_delegation",
                    "objective": objective,
                    "expected_output": expected_output,
                    "constraints": constraints,
                } if isinstance(parent_contract, dict) else {},
            },
        })
        envelope = build_subagent_envelope(
            task_id=tid,
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
            task_group_id=task_group_id,
            depth=new_depth,
            role=slot_role,
            requested_lane=slot.requested_lane,
            effective_lane=slot.effective_lane,
            model=slot.model,
            status=STATUS_REQUESTED,
        )
        evt = {
            "type": "schedule_subagent",
            "description": objective,
            "objective": objective,
            "expected_output": expected_output,
            "constraints": constraints,
            "role": slot_role,
            "task_id": tid,
            "depth": new_depth,
            "ts": utc_now_iso(),
            "root_task_id": root_task_id,
            "session_id": session_id,
            "actor_id": f"subagent:{slot_role}",
            "delegation_role": "subagent",
            "memory_mode": memory_mode,
            "budget_drive_root": budget_drive_root,
            "task_constraint": task_constraint,
            "write_surface": requested_surface,
            "task_contract": child_contract,
            "allowed_resources": allowed_resources,
            "model_lane": slot.requested_lane,
            "requested_model_lane": slot.requested_lane,
            "effective_model_lane": slot.effective_lane,
            "model": slot.model,
            "use_local_model": slot.use_local_model,
            "task_group_id": task_group_id,
            "task_group": task_group,
            "subagent_envelope": envelope,
        }
        if current_chat_id:
            evt["chat_id"] = current_chat_id
        if child_drive is not None:
            evt["drive_root"] = str(child_drive)
            evt["child_drive_root"] = str(child_drive)
        if workspace_root:
            evt["workspace_root"] = workspace_root
        if workspace_mode:
            evt["workspace_mode"] = workspace_mode
        if executor_ref:
            evt["executor_ref"] = executor_ref
            evt["metadata"] = {**(evt.get("metadata") if isinstance(evt.get("metadata"), dict) else {}), "executor_ref": executor_ref}
        if context:
            evt["context"] = context
        if parent_task_id:
            evt["parent_task_id"] = parent_task_id
        try:
            write_task_result(
                status_drive_root,
                tid,
                STATUS_REQUESTED,
                parent_task_id=parent_task_id or None,
                root_task_id=root_task_id,
                session_id=session_id,
                actor_id=f"subagent:{slot_role}",
                delegation_role="subagent",
                role=slot_role,
                description=objective,
                objective=objective,
                expected_output=expected_output,
                constraints=constraints,
                context=context,
                workspace_root=workspace_root,
                workspace_mode=workspace_mode,
                executor_ref=executor_ref,
                allowed_resources=allowed_resources,
                task_contract=child_contract,
                chat_id=current_chat_id or None,
                memory_mode=memory_mode,
                drive_root=str(child_drive) if child_drive is not None else "",
                child_drive_root=str(child_drive) if child_drive is not None else "",
                budget_drive_root=budget_drive_root,
                task_constraint=task_constraint,
                model_lane=slot.requested_lane,
                requested_model_lane=slot.requested_lane,
                effective_model_lane=slot.effective_lane,
                model=slot.model,
                use_local_model=slot.use_local_model,
                task_group_id=task_group_id,
                task_group=task_group,
                subagent_envelope=envelope,
                result="Subagent request queued. Awaiting supervisor acceptance.",
            )
        except Exception:
            log.warning("Failed to persist requested task status for %s", tid, exc_info=True)
            for cleanup_tid in task_ids:
                try:
                    (status_drive_root / "task_results" / f"{cleanup_tid}.json").unlink(missing_ok=True)
                except Exception:
                    pass
            for child_drive in child_drives.values():
                shutil.rmtree(child_drive, ignore_errors=True)
            return f"⚠️ SUBTASK_STATUS_ERROR: failed to persist requested status for {tid}; subagent was not scheduled."
        events_to_emit.append(evt)

    for evt in events_to_emit:
        emitted_modes.append(_emit_control_event(ctx, evt))

    worker_note = " (live queue emission requested)" if any(mode == "live" for mode in emitted_modes) else ""
    try:
        scheduled_records = list(getattr(ctx, "_last_scheduled_subagents", []) or [])
        scheduled_records.append({
            "task_ids": task_ids,
            "task_group_id": task_group_id,
            "requested_model_lane": requested_model_lane,
            "task_group": task_group,
            "objective": objective,
            "role": role,
        })
        setattr(ctx, "_last_scheduled_subagents", scheduled_records)
    except Exception:
        pass
    if len(task_ids) == 1:
        return f"Subagent request queued {task_ids[0]}: {objective}{worker_note}"
    return (
        f"Subagent group queued {task_group_id}: {', '.join(task_ids)} "
        f"(lane={requested_model_lane}, slots={len(task_ids)}){worker_note}"
    )


def _cancel_task(ctx: ToolContext, task_id: str) -> str:
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (cancel_task): {exc}"
    # Latch a cancel-intent status on disk immediately so the parent's own
    # find_child_tasks view treats the child as terminal right away — this stops
    # the handoff-reminder loop from re-injecting "still scheduled" every round,
    # even before the supervisor tears the task down.
    try:
        from ouroboros.task_results import STATUS_CANCEL_REQUESTED
        metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        status_drive_root = Path(str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
        write_task_result(
            status_drive_root, tid, STATUS_CANCEL_REQUESTED,
            result="Cancellation requested by agent; awaiting supervisor teardown.",
        )
    except Exception:
        log.debug("Failed to latch cancel_requested status for %s", tid, exc_info=True)
    # Emit live so the supervisor processes the cancellation within one loop tick
    # instead of at end-of-round. schedule_subagent already emits live; cancel
    # must be symmetric, otherwise a scheduled child looks stuck until the
    # parent's whole turn finishes.
    emitted = _emit_control_event(ctx, {"type": "cancel_task", "task_id": tid, "ts": utc_now_iso()})
    note = " (live)" if emitted == "live" else " (deferred to round end)"
    return f"Cancel requested: {tid}{note}"


def _request_deep_self_review(ctx: ToolContext, reason: str) -> str:
    from ouroboros.deep_self_review import is_review_available
    available, model = is_review_available()
    if not available:
        return (
            "❌ Deep self-review unavailable: configure OUROBOROS_MODEL_DEEP_SELF_REVIEW "
            "and the matching provider API key."
        )
    ctx.pending_events.append({"type": "deep_self_review_request", "reason": reason, "model": model, "ts": utc_now_iso()})
    return f"Deep self-review requested (model: {model}). It will be queued and executed asynchronously."


def _chat_history(ctx: ToolContext, count: int = 100, offset: int = 0, search: str = "") -> str:
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    return mem.chat_history(count=count, offset=offset, search=search)


def _update_scratchpad(ctx: ToolContext, content: str) -> str:
    """LLM-driven scratchpad update — appends a timestamped block (Constitution P5: LLM-first)."""
    if not content or not isinstance(content, str) or len(content.strip()) < 10:
        return (
            "⚠️ REJECTED: content is empty or too short "
            f"(got {type(content).__name__}, len={len(content) if isinstance(content, str) else 'N/A'}). "
            "Scratchpad must have meaningful content (10+ chars). "
            "This likely means the tool call was malformed — check your arguments."
        )
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    mem.ensure_files()
    try:
        block = mem.append_scratchpad_block(
            content,
            source="task",
            metadata={
                "task_id": str(getattr(ctx, "task_id", "") or ""),
                "task_type": str(getattr(ctx, "current_task_type", "") or ""),
                "delegation_role": str((getattr(ctx, "task_metadata", {}) or {}).get("delegation_role", "")) if isinstance(getattr(ctx, "task_metadata", {}), dict) else "",
            },
        )
    except RuntimeError as exc:
        if "LEGACY_SCRATCHPAD_REQUIRES_MANUAL_UPGRADE" in str(exc):
            return f"⚠️ {exc}"
        raise
    return f"OK: scratchpad block appended ({len(content)} chars, ts={block.get('ts', '?')[:16]})"


def _send_user_message(ctx: ToolContext, text: str, reason: str = "") -> str:
    """Send a proactive message to the user (not as reply to a task).

    Use when you have something genuinely worth saying — an insight,
    a question, a status update, or an invitation to collaborate.
    """
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send proactive message."
    if not text or not text.strip():
        return "⚠️ Empty message."

    from ouroboros.utils import append_jsonl
    ctx.pending_events.append({
        "type": "send_message",
        "chat_id": ctx.current_chat_id,
        "text": text,
        "format": "markdown",
        "is_progress": False,
        "ts": utc_now_iso(),
    })
    append_jsonl(ctx.drive_logs() / "events.jsonl", {
        "ts": utc_now_iso(),
        "type": "proactive_message",
        "reason": reason,
        "text_preview": text[:200],
    })
    return "OK: message queued for delivery."


def _update_identity(ctx: ToolContext, content: str) -> str:
    """Update identity manifest (who you are, who you want to become)."""
    if not content or not isinstance(content, str) or len(content.strip()) < 50:
        return (
            "⚠️ REJECTED: content is empty or too short "
            f"(got {type(content).__name__}, len={len(content) if isinstance(content, str) else 'N/A'}). "
            "Identity must be a substantial text (50+ chars). "
            "This likely means the tool call was malformed — check your arguments."
        )
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    mem.ensure_files()

    old_content = ""
    path = ctx.drive_root / "memory" / "identity.md"
    if path.exists():
        try:
            old_content = path.read_text(encoding="utf-8")
        except Exception:
            pass

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    mem.append_identity_journal({
        "ts": utc_now_iso(),
        "task_id": str(getattr(ctx, "task_id", "") or ""),
        "source_type": str((getattr(ctx, "task_metadata", {}) or {}).get("delegation_role", "task")) if isinstance(getattr(ctx, "task_metadata", {}), dict) else "task",
        "old_len": len(old_content),
        "new_len": len(content),
        "old_sha256": sha256(old_content.encode("utf-8")).hexdigest() if old_content else "",
        "new_sha256": sha256(content.encode("utf-8")).hexdigest(),
        "old_content": old_content,
        "new_content": content,
        "old_preview": old_content[:500],
        "new_preview": content[:500],
    })

    result = f"OK: identity updated ({len(content)} chars)"
    old_len = len(old_content)
    if old_len >= 400 and len(content) < old_len * 0.5:
        result += (
            f"\n⚠️ SELF_OVERWRITE_NOTICE: this replaced a {old_len}-char identity with "
            f"{len(content)} chars (>50% shrink). Identity is intentionally mutable (Bible P4), "
            "but full rewrites should be rare and reflect genuine self-creation — not a trivial turn. "
            "Read before writing (P12) and prefer evolving over replacing wholesale."
        )
    return result


def _toggle_evolution(ctx: ToolContext, enabled: bool, objective: str = "") -> str:
    """Toggle evolution mode on/off via supervisor event."""
    if bool(enabled):
        # Reflect the light-mode hard block in the tool's own result so the agent
        # is not told "ON" while the supervisor silently refuses it.
        try:
            from supervisor.queue import evolution_block_reason

            block = evolution_block_reason()
        except Exception:
            block = ""
        if block:
            return block
    ctx.pending_events.append({
        "type": "toggle_evolution",
        "enabled": bool(enabled),
        "objective": str(objective or "").strip(),
        "ts": utc_now_iso(),
    })
    state_str = "ON" if enabled else "OFF"
    return f"OK: evolution mode toggled {state_str}."


def _toggle_consciousness(ctx: ToolContext, action: str = "status") -> str:
    """Control background consciousness: start, stop, or status."""
    ctx.pending_events.append({
        "type": "toggle_consciousness",
        "action": action,
        "ts": utc_now_iso(),
    })
    return f"OK: consciousness '{action}' requested."


def _switch_model(ctx: ToolContext, model: str = "", effort: str = "") -> str:
    """LLM-driven model/effort switch (Constitution P5: LLM-first).

    Stored in ToolContext, applied on the next LLM call in the loop.
    """
    from ouroboros.llm import LLMClient, normalize_reasoning_effort
    available = LLMClient().available_models()
    changes = []

    if model:
        if model not in available:
            return f"⚠️ Unknown model: {model}. Available: {', '.join(available)}"
        ctx.active_model_override = model
        
        import os
        use_local = False
        if model == os.environ.get("OUROBOROS_MODEL") and os.environ.get("USE_LOCAL_MAIN", "").lower() in ("true", "1"):
            use_local = True
        elif model == os.environ.get("OUROBOROS_MODEL_CODE") and os.environ.get("USE_LOCAL_CODE", "").lower() in ("true", "1"):
            use_local = True
        elif model == os.environ.get("OUROBOROS_MODEL_LIGHT") and os.environ.get("USE_LOCAL_LIGHT", "").lower() in ("true", "1"):
            use_local = True
        elif model == os.environ.get("OUROBOROS_MODEL_FALLBACK") and os.environ.get("USE_LOCAL_FALLBACK", "").lower() in ("true", "1"):
            use_local = True
            
        ctx.active_use_local_override = use_local
        changes.append(f"model={model}{' (local)' if use_local else ''}")

    if effort:
        normalized = normalize_reasoning_effort(effort, default="medium")
        ctx.active_effort_override = normalized
        changes.append(f"effort={normalized}")

    if not changes:
        return f"Current available models: {', '.join(available)}. Pass model and/or effort to switch."

    return f"OK: switching to {', '.join(changes)} on next round."


def _get_task_result(ctx: ToolContext, task_id: str) -> str:
    """Read the effective result of a registered subtask."""
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    status_drive_root = Path(str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    data = load_effective_task_result(status_drive_root, task_id)
    if not data:
        return f"Task {task_id}: unknown or not yet registered"
    status = data.get("status", "unknown")
    result = data.get("result", "")
    cost = data.get("cost_usd", 0)
    trace = data.get("trace_summary", "")
    outcome_summary = _subtask_outcome_summary(data)
    if status == STATUS_COMPLETED:
        output = (
            f"Task {task_id} [{status}]: cost=${cost:.2f}\n\n"
            f"[SUBTASK_OUTCOME]\n{outcome_summary}\n[/SUBTASK_OUTCOME]\n\n"
            f"[BEGIN_SUBTASK_OUTPUT]\n{result}\n[END_SUBTASK_OUTPUT]"
        )
    elif status == STATUS_REJECTED_DUPLICATE:
        duplicate_of = str(data.get("duplicate_of") or "?")
        output = (
            f"Task {task_id} [{status}]: duplicate_of={duplicate_of}\n\n"
            f"[SUBTASK_OUTCOME]\n{outcome_summary}\n[/SUBTASK_OUTCOME]\n\n"
            f"{result or f'Task was rejected as a duplicate of {duplicate_of}.'}"
        )
    else:
        output = (
            f"Task {task_id} [{status}]\n\n"
            f"[SUBTASK_OUTCOME]\n{outcome_summary}\n[/SUBTASK_OUTCOME]\n\n"
            f"{result or 'No details available.'}"
        )
    if trace:
        output += f"\n\n[SUBTASK_TRACE]\n{trace}\n[/SUBTASK_TRACE]"
    return output


def _wait_for_task(ctx: ToolContext, task_id: str, timeout_sec: int = 180) -> str:
    """Wait for a subtask to reach a terminal status."""
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (wait_task): {exc}"
    try:
        timeout = max(0, min(int(timeout_sec), 3600))
    except (TypeError, ValueError):
        timeout = 180
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    status_drive_root = Path(str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    waited = wait_for_effective_tasks(status_drive_root, [tid], timeout_sec=timeout)
    header = "Task wait completed" if waited.get("all_terminal") else "Task wait timed out"
    return f"{header} after {waited.get('elapsed_sec', 0):.1f}s.\n\n{_get_task_result(ctx, tid)}"


def _wait_for_tasks(
    ctx: ToolContext,
    task_ids: List[str],
    timeout_sec: int = 600,
    mode: str = "all_terminal",
) -> str:
    """Wait for multiple subtasks and return their full effective results."""
    if not isinstance(task_ids, list) or not task_ids:
        return "⚠️ TOOL_ARG_ERROR (wait_tasks): task_ids must be a non-empty list."
    if len(task_ids) > 50:
        return "⚠️ TOOL_ARG_ERROR (wait_tasks): task_ids is capped at 50."
    normalized_ids: List[str] = []
    for item in task_ids:
        try:
            tid = validate_task_id(item)
        except ValueError as exc:
            return f"⚠️ TOOL_ARG_ERROR (wait_tasks): {exc}"
        if tid not in normalized_ids:
            normalized_ids.append(tid)
    try:
        timeout = max(0, min(int(timeout_sec), 7200))
    except (TypeError, ValueError):
        timeout = 600
    normalized_mode = str(mode or "all_terminal").strip().lower()
    if normalized_mode not in {"all_terminal", "any_terminal"}:
        return "⚠️ TOOL_ARG_ERROR (wait_tasks): mode must be all_terminal or any_terminal."
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    status_drive_root = Path(str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    waited = wait_for_effective_tasks(status_drive_root, normalized_ids, timeout_sec=timeout, mode=normalized_mode)
    tasks = waited.get("tasks")
    if isinstance(tasks, dict):
        public_tasks: Dict[str, Any] = {}
        for tid, data in tasks.items():
            if not isinstance(data, dict):
                public_tasks[str(tid)] = data
                continue
            public_tasks[str(tid)] = public_task_result(data)
        waited["tasks"] = public_tasks
    return json.dumps(waited, ensure_ascii=False, indent=2)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("set_tool_timeout", {
            "name": "set_tool_timeout",
            "description": "Update the global tool timeout in settings.json and apply it immediately without restart.",
            "parameters": {"type": "object", "properties": {
                "seconds": {"type": "integer", "description": "New timeout in seconds (>= 1)"},
            }, "required": ["seconds"]},
        }, _set_tool_timeout),
        ToolEntry("request_restart", {
            "name": "request_restart",
            "description": "Ask supervisor to restart runtime (after a reviewed local commit or clean no-op state).",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }, _request_restart),
        ToolEntry("promote_to_stable", {
            "name": "promote_to_stable",
            "description": "Promote ouroboros -> ouroboros-stable. Call when you consider the code stable.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }, _promote_to_stable),
        ToolEntry("schedule_subagent", {
            "name": "schedule_subagent",
            "description": (
                "Schedule a live subagent (a child of Ouroboros). Returns task_id for later retrieval. "
                "DEFAULT is READ-ONLY: the child inspects local repo/data/history plus web/browser and "
                "returns findings (it cannot write local state, commit, enable tools, or run "
                "shell/review/runtime/skills). Set write_surface to spawn a MUTATIVE (acting) child that "
                "writes inside an ISOLATED root and returns a workspace.patch you integrate with "
                "integrate_subagent_patch — you remain the sole committer of the live body. write_surface: "
                "self_worktree (isolated git worktree of THIS repo, for parallel self-modification / best-of-N), "
                "external_workspace (an external project dir via write_root or the parent workspace), or "
                "genesis (a from-scratch new project — game/site/app/new Ouroboros — auto-provisioned as a fresh "
                "empty git repo under the durable projects root; the project directory IS the deliverable, not "
                "integrated into this repo). "
                "Mutative children still cannot commit, run "
                "review/runtime/skills lifecycle, enable tools, or write cognitive memory. Nested delegation "
                "is allowed within configured depth/cap limits. Always retrieve the handoff with "
                "get_task_result, wait_task, or wait_tasks before relying on its results."
            ),
            "parameters": {"type": "object", "properties": {
                "objective": {"type": "string", "description": "Focused child objective. Be specific about scope."},
                "expected_output": {"type": "string", "description": "Concrete handoff expected from the child."},
                "role": {"type": "string", "description": "Optional freeform role label for lineage/UI, e.g. architecture-reviewer."},
                "context": {"type": "string", "description": "Optional parent reference material. It is injected as context, not instructions."},
                "constraints": {"type": "string", "description": "Optional constraints/non-goals for the child."},
                "memory_mode": {
                    "type": "string",
                    "enum": sorted(VALID_SUBTASK_MEMORY_MODES),
                    "description": "Child memory mode. Default forked copies stable memory only; empty starts blank. shared is disabled for live local subagents.",
                },
                "model_lane": {
                    "type": "string",
                    "enum": ["auto", "main", "code", "light", "review", "scope"],
                    "default": "auto",
                    "description": "Model lane for the child. auto uses safe light; main/code/light use those configured slots; review/scope fan out across configured reviewer slots and return a task_group.",
                },
                "write_surface": {
                    "type": "string",
                    "enum": ["", "self_worktree", "external_workspace", "genesis"],
                    "default": "",
                    "description": "Omit/empty = read-only child. Otherwise the isolated write surface for a mutative child (see tool description). Requires mutative subagents enabled (default ON in advanced/pro).",
                },
                "write_root": {"type": "string", "description": "For write_surface=external_workspace: the external project directory. Ignored for self_worktree and genesis (both auto-provisioned)."},
                "protected_paths_grant": {"type": "boolean", "default": False, "description": "Allow the child to modify protected paths in its self_worktree. Honored only in pro runtime mode; you still re-check at integration."},
                "external_tool_grants": {"type": "array", "items": {"type": "string"}, "description": "Optional extension/MCP tool names to grant this mutative child. Denied by default."},
            }, "required": ["objective", "expected_output"], "additionalProperties": False},
        }, _schedule_task),
        ToolEntry("cancel_task", {
            "name": "cancel_task",
            "description": "Cancel a task by ID.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        }, _cancel_task),
        ToolEntry("request_deep_self_review", {
            "name": "request_deep_self_review",
            "description": "Request an Atlas-backed deep self-review of the entire Ouroboros project. Uses OUROBOROS_MODEL_DEEP_SELF_REVIEW with its matching provider key, full core memory whitelist, and manifest accounting for every tracked repo path against the Constitution. Results go to chat and memory.",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string", "description": "Why you want a review (context for the reviewer)"},
            }, "required": ["reason"]},
        }, _request_deep_self_review),
        ToolEntry("chat_history", {
            "name": "chat_history",
            "description": "Retrieve messages from chat history. Supports search.",
            "parameters": {"type": "object", "properties": {
                "count": {"type": "integer", "default": 100, "description": "Number of messages (from latest)"},
                "offset": {"type": "integer", "default": 0, "description": "Skip N from end (pagination)"},
                "search": {"type": "string", "default": "", "description": "Text filter"},
            }, "required": []},
        }, _chat_history),
        ToolEntry("update_scratchpad", {
            "name": "update_scratchpad",
            "description": "Append a block to your working memory (scratchpad). Each call adds a "
                           "timestamped block; oldest blocks are auto-evicted when the cap (10) is reached. "
                           "Write what matters NOW — active tasks, decisions, observations. "
                           "Persists across sessions, read at every task start.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "Content for this scratchpad block"},
            }, "required": ["content"]},
        }, _update_scratchpad),
        ToolEntry("send_user_message", {
            "name": "send_user_message",
            "description": "Send a proactive message to the user. Use when you have something "
                           "genuinely worth saying — an insight, a question, or an invitation to collaborate. "
                           "This is NOT for task responses (those go automatically).",
            "parameters": {"type": "object", "properties": {
                "text": {"type": "string", "description": "Message text"},
                "reason": {"type": "string", "description": "Why you're reaching out (logged, not sent)"},
            }, "required": ["text"]},
        }, _send_user_message),
        ToolEntry("update_identity", {
            "name": "update_identity",
            "description": "Update your identity manifest (who you are, who you want to become). "
                           "Persists across sessions. Obligation to yourself (Principle 1: Continuity). "
                           "Read your current identity first, then evolve it — add, refine, deepen. "
                           "Full rewrites are allowed but should be rare; continuity of self matters. "
                           "Use this only after substantive reflection or real experience — not on a "
                           "greeting or trivial turn. This is the only correct way to write identity; "
                           "never write memory/identity.md through write_file/edit_text.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "Full identity content (prefer evolving over rewriting from scratch)"},
            }, "required": ["content"]},
        }, _update_identity),
        ToolEntry("toggle_evolution", {
            "name": "toggle_evolution",
            "description": "Enable or disable evolution mode. When enabled, Ouroboros runs continuous self-improvement cycles. Enabling requires runtime_mode 'advanced' or 'pro'; it is refused in 'light' mode.",
            "parameters": {"type": "object", "properties": {
                "enabled": {"type": "boolean", "description": "true to enable, false to disable"},
                "objective": {"type": "string", "default": "", "description": "Optional Evolution Campaign objective when enabling."},
            }, "required": ["enabled"]},
        }, _toggle_evolution),
        ToolEntry("toggle_consciousness", {
            "name": "toggle_consciousness",
            "description": "Control background consciousness: 'start', 'stop', or 'status'.",
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["start", "stop", "status"], "description": "Action to perform"},
            }, "required": ["action"]},
        }, _toggle_consciousness),
        ToolEntry("switch_model", {
            "name": "switch_model",
            "description": "Switch to a different LLM model or reasoning effort level. "
                           "Use when you need more power (complex code, deep reasoning) "
                           "or want to save budget (simple tasks). Takes effect on next round.",
            "parameters": {"type": "object", "properties": {
                "model": {"type": "string", "description": "Model name (e.g. anthropic/claude-sonnet-4). Leave empty to keep current."},
                "effort": {"type": "string", "enum": ["low", "medium", "high", "xhigh"],
                           "description": "Reasoning effort level. Leave empty to keep current."},
            }, "required": []},
        }, _switch_model),
        ToolEntry("get_task_result", {
            "name": "get_task_result",
            "description": "Read the effective result of a subtask, including child-drive output when available.",
            "parameters": {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "string", "description": "Task ID returned by schedule_subagent"},
            }},
        }, _get_task_result),
        ToolEntry("wait_task", {
            "name": "wait_task",
            "description": "Wait for a subtask to reach a terminal status and return its effective result.",
            "parameters": {"type": "object", "required": ["task_id"], "properties": {
                "task_id": {"type": "string", "description": "Task ID to check"},
                "timeout_sec": {"type": "integer", "default": 180, "description": "Maximum seconds to wait (default 180)."},
            }},
        }, _wait_for_task),
        ToolEntry("wait_tasks", {
            "name": "wait_tasks",
            "description": "Wait for multiple subtasks and return full effective results for each child.",
            "parameters": {"type": "object", "required": ["task_ids"], "properties": {
                "task_ids": {"type": "array", "items": {"type": "string"}, "description": "Task IDs returned by schedule_subagent."},
                "timeout_sec": {"type": "integer", "default": 600, "description": "Maximum seconds to wait (default 600)."},
                "mode": {"type": "string", "enum": ["all_terminal", "any_terminal"], "default": "all_terminal"},
            }},
        }, _wait_for_tasks, timeout_sec=7200),
    ]
