"""Pre-implementation Atlas-backed design review tool."""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import os
import pathlib
import time
from dataclasses import dataclass, replace
from datetime import timedelta

from ouroboros.config import SETTINGS_DEFAULTS
from ouroboros.deadline_utils import parse_deadline_ts, utc_now as _planning_now
from ouroboros.llm import LLMClient
from ouroboros.review_substrate import review_repo_dirs_for
from ouroboros.task_results import (
    load_plan_review_state,
    plan_review_wave,
    plan_review_wave_handoffs,
    plan_review_wave_task_ids,
    persist_plan_review_handoffs,
    record_plan_review_collection,
    record_plan_review_consumed,
    record_plan_review_result,
    record_plan_review_scout,
    represent_plan_review,
    reserve_plan_review_wave,
)
from ouroboros.task_status import FINAL_STATUSES, find_child_tasks, wait_for_effective_tasks
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.review_context_atlas import ReviewContextAtlasRequest, compile_review_context_atlas
from ouroboros.tools.review_helpers import (
    build_head_snapshot_section,
    load_governance_doc,
    load_checklist_section,
    review_wave_budget_gate,
    REVIEW_PROMPT_TOKEN_BUDGET as _REVIEW_BUDGET,
)
from ouroboros.tools.review_synthesis import (
    emit_plan_review_usage as _emit_plan_review_usage,  # noqa: F401 — test-compat re-export
    build_plan_review_messages,
    PLAN_REVIEW_CONTROL_PREFIX,
    UNBINDABLE_DISPOSITION_NOTE as _UNBINDABLE_DISPOSITION_NOTE,
    VACUOUS_DISPOSITION_NOTE as _VACUOUS_DISPOSITION_NOTE,
    addressable_plan_findings,
    vacuous_review_disposition as _vacuous_review_disposition,
    all_planning_tasks_terminal as _all_planning_tasks_known_terminal,
    bounded_planning_reason as _bounded_planning_reason,
    build_plan_review_system_prompt,
    build_plan_review_user_content,
    completed_planning_handoffs as _completed_planning_handoffs,
    format_planning_handoffs as _format_planning_handoffs,
    format_plan_review_output as _format_output,
    normalize_plan_scope as _normalize_plan_scope,
    parse_plan_review_signal,
    plan_review_fingerprint as _plan_request_fingerprint,
    plan_text_fingerprint,
    planning_handoff_selection as _planning_handoff_selection,
    planning_scout_framing as _planning_scout_framing,
    planning_swarm_context as _planning_swarm_context,
    render_plan_review_result as _render_existing_plan_review,
    summarize_plan_review_results as _summarize_plan_review_results,
    validate_plan_review_disposition,
)
from ouroboros.utils import estimate_tokens, utc_now_iso

_addressable_plan_findings = addressable_plan_findings
_parse_aggregate_signal = parse_plan_review_signal
_build_system_prompt = build_plan_review_system_prompt
_build_user_content = build_plan_review_user_content

log = logging.getLogger(__name__)

_PLAN_REVIEW_MAX_TOKENS = 65536
_PLAN_REVIEW_EFFORT = "high"
_PLAN_REVIEW_SLOT_TIMEOUT_SEC = 560
# Wrapper covers the shared scout cutoff plus one reviewer slot below the hard timeout.
_PLAN_SWARM_MAX_WAIT_DEFAULT_SEC = int(SETTINGS_DEFAULTS["OUROBOROS_PLAN_TASK_SWARM_MAX_WAIT_SEC"])  # config SSOT (no DRY mirror)
_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC = _PLAN_SWARM_MAX_WAIT_DEFAULT_SEC + _PLAN_REVIEW_SLOT_TIMEOUT_SEC + 60
_PLAN_TASK_TOOL_TIMEOUT_SEC = _PLAN_REVIEW_WRAPPER_TIMEOUT_SEC + 10


def _effective_swarm_max_wait() -> float:
    from ouroboros.config import get_plan_task_swarm_max_wait_sec
    return min(get_plan_task_swarm_max_wait_sec(), float(_PLAN_SWARM_MAX_WAIT_DEFAULT_SEC))

_PLAN_MODEL_CONTEXT_WINDOW = 1_000_000
_PLAN_OUTPUT_MARGIN_TOKENS = 155_000
_PLAN_BUDGET_TOKEN_LIMIT = min(
    _REVIEW_BUDGET,
    _PLAN_MODEL_CONTEXT_WINDOW - _PLAN_REVIEW_MAX_TOKENS - _PLAN_OUTPUT_MARGIN_TOKENS,
)


@dataclass(frozen=True)
class _PlanReviewRequest:
    plan: str
    goal: str
    files_to_touch: list
    context_level: str = ""
    context_notes: str = ""
    include_tests: bool = False
    plan_class: str = ""
    scope: dict | None = None
    review_disposition: dict | None = None


@dataclass
class _PlanReviewFinalization:
    request: _PlanReviewRequest
    raw_results: list[dict]
    models: list[str]
    estimated_tokens: int
    subject_repo: pathlib.Path
    governance_repo: pathlib.Path
    planning_handoffs: dict
    state_root: pathlib.Path
    state_task_id: str
    request_fingerprint: str
    degraded_scout_note: str
    reviewed_result_hashes: dict[str, str]


def get_tools():
    return [
        ToolEntry(
            name="plan_task",
            schema={
                "name": "plan_task",
                "description": (
                    "Run a pre-implementation design review of a proposed plan. It first starts a small "
                    "local-readonly planning-scout subagent swarm and waits for every started scout to "
                    "finish or reach one shared OUROBOROS_PLAN_TASK_SWARM_MAX_WAIT_SEC cutoff "
                    "for raw handoffs, then runs the configured reviewer slots (an arbitrary N, "
                    "duplicates allowed) in parallel. Call this BEFORE writing any code for non-trivial tasks (>2 files or >50 lines "
                    "of changes). The agent chooses the context level: minimal includes governance docs, the plan, "
                    "and touched-file snapshots; localized/broad/constitutional add a generated repository Atlas. "
                    "Reviewers identify forgotten touchpoints, implicit contract "
                    "violations, simpler alternatives, and Bible/architecture compliance issues — before you've "
                    "written a single line. Uses the reviewer slots configured in OUROBOROS_REVIEW_MODELS (same "
                    "slot as the commit triad); duplicate model IDs are allowed and count as separate stochastic "
                    "slots. Returns structured feedback from every reviewer slot with detailed explanations and "
                    "alternative approaches. GREEN closes the exact plan fingerprint; REVIEW_REQUIRED "
                    "is closed by an exact fingerprint-bound review_disposition without another LLM call; "
                    "REVISE_PLAN requires changed plan text and a fresh review."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "plan": {"type": "string", "description": "Describe what you plan to implement: which files you will change, what the key design decisions are, and what you will NOT change."},
                        "goal": {"type": "string", "description": "The high-level goal of the task (what problem is being solved)."},
                        "plan_class": {
                            "type": "string",
                            "enum": ["self_mod", "external", "creative", "research"],
                            "description": (
                                "What KIND of plan this is — your own classification: self_mod (changes to the "
                                "Ouroboros system repo — full governance pack), external (an external codebase/"
                                "workspace), creative (content/design/site deliverables), research (investigation/"
                                "analysis). The host STRUCTURALLY escalates to self_mod when files_to_touch resolve "
                                "under the system repo. Non-self_mod classes get a leaner doc pack (ARCHITECTURE as "
                                "a navigation map) and task-fit scout framing."
                            ),
                        },
                        "files_to_touch": {"type": "array", "description": "Optional list of repo-relative file paths you plan to modify. Their current content (HEAD snapshot) will be injected so reviewers can reason about concrete code, not just abstract plans.", "items": {"type": "string"}},
                        "context_level": {
                            "type": "string",
                            "enum": ["minimal", "localized", "broad", "constitutional"],
                            "description": (
                                "Agent-chosen repository context level. Choose explicitly: minimal omits generated "
                                "Atlas context but keeps governance docs and touched-file snapshots; localized adds "
                                "a small Atlas around files_to_touch; broad is for shared contracts; constitutional "
                                "is for self-evolution/immune surfaces. For non-self_mod plan classes it may be "
                                "omitted and defaults to minimal."
                            ),
                        },
                        "context_notes": {
                            "type": "string",
                            "default": "",
                            "description": "Optional agent-chosen notes explaining why this context level/evidence is appropriate.",
                        },
                        "scope": {
                            "type": "object",
                            "additionalProperties": False,
                            "description": (
                                "Optional structured intent boundary shown beside the goal: what is in scope, "
                                "mandatory invariants, non-goals, the existing seam selected for extension, "
                                "and explicitly rejected expansions."
                            ),
                            "properties": {
                                "in_scope": {"type": "array", "items": {"type": "string"}},
                                "invariants": {"type": "array", "items": {"type": "string"}},
                                "non_goals": {"type": "array", "items": {"type": "string"}},
                                "selected_seam": {"type": "string"},
                                "rejected_expansions": {"type": "array", "items": {"type": "string"}},
                            },
                        },
                        "review_disposition": {
                            "type": "object",
                            "additionalProperties": False,
                            "description": (
                                "Resolve a prior REVIEW_REQUIRED result for the exact unchanged plan "
                                "fingerprint without another reviewer call. Every reported finding id "
                                "must appear exactly once. Omit the field on a first submission."
                            ),
                            "properties": {
                                "review_fingerprint": {"type": "string"},
                                "items": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "finding_id": {"type": "string"},
                                            "decision": {
                                                "type": "string",
                                                "enum": ["accept", "reject", "defer"],
                                            },
                                            "rationale": {"type": "string"},
                                            "plan_revision": {
                                                "type": "string",
                                                "description": (
                                                    "Required for accept: a concrete reference to the "
                                                    "corresponding revision/implementation adjustment."
                                                ),
                                            },
                                        },
                                        "required": ["finding_id", "decision", "rationale"],
                                    },
                                },
                            },
                            "required": ["review_fingerprint", "items"],
                        },
                        "include_tests": {
                            "type": "boolean",
                            "default": False,
                            "description": "Whether generated Atlas context may include related tests.",
                        },
                    },
                    # context_level is enforced HOST-SIDE by _resolve_plan_context_level:
                    # explicit-choice for self_mod, optional (defaults minimal) for
                    # external/creative/research — an unconditional schema `required`
                    # contradicted that contract (triad r2, self_consistency).
                    "required": ["plan", "goal"],
                },
            },
            handler=_handle_plan_task,
            timeout_sec=_PLAN_TASK_TOOL_TIMEOUT_SEC,
        )
    ]


def _handle_plan_task(ctx: ToolContext, **params) -> str:
    review_disposition = params.get("review_disposition")
    vacuous_disposition = _vacuous_review_disposition(review_disposition)
    review_disposition = None if vacuous_disposition else review_disposition
    setattr(ctx, "_plan_disposition_ignored_unbindable", False)
    request = _PlanReviewRequest(
        plan=str(params.get("plan") or ""),
        goal=str(params.get("goal") or ""),
        files_to_touch=list(params.get("files_to_touch") or []),
        context_level=str(params.get("context_level") or ""),
        context_notes=str(params.get("context_notes") or ""),
        include_tests=bool(params.get("include_tests", False)),
        plan_class=str(params.get("plan_class") or ""),
        scope=params.get("scope"),
        review_disposition=review_disposition,
    )
    if not request.plan.strip():
        return "ERROR: plan parameter is required and must not be empty."
    if not request.goal.strip():
        return "ERROR: goal parameter is required and must not be empty."

    try:
        try:
            asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    asyncio.run,
                    asyncio.wait_for(
                        _run_plan_review_async(
                            ctx,
                            request,
                        ),
                        timeout=_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC,
                    ),
                ).result(timeout=_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC + 5)
        except RuntimeError:
            result = asyncio.run(
                asyncio.wait_for(
                    _run_plan_review_async(
                        ctx,
                        request,
                    ),
                    timeout=_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC,
                )
            )
        if isinstance(result, str) and vacuous_disposition:
            result += _VACUOUS_DISPOSITION_NOTE
        elif isinstance(result, str) and getattr(ctx, "_plan_disposition_ignored_unbindable", False):
            result += _UNBINDABLE_DISPOSITION_NOTE
        return result
    except concurrent.futures.TimeoutError:
        return f"ERROR: Plan review timed out after {_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC}s."
    except asyncio.TimeoutError:
        return f"ERROR: Plan review timed out after {_PLAN_REVIEW_WRAPPER_TIMEOUT_SEC}s."
    except Exception as e:
        log.error("plan_task failed: %s", e, exc_info=True)
        return f"ERROR: Plan review failed: {e}"


def _planning_swarm_count(context_level: str, files_to_touch: list) -> int:
    try:
        from ouroboros.config import get_max_active_subagents_per_root

        cap = get_max_active_subagents_per_root()
    except Exception:
        cap = 3
    desired = 2 if context_level in {"broad", "constitutional"} or len(files_to_touch or []) > 3 else 1
    return max(1, min(int(cap or 1), desired))


def _persist_planning_handoffs(ctx: ToolContext, handoffs: dict) -> dict:
    task_id = str(getattr(ctx, "task_id", "") or "plan_review")
    return persist_plan_review_handoffs(ctx.drive_root, task_id, handoffs)


def _planning_handoff_path(ctx: ToolContext) -> pathlib.Path:
    task_id = str(getattr(ctx, "task_id", "") or "plan_review")
    return pathlib.Path(ctx.drive_root) / "task_results" / "artifacts" / task_id / "plan_task_handoffs.json"


def _planning_state_location(ctx: ToolContext) -> tuple[pathlib.Path, str]:
    root = pathlib.Path(str(getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    task_id = str(getattr(ctx, "task_id", "") or "").strip()
    if not task_id:
        raise ValueError(
            "PLAN_REVIEW_TASK_ID_REQUIRED: durable review state must belong to a real task"
        )
    return root, task_id


def _collect_planning_handoffs(
    ctx: ToolContext,
    *,
    task_ids: list[str],
    schedule_outputs: list[str],
    fingerprint: str,
    wait_timeout: float,
    max_wait: float = 0.0,
    intended_scouts: list[dict] | None = None,
    cutoff_at: str = "",
) -> dict:
    """Wait for every started scout until terminal or the one shared cutoff."""
    status_root = pathlib.Path(str(getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    slice_sec = max(0.25, float(wait_timeout or 0))
    ceiling = max(0.0, float(max_wait or slice_sec))
    cutoff = parse_deadline_ts(cutoff_at) if cutoff_at else _planning_now() + timedelta(seconds=ceiling)
    if cutoff is None:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: scout_cutoff_at is malformed")
    start = time.monotonic()
    remaining_at_start = max(0.0, (cutoff - _planning_now()).total_seconds())
    stop_reason = ""
    waited: dict = {}
    while True:
        remaining = (cutoff - _planning_now()).total_seconds()
        if remaining <= 0.01:
            waited = wait_for_effective_tasks(
                status_root, task_ids, timeout_sec=0.0, mode="all_terminal", poll_interval_sec=0.25,
            )
            stop_reason = "ceiling"
            break
        waited = wait_for_effective_tasks(
            status_root,
            task_ids,
            timeout_sec=min(slice_sec, remaining),
            mode="all_terminal",
            poll_interval_sec=0.25,
        )
        tasks = waited.get("tasks") if isinstance(waited.get("tasks"), dict) else {}
        if waited.get("all_terminal") or _all_planning_tasks_known_terminal(task_ids, tasks or {}):
            break
        # Heartbeats are diagnostic; every scout gets the same terminal-or-cutoff window.
    tasks = waited.get("tasks") if isinstance(waited.get("tasks"), dict) else {}
    attempts = intended_scouts or [
        {"role": str((tasks.get(task_id) or {}).get("role") or ""),
         "schedule_status": "started", "task_ids": [task_id], "schedule_reason": ""}
        for task_id in task_ids
    ]
    included_task_ids, omissions = _planning_handoff_selection(attempts, tasks or {}, stop_reason)
    handoffs = {
        "schema_version": 1,
        "ts": utc_now_iso(),
        "request_fingerprint": fingerprint,
        "task_ids": task_ids,
        "schedule_outputs": schedule_outputs,
        "scout_cutoff_at": cutoff.isoformat(),
        "wait": waited,
        "wait_stop_reason": stop_reason,
        "wait_elapsed_sec": round(time.monotonic() - start, 2),
        "wait_remaining_at_start_sec": round(remaining_at_start, 2),
        "included_task_ids": included_task_ids,
        "omissions": omissions,
        "consumed_task_ids": [],
    }
    artifact = _persist_planning_handoffs(ctx, handoffs)
    handoffs["artifact"] = artifact
    return handoffs


def _planning_swarm_timing(ctx: ToolContext) -> tuple[float, float]:
    from ouroboros.config import get_plan_task_swarm_timeout_sec

    wait_timeout, max_wait = get_plan_task_swarm_timeout_sec(), _effective_swarm_max_wait()
    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    deadline = parse_deadline_ts(metadata.get("deadline_at"))
    if deadline is not None:
        remaining = (deadline - _planning_now()).total_seconds()
        max_wait = 0.0 if remaining <= 0 else min(max_wait, remaining / 4.0)
    event_queue = getattr(ctx, "event_queue", None)
    live = event_queue is not None and event_queue.__class__.__module__ in {"queue", "multiprocessing.queues"}
    if not live:
        wait_timeout = min(wait_timeout, 0.25)
        max_wait = min(max_wait, wait_timeout)
    return wait_timeout, max_wait


def _planning_direct_children(ctx: ToolContext) -> dict[str, dict]:
    """Read durable direct-child authority for scheduling recovery."""
    root, parent_id = _planning_state_location(ctx)
    try:
        rows = find_child_tasks(
            root,
            parent_task_id=parent_id,
            root_task_id="",
            exclude_task_id=parent_id,
            scope="direct",
        )
    except Exception:
        log.debug("plan_task could not read durable child authority", exc_info=True)
        return {}
    return {
        str(row.get("task_id") or row.get("id") or ""): row
        for row in rows
        if isinstance(row, dict) and str(row.get("task_id") or row.get("id") or "")
    }


def _scheduled_side_channel_ids(ctx: ToolContext) -> list[str]:
    records = getattr(ctx, "_last_scheduled_subagents", [])
    if not isinstance(records, list):
        return []
    return list(dict.fromkeys(
        str(task_id)
        for record in records
        if isinstance(record, dict)
        for task_id in (record.get("task_ids") or [])
        if str(task_id)
    ))


def _schedule_planning_scouts(
    ctx: ToolContext, wave: dict, *, fingerprint: str, objective: str, constraints: str, context: str,
) -> dict:
    from ouroboros.tools.control import _schedule_task

    root, parent_id = _planning_state_location(ctx)
    for attempt in wave.get("intended_scouts") or []:
        if str(attempt.get("schedule_status") or "") != "pending":
            continue
        role = str(attempt.get("role") or "")
        before_side = set(_scheduled_side_channel_ids(ctx))
        before_durable = set(_planning_direct_children(ctx))
        try:
            output = _schedule_task(
                ctx, objective=objective,
                expected_output=("A concise planning handoff with sections: summary, missed_touchpoints, "
                                 "risks, suggested_scope_adjustments, tests_to_run, blockers."),
                role=role, context=context, constraints=constraints, memory_mode="forked", model_lane="light",
            )
            reason = _bounded_planning_reason(output)
        except Exception as exc:
            output, reason = "", _bounded_planning_reason(f"{type(exc).__name__}: {exc}")
        after_side = [task_id for task_id in _scheduled_side_channel_ids(ctx) if task_id not in before_side]
        after_durable = [
            task_id
            for task_id, row in _planning_direct_children(ctx).items()
            if task_id not in before_durable and str(row.get("role") or "") == role
        ]
        after = list(dict.fromkeys(after_side + after_durable))
        if len(after) > 1:
            raise ValueError(
                "PLAN_REVIEW_STATE_INVALID: one planning scout intent issued multiple child ids"
            )
        status = "started" if after else "failed"
        if after:
            reason = reason or "scheduled"
        else:
            reason = _bounded_planning_reason(
                "host issued no task id" + (f"; {reason}" if reason else "")
            )
        wave = record_plan_review_scout(
            root, parent_id, fingerprint=fingerprint, role=role, schedule_status=status,
            task_ids=after, reason=reason,
        )
    return wave


def _recover_pending_planning_scouts(
    ctx: ToolContext,
    state: dict,
    wave: dict,
    *,
    fingerprint: str,
) -> dict:
    """Resolve an interrupted schedule from durable child rows before declaring omission."""
    root, parent_id = _planning_state_location(ctx)
    assigned = {
        str(task_id)
        for stored_wave in state.get("waves") or []
        for attempt in stored_wave.get("intended_scouts") or []
        for task_id in (attempt.get("task_ids") or [])
        if str(task_id)
    }
    children = _planning_direct_children(ctx)
    created_at = parse_deadline_ts(wave.get("created_at"))
    for attempt in list(wave.get("intended_scouts") or []):
        if str(attempt.get("schedule_status") or "") != "pending":
            continue
        role = str(attempt.get("role") or "")
        candidates: list[str] = []
        if created_at is not None:
            for task_id, row in children.items():
                row_ts = parse_deadline_ts(row.get("ts"))
                if (
                    task_id not in assigned
                    and str(row.get("role") or "") == role
                    and row_ts is not None
                    and row_ts >= created_at
                ):
                    candidates.append(task_id)
        if len(candidates) == 1:
            status = "started"
            reason = "recovered durable issued child id after interrupted scheduling"
            assigned.add(candidates[0])
        else:
            status = "unknown"
            reason = (
                "scheduling was interrupted and durable child authority was ambiguous"
                if len(candidates) > 1
                else "scheduling was interrupted before any issued child id was durably recoverable"
            )
            candidates = []
        wave = record_plan_review_scout(
            root,
            parent_id,
            fingerprint=fingerprint,
            role=role,
            schedule_status=status,
            task_ids=candidates,
            reason=reason,
        )
    return wave


def _collect_host_planning_wave(
    ctx: ToolContext, wave: dict, *, fingerprint: str, wait_timeout: float, max_wait: float,
) -> tuple[dict, dict]:
    root, parent_id = _planning_state_location(ctx)
    task_ids = plan_review_wave_task_ids(wave)
    handoffs = _collect_planning_handoffs(
        ctx, task_ids=task_ids,
        schedule_outputs=[str(item.get("schedule_reason") or "") for item in wave.get("intended_scouts") or []],
        fingerprint=fingerprint, wait_timeout=wait_timeout, max_wait=max_wait,
        intended_scouts=list(wave.get("intended_scouts") or []),
        cutoff_at=str(wave.get("scout_cutoff_at") or ""),
    )
    wave = record_plan_review_collection(
        root, parent_id, fingerprint=fingerprint,
        included_task_ids=list(handoffs.get("included_task_ids") or []),
        omissions=list(handoffs.get("omissions") or []),
        stop_reason=str(handoffs.get("wait_stop_reason") or ""),
    )
    handoffs.update({key: value for key, value in plan_review_wave_handoffs(wave).items() if key != "wait"})
    handoffs["artifact"] = _persist_planning_handoffs(ctx, handoffs)
    return handoffs, wave


def _start_planning_swarm(
    ctx: ToolContext,
    request: _PlanReviewRequest,
) -> dict:
    from ouroboros.config import get_max_workers

    plan = request.plan
    goal = request.goal
    files_to_touch = request.files_to_touch
    context_level = request.context_level
    context_notes = request.context_notes
    plan_class = request.plan_class or "self_mod"
    scope = request.scope
    include_tests = request.include_tests
    fingerprint = _plan_request_fingerprint(
        plan=plan, goal=goal, files_to_touch=files_to_touch, context_level=context_level,
        context_notes=context_notes, plan_class=plan_class, scope=scope, include_tests=include_tests,
    )
    wait_timeout, max_wait = _planning_swarm_timing(ctx)
    root, parent_id = _planning_state_location(ctx)
    try:
        state = load_plan_review_state(root, parent_id)
        wave = plan_review_wave(state, fingerprint)
        resumed = wave is not None
        created = False
        if wave is None:
            roles = [f"planning-scout-{idx + 1}" for idx in range(_planning_swarm_count(context_level, files_to_touch))]
            wave, created = reserve_plan_review_wave(
                root, parent_id, fingerprint=fingerprint, plan_text_hash=plan_text_fingerprint(plan),
                scout_roles=roles, cutoff_at=(_planning_now() + timedelta(seconds=max_wait)).isoformat(),
            )
            resumed = not created
            if created and get_max_workers() < 2:
                message = "no spare worker capacity; increase OUROBOROS_MAX_WORKERS to at least 2"
                for attempt in list(wave.get("intended_scouts") or []):
                    wave = record_plan_review_scout(
                        root, parent_id, fingerprint=fingerprint, role=str(attempt.get("role") or ""),
                        schedule_status="failed", task_ids=[], reason=message,
                    )
            elif created:
                objective, constraints = _planning_scout_framing(plan_class)
                wave = _schedule_planning_scouts(
                    ctx, wave, fingerprint=fingerprint, objective=objective, constraints=constraints,
                    context=_planning_swarm_context(
                        plan=plan, goal=goal, files_to_touch=files_to_touch, context_level=context_level,
                        context_notes=context_notes, scope=scope,
                    ),
                )
        if not created and any(
            str(item.get("schedule_status") or "") == "pending"
            for item in wave.get("intended_scouts") or []
        ):
            wave = _recover_pending_planning_scouts(
                ctx, state, wave, fingerprint=fingerprint,
            )
        handoffs, wave = _collect_host_planning_wave(
            ctx, wave, fingerprint=fingerprint, wait_timeout=wait_timeout, max_wait=max_wait,
        )
    except (OSError, TimeoutError, ValueError) as exc:
        return {"started": False, "error": f"ERROR: PLAN_SCOUT_WAVE_STATE_PERSIST_FAILED: {exc}"}

    task_ids = plan_review_wave_task_ids(wave)
    wait_payload = handoffs.get("wait") if isinstance(handoffs.get("wait"), dict) else {}
    tasks = wait_payload.get("tasks") if isinstance(wait_payload.get("tasks"), dict) else {}
    completed = _completed_planning_handoffs(tasks or {})
    if not (handoffs.get("artifact") or {}).get("path"):
        return {"started": False, "error": "ERROR: raw planning handoff audit could not be saved.",
                "task_ids": task_ids, "handoffs": handoffs, "resumed": resumed}
    return {"started": True, "task_ids": task_ids, "handoffs": handoffs, "resumed": resumed,
            "degraded_evidence": not bool(completed)}


def _reviewed_handoff_hashes(handoffs: dict) -> dict[str, str]:
    """Hash the exact in-memory scout snapshots before the panel is dispatched."""
    included = [str(item) for item in (handoffs.get("included_task_ids") or []) if str(item)]
    wait = handoffs.get("wait") if isinstance(handoffs.get("wait"), dict) else {}
    tasks = wait.get("tasks") if isinstance(wait.get("tasks"), dict) else {}
    from ouroboros.tools.join_ledger import _child_result_sha256

    result: dict[str, str] = {}
    for child_task_id in included:
        snapshot = tasks.get(child_task_id)
        if not isinstance(snapshot, dict):
            raise ValueError(
                f"PLAN_REVIEW_STATE_INVALID: included scout {child_task_id} has no reviewed snapshot"
            )
        result[child_task_id] = _child_result_sha256(snapshot)
    return result


def _mark_planning_handoffs_consumed(ctx: ToolContext, handoffs: dict) -> dict:
    """Mark exactly the handoffs embedded in the reviewer request as consumed."""
    included = [str(item) for item in (handoffs.get("included_task_ids") or []) if str(item)]
    from ouroboros.tools.join_ledger import (
        CHILD_RESULT_DISPOSITION_TYPE,
        _record_child_result_disposition,
    )

    reviewed_hashes = handoffs.get("reviewed_result_hashes")
    if not isinstance(reviewed_hashes, dict) or set(reviewed_hashes) != set(included):
        # Compatibility for callers/tests that consume before the paid review is
        # stored. Production resume always uses the durable exact hash mapping.
        reviewed_hashes = _reviewed_handoff_hashes(handoffs)

    disposition_warnings: list[dict] = []
    for child_task_id in included:
        recorded = _record_child_result_disposition(
            ctx,
            {
                "type": CHILD_RESULT_DISPOSITION_TYPE,
                "child_task_id": child_task_id,
                "disposition": "integrated",
                "child_result_sha256": str(reviewed_hashes[child_task_id]),
            },
            "The exact planning scout handoff was embedded in the plan-review request.",
        )
        if "CHILD_RESULT_STALE" in recorded:
            disposition_warnings.append({
                "task_id": child_task_id,
                "code": "CHILD_RESULT_STALE",
                "detail": _bounded_planning_reason(recorded),
            })
        elif not recorded.startswith("OK:"):
            raise ValueError(recorded)
    root, task_id = _planning_state_location(ctx)
    wave = record_plan_review_consumed(
        root, task_id, fingerprint=str(handoffs.get("request_fingerprint") or ""),
        consumed_task_ids=included,
        disposition_warnings=disposition_warnings,
    )
    handoffs.update({key: value for key, value in plan_review_wave_handoffs(wave).items() if key != "wait"})
    handoffs.pop("reviewed_result_hashes", None)
    handoffs.pop("review_evidence_status", None)
    handoffs.setdefault("wait", {})
    artifact = _persist_planning_handoffs(ctx, handoffs)
    handoffs["artifact"] = artifact
    return wave


def _capture_late_planning_audit(ctx: ToolContext, handoffs: dict) -> None:
    """Record late omitted results for audit without feeding or reopening review."""
    omitted_ids = [
        str(item.get("task_id") or "")
        for item in (handoffs.get("omissions") or [])
        if isinstance(item, dict) and str(item.get("task_id") or "")
    ]
    if not omitted_ids:
        return
    status_root = pathlib.Path(str(getattr(ctx, "budget_drive_root", "") or ctx.drive_root))
    current = wait_for_effective_tasks(
        status_root,
        omitted_ids,
        timeout_sec=0.0,
        mode="all_terminal",
        poll_interval_sec=0.25,
    )
    tasks = current.get("tasks") if isinstance(current.get("tasks"), dict) else {}
    late_tasks = {
        task_id: row
        for task_id, row in (tasks or {}).items()
        if isinstance(row, dict)
        and str(row.get("status") or "").strip().lower() in FINAL_STATUSES
        and str(row.get("result") or "").strip()
    }
    if not late_tasks:
        return
    handoffs["late_audit"] = {
        "captured_at": utc_now_iso(),
        "affects_review": False,
        "tasks": late_tasks,
    }
    artifact = _persist_planning_handoffs(ctx, handoffs)
    handoffs["artifact"] = artifact


def _resolve_plan_roots(
    ctx: ToolContext, files_to_touch: list,
) -> tuple[pathlib.Path, pathlib.Path]:
    """Resolve governance and subject roots without silently mixing them."""
    governance, subject = review_repo_dirs_for(ctx)
    for raw in files_to_touch or []:
        candidate = pathlib.Path(str(raw or ""))
        resolved = (candidate if candidate.is_absolute() else subject / candidate).resolve(strict=False)
        try:
            resolved.relative_to(subject)
        except ValueError as exc:
            raise ValueError(
                f"planned path {raw!r} escapes active subject root {subject}"
            ) from exc
    return governance, subject


def _planning_evidence_horizon(
    ctx: ToolContext,
    *,
    governance_repo: pathlib.Path,
    subject_repo: pathlib.Path,
    scope: dict | None = None,
) -> str:
    """One compact planning-evidence manifest; no second context pipeline.

    The plan and goal remain the canonical inline intent.  This block contributes
    the durable task contract, lineage aliases, raw forensic refs and disclosed
    omissions exactly once to the shared reviewer prompt.
    """
    from ouroboros.observability import redact_projection

    meta = getattr(ctx, "task_metadata", {})
    meta = meta if isinstance(meta, dict) else {}
    contract = getattr(ctx, "task_contract", {})
    contract = contract if isinstance(contract, dict) else {}
    task_id = str(getattr(ctx, "task_id", "") or meta.get("task_id") or "")
    root_id = str(meta.get("root_task_id") or task_id)
    refs: list[dict] = []
    if task_id:
        candidates = (
            pathlib.Path(ctx.drive_root) / "task_results" / f"{task_id}.json",
            _planning_handoff_path(ctx),
        )
        for candidate in candidates:
            if candidate.is_file():
                refs.append({"kind": candidate.stem, "path": str(candidate)})
    omissions: list[dict] = []
    if not contract:
        omissions.append({
            "section": "task_contract",
            "reason": "not_available_in_tool_context",
        })
    payload = {
        "schema_version": 1,
        "canonical_intent": {
            "goal_ref": "Implementation Plan Under Review.Goal",
            "plan_ref": "Implementation Plan Under Review.Proposed Plan",
            "scope": _normalize_plan_scope(scope),
            "task_contract": redact_projection(contract).value if contract else {},
        },
        "aliases": {
            "task_id": task_id,
            "root_task_id": root_id,
            "parent_task_id": str(meta.get("parent_task_id") or ""),
            "project_id": str(getattr(ctx, "project_id", "") or meta.get("project_id") or ""),
        },
        "roots": {
            "governance": str(governance_repo),
            "subject": str(subject_repo),
        },
        "forensic_refs": refs,
        "omissions_manifest": omissions,
    }
    return (
        "## Planning Evidence Horizon\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, default=str)
        + "\n```"
    )


def _apply_review_disposition(
    ctx: ToolContext,
    audit: dict,
    review: dict,
    fingerprint: str,
    disposition: dict,
) -> str:
    updated, error = validate_plan_review_disposition(review, fingerprint, disposition)
    if error or updated is None:
        return error
    updated["disposition"]["recorded_at"] = utc_now_iso()
    root, task_id = _planning_state_location(ctx)
    try:
        wave = record_plan_review_result(
            root, task_id, fingerprint=fingerprint, review=updated, require_latest=True,
        )
    except (OSError, TimeoutError, ValueError) as exc:
        return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(exc)
    audit.update(plan_review_wave_handoffs(wave))
    persisted = _persist_planning_handoffs(ctx, audit)
    if persisted.get("error"):
        return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(persisted["error"])
    audit["artifact"] = persisted
    return _planning_disposition_warning_note(audit) + _render_existing_plan_review(updated)


def _planning_disposition_warning_note(handoffs: dict) -> str:
    warnings = handoffs.get("disposition_warnings")
    count = len(warnings) if isinstance(warnings, list) else 0
    if not count:
        return ""
    return (
        "⚠️ PLANNING SCOUT SNAPSHOT CHANGED: "
        f"{count} reviewer-included scout result(s) changed after the exact snapshot "
        "was sent to the panel. The reviewed snapshot remains plan evidence; the newer "
        "result is audit-only and was not marked integrated.\n\n"
    )


def _replay_closed_review_disposition(
    review: dict,
    fingerprint: str,
    disposition: dict,
) -> str:
    """Accept only a semantic replay of the disposition that already closed review."""
    updated, error = validate_plan_review_disposition(review, fingerprint, disposition)
    if error or updated is None:
        return error

    def _signature(value: object) -> tuple[str, tuple[tuple[str, str, str, str], ...]]:
        payload = value if isinstance(value, dict) else {}
        items = payload.get("items") if isinstance(payload.get("items"), list) else []
        normalized = sorted(
            (
                str(item.get("finding_id") or ""),
                str(item.get("decision") or ""),
                str(item.get("rationale") or ""),
                str(item.get("plan_revision") or ""),
            )
            for item in items
            if isinstance(item, dict)
        )
        return str(payload.get("review_fingerprint") or ""), tuple(normalized)

    if _signature(review.get("disposition")) != _signature(updated.get("disposition")):
        return (
            "ERROR: PLAN_REVIEW_DISPOSITION_IMMUTABLE: this exact review was already "
            "closed by a different disposition."
        )
    return _render_existing_plan_review(review, cached=True)


def _reuse_or_disposition_plan_review(
    ctx: ToolContext,
    fingerprint: str,
    review_disposition: dict | None,
    plan_text_hash: str = "",
) -> str | None:
    if _vacuous_review_disposition(review_disposition):
        review_disposition = None  # vacuous == absent (see review_synthesis)
    root, task_id = _planning_state_location(ctx)
    try:
        state = load_plan_review_state(root, task_id)
    except (OSError, TimeoutError, ValueError) as exc:
        return "ERROR: PLAN_REVIEW_STATE_INVALID: " + str(exc)
    wave = plan_review_wave(state, fingerprint)
    review = wave.get("review") if isinstance((wave or {}).get("review"), dict) else {}
    expected_fp = str(review.get("request_fingerprint") or "")
    prior_revise = next((
        item for item in state.get("waves") or []
        if isinstance(item.get("review"), dict)
        and str(item["review"].get("aggregate_signal") or "") == "REVISE_PLAN"
        and str(item.get("request_fingerprint") or "") != fingerprint
        and plan_text_hash and str(item["review"].get("plan_text_hash") or "") == plan_text_hash
    ), None)
    if prior_revise is not None:
        return (
            "ERROR: PLAN_REVIEW_REVISION_REQUIRED: REVISE_PLAN requires changed plan "
            "text as well as a new request fingerprint."
        )
    if not review or expected_fp != fingerprint:
        if review_disposition is not None:
            # UNBINDABLE: no review for this fingerprint — can close nothing; discard
            # pre-launch, run a real review (erroring wedged submissions, v6.65.0/1).
            setattr(ctx, "_plan_disposition_ignored_unbindable", True)
        return None
    if str((wave or {}).get("review_evidence_status") or "") == "pending":
        try:
            wave = _mark_planning_handoffs_consumed(ctx, dict(wave or {}))
            state = load_plan_review_state(root, task_id)
        except (OSError, TimeoutError, ValueError) as exc:
            return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(exc)
        review = wave.get("review") if isinstance(wave.get("review"), dict) else {}
        if str(wave.get("review_evidence_status") or "") != "integrated":
            return (
                "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: stored panel result remains "
                "pending evidence integration."
            )
    audit = plan_review_wave_handoffs(wave or {})
    _capture_late_planning_audit(ctx, audit)
    audit["artifact"] = _persist_planning_handoffs(ctx, audit)
    if audit["artifact"].get("error"):
        return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(audit["artifact"]["error"])
    aggregate = str(review.get("aggregate_signal") or "")
    if aggregate == "REVISE_PLAN":
        return (
            "ERROR: PLAN_REVIEW_REVISION_REQUIRED: this unchanged fingerprint already "
            "received REVISE_PLAN. Change the plan text and call plan_task again; no "
            "duplicate scout or reviewer wave was started."
        )
    if bool(review.get("closed")):
        if review_disposition is not None:
            replayed = _replay_closed_review_disposition(
                review, fingerprint, review_disposition,
            )
            return (
                replayed
                if replayed.startswith("ERROR:")
                else _planning_disposition_warning_note(audit) + replayed
            )
        return _planning_disposition_warning_note(audit) + _render_existing_plan_review(
            review, cached=True,
        )
    if review_disposition is not None:
        return _apply_review_disposition(ctx, audit, review, fingerprint, review_disposition)
    if str(state.get("latest_review_fingerprint") or "") != fingerprint:
        try:
            represented = represent_plan_review(
                root, task_id, fingerprint=fingerprint,
            )
        except (OSError, TimeoutError, ValueError) as exc:
            return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(exc)
        represented_review = (
            represented.get("review")
            if isinstance(represented.get("review"), dict)
            else review
        )
        return _planning_disposition_warning_note(audit) + _render_existing_plan_review(
            represented_review, cached=True,
        )
    return (
        "ERROR: PLAN_REVIEW_DISPOSITION_REQUIRED: this unchanged fingerprint already "
        "received REVIEW_REQUIRED. Re-call plan_task with review_disposition covering "
        f"every finding. fingerprint={fingerprint}; finding_ids="
        + json.dumps([
            item.get("finding_id") for item in (review.get("findings") or [])
            if isinstance(item, dict)
        ], ensure_ascii=False)
    )


def _plan_deadline_skip(ctx: ToolContext, *, emit: bool = False) -> str:
    from ouroboros.config import get_plan_task_deadline_min_sec

    metadata = getattr(ctx, "task_metadata", {})
    metadata = metadata if isinstance(metadata, dict) else {}
    deadline = parse_deadline_ts(metadata.get("deadline_at"))
    if deadline is None:
        return ""
    remaining = (deadline - _planning_now()).total_seconds()
    scaled = max(0.0, remaining / 4.0)
    minimum = get_plan_task_deadline_min_sec()
    if remaining > 0 and scaled >= minimum:
        return ""
    if emit:
        try:
            event_queue = getattr(ctx, "event_queue", None)
            if event_queue is not None:
                event_queue.put_nowait({
                    "type": "plan_task_deadline_skip",
                    "task_id": str(getattr(ctx, "task_id", "") or ""),
                    "remaining_sec": round(remaining, 1),
                    "scaled_ceiling_sec": round(scaled, 1),
                    "min_useful_sec": minimum,
                    "ts": utc_now_iso(),
                })
        except Exception:
            pass
    if remaining <= 0:
        return (
            "PLAN_TASK_SKIPPED_DEADLINE: the task deadline has expired; no new planning "
            "scout or reviewer work was started. Proceed with your own best plan directly; "
            "do not re-call plan_task under this deadline."
        )
    return (
        "PLAN_TASK_SKIPPED_DEADLINE: insufficient time for useful planning — "
        f"remaining {int(remaining)}s gives a swarm window of {int(scaled)}s "
        f"(< {int(minimum)}s useful floor). Proceed with your own best plan directly; "
        "do not re-call plan_task under this deadline."
    )


def _finalize_plan_review_output(
    ctx: ToolContext,
    finalization: _PlanReviewFinalization,
) -> str:
    """Persist the authoritative review result and render its public projection."""
    request = finalization.request
    raw_results = finalization.raw_results
    models = finalization.models
    estimated_tokens = finalization.estimated_tokens
    subject_repo = finalization.subject_repo
    governance_repo = finalization.governance_repo
    planning_handoffs = finalization.planning_handoffs
    state_root = finalization.state_root
    state_task_id = finalization.state_task_id
    request_fingerprint = finalization.request_fingerprint
    degraded_scout_note = finalization.degraded_scout_note
    reviewed_result_hashes = finalization.reviewed_result_hashes
    ctx._last_plan_review_raw_results = raw_results
    ctx._last_plan_review_estimated_tokens = estimated_tokens
    ctx._last_plan_review_subject_root = str(subject_repo)
    ctx._last_plan_review_governance_root = str(governance_repo)
    summary = _summarize_plan_review_results(raw_results)
    aggregate_signal = str(summary["aggregate_signal"])
    review_record = {
        "schema_version": 1,
        "request_fingerprint": request_fingerprint,
        "plan_text_hash": plan_text_fingerprint(request.plan),
        "aggregate_signal": aggregate_signal,
        "findings": list(summary["findings"]),
        "reviewed_at": utc_now_iso(),
        "closed": aggregate_signal == "GREEN",
        "included_task_ids": list(planning_handoffs.get("included_task_ids") or []),
        "omitted_task_ids": [
            str(item.get("task_id") or "")
            for item in (planning_handoffs.get("omissions") or [])
            if isinstance(item, dict) and str(item.get("task_id") or "")
        ],
    }
    if planning_handoffs:
        try:
            wave = record_plan_review_result(
                state_root, state_task_id, fingerprint=request_fingerprint, review=review_record,
                reviewed_result_hashes=reviewed_result_hashes,
            )
        except (OSError, TimeoutError, ValueError) as exc:
            return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
        if str(wave.get("review_evidence_status") or "") == "pending":
            try:
                wave = _mark_planning_handoffs_consumed(ctx, dict(wave))
            except (OSError, TimeoutError, ValueError) as exc:
                return f"ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: {exc}"
        planning_handoffs.update({
            key: value for key, value in plan_review_wave_handoffs(wave).items() if key != "wait"
        })
        artifact = _persist_planning_handoffs(ctx, planning_handoffs)
        if artifact.get("error"):
            return "ERROR: PLAN_REVIEW_STATE_PERSIST_FAILED: " + str(artifact["error"])
        planning_handoffs["artifact"] = artifact
        _capture_late_planning_audit(ctx, planning_handoffs)

    if aggregate_signal == "GREEN":
        next_step = "Proceed with the reviewed plan."
    elif aggregate_signal == "REVIEW_REQUIRED":
        next_step = (
            "Re-call plan_task with this exact unchanged fingerprint and a "
            "review_disposition covering every finding id; that path makes no new LLM call."
        )
    else:
        next_step = (
            "Change the plan text so its fingerprint changes, then call plan_task again. "
            "A disposition cannot override REVISE_PLAN."
        )
    footer = "\n".join([
        "", "## Plan Review Contract", "", f"**Plan fingerprint:** `{request_fingerprint}`",
        next_step, "",
        PLAN_REVIEW_CONTROL_PREFIX + json.dumps(
            {"outcome": aggregate_signal, "closed": aggregate_signal == "GREEN"},
            separators=(",", ":"),
        ),
        f"PLAN_REVIEW_OUTCOME: {aggregate_signal}", f"AGGREGATE: {aggregate_signal}",
    ])
    return (
        degraded_scout_note
        + _planning_disposition_warning_note(planning_handoffs)
        + _format_output(raw_results, models, request.goal, estimated_tokens)
        + "\n\n"
        + footer
    )


async def _run_plan_review_async(
    ctx: ToolContext,
    request: _PlanReviewRequest,
    *,
    planning_handoff_override: tuple[str, str] | None = None,
    additional_context: str = "",
) -> str:
    plan = request.plan
    goal = request.goal
    files_to_touch = request.files_to_touch
    context_level = request.context_level
    context_notes = request.context_notes
    include_tests = request.include_tests
    plan_class = request.plan_class
    review_disposition = request.review_disposition
    try:
        scope = _normalize_plan_scope(request.scope)
    except ValueError as exc:
        return f"ERROR: PLAN_SCOPE_INVALID: {exc}"
    from ouroboros import config as _cfg
    deadline_skip = _plan_deadline_skip(ctx)
    deadline_blocked = bool(deadline_skip)
    try:
        state_root, state_task_id = _planning_state_location(ctx)
    except ValueError as exc:
        return f"ERROR: PLAN_REVIEW_STATE_INVALID: {exc}"
    try:
        has_prior_state = bool(load_plan_review_state(state_root, state_task_id).get("waves"))
    except (OSError, TimeoutError, ValueError) as exc:
        return f"ERROR: PLAN_REVIEW_STATE_INVALID: {exc}"
    if not deadline_blocked or has_prior_state:
        try:
            governance_repo, subject_repo = _resolve_plan_roots(ctx, files_to_touch)
        except ValueError as exc:
            return f"ERROR: PLAN_SUBJECT_ROOT_INVALID: {exc}"
        resolved_class, escalation_note = _resolve_plan_class(ctx, plan_class, files_to_touch)
        if escalation_note:
            ctx.emit_progress_fn(f"📐 plan_task: {escalation_note}")
        try:
            resolved_context_level = _resolve_plan_context_level(context_level, plan_class=resolved_class)
        except ValueError as exc:
            return f"ERROR: {exc}"
        resolved_request = replace(
            request,
            context_level=resolved_context_level,
            plan_class=resolved_class,
            scope=scope,
        )
        request_fingerprint = _plan_request_fingerprint(
            plan=plan,
            goal=goal,
            files_to_touch=files_to_touch,
            context_level=resolved_context_level,
            context_notes=context_notes,
            plan_class=resolved_class,
            scope=scope,
            include_tests=include_tests,
        )
        existing = _reuse_or_disposition_plan_review(
            ctx, request_fingerprint, review_disposition, plan_text_fingerprint(plan)
        )
        if existing is not None:
            return existing
        if not list(_cfg.get_review_models() or []):
            return "ERROR: No review models configured. Set OUROBOROS_REVIEW_MODELS in settings."
        models = _get_review_models()

    if deadline_blocked:
        return _plan_deadline_skip(ctx, emit=True) or deadline_skip
    degraded_scout_note = ""
    planning_handoffs: dict = {}
    reviewed_result_hashes: dict[str, str] = {}
    if planning_handoff_override is not None:
        planning_handoff_raw, planning_handoff_compact = planning_handoff_override
    else:
        swarm = _start_planning_swarm(ctx, resolved_request)
        if not swarm.get("started"):
            return str(swarm.get("error") or "ERROR: plan_task planning swarm failed closed.")
        planning_handoffs = dict(swarm.get("handoffs") or {})
        try:
            reviewed_result_hashes = _reviewed_handoff_hashes(planning_handoffs)
        except ValueError as exc:
            return f"ERROR: PLAN_REVIEW_STATE_INVALID: {exc}"
        planning_handoff_raw = _format_planning_handoffs(planning_handoffs, raw=True)
        planning_handoff_compact = _format_planning_handoffs(planning_handoffs, raw=False)
        degraded_scout_note = (
            "⚠️ DEGRADED PLANNING EVIDENCE: one or more intended scouts produced no usable "
            "handoff before the shared cutoff; reviewers received the complete "
            "omissions manifest.\n\n"
            if swarm.get("degraded_evidence") else ""
        )

    checklist = _load_plan_checklist()
    bible_text = load_governance_doc(governance_repo, "BIBLE.md", on_missing="explicit")
    dev_md = load_governance_doc(governance_repo, "docs/DEVELOPMENT.md", on_missing="explicit")
    arch_md = load_governance_doc(governance_repo, "docs/ARCHITECTURE.md", on_missing="explicit")
    checklists_md = load_governance_doc(governance_repo, "docs/CHECKLISTS.md", on_missing="explicit")
    # v6.61.0 (5.2) doc tiering — a GOVERNANCE-contract change approved by owner quiz 19
    # (DEVELOPMENT.md Core-Governance table + prose updated in the same commit): reviewers
    # of a NON-self_mod plan keep BIBLE + DEVELOPMENT in full but get ARCHITECTURE as the
    # LOSSLESS navigation map (every section + line range, full sections on demand) — an
    # external/creative/research plan needs the self-body's operational map as an index,
    # not ~45K tokens of inline detail. self_mod keeps today's full pack untouched.
    if resolved_class != "self_mod" and arch_md.strip():
        from ouroboros.context_layout import generate_doc_nav_map

        arch_md = generate_doc_nav_map(
            arch_md, title="ARCHITECTURE.md", rel_path="docs/ARCHITECTURE.md"
        )

    ctx.emit_progress_fn("📐 plan_task: reading planned-touch file snapshots…")
    canonical_docs = {
        "BIBLE.md",
        "docs/DEVELOPMENT.md",
        "docs/ARCHITECTURE.md",
        "docs/CHECKLISTS.md",
    }
    head_snapshots = ""
    if files_to_touch:
        head_snapshots = build_head_snapshot_section(subject_repo, files_to_touch)

    system_prompt = _build_system_prompt(
        checklist,
        bible_text,
        dev_md,
        arch_md,
        checklists_md,
        context_level=resolved_context_level,
        plan_class=resolved_class,
    )
    placeholder = "__GENERATED_PLAN_ATLAS_PENDING__"
    user_content, user_stable_len = _build_user_content(
        resolved_request,
        head_snapshots,
        placeholder if resolved_context_level != "minimal" else "",
        "",
    )
    user_content += "\n\n" + _planning_evidence_horizon(
        ctx,
        governance_repo=governance_repo,
        subject_repo=subject_repo,
        scope=scope,
    )
    if planning_handoff_raw:
        user_content += "\n\n" + planning_handoff_raw
    if additional_context:
        user_content += "\n\n" + additional_context
    fixed_prompt_tokens = estimate_tokens(system_prompt + user_content)
    if resolved_context_level != "minimal":
        target_tokens = _plan_context_target_tokens(resolved_context_level)
        ctx.emit_progress_fn(
            f"📐 plan_task: building {resolved_context_level} Generated Plan Review Atlas…"
        )
        try:
            atlas = compile_review_context_atlas(
                ReviewContextAtlasRequest(
                    repo_dir=subject_repo,
                    anchors=tuple(files_to_touch),
                    already_included=frozenset(
                        set(files_to_touch)
                        | (canonical_docs if subject_repo == governance_repo else set())
                    ),
                    fixed_prompt_tokens=fixed_prompt_tokens,
                    target_total_tokens=target_tokens,
                    hard_total_tokens=_PLAN_BUDGET_TOKEN_LIMIT,
                    include_tests=bool(include_tests),
                    title=f"Generated Plan Review Atlas ({resolved_context_level})",
                    drive_root=pathlib.Path(ctx.drive_root),
                )
            )
        except Exception as e:
            return f"ERROR: Failed to build review context atlas: {e}"

        if atlas.status == "budget_exceeded":
            estimated = int((atlas.manifest or {}).get("estimated_total_tokens") or 0)
            return (
                "⚠️ PLAN_REVIEW_SKIPPED: generated repository atlas exceeded hard budget"
                + (f" ({estimated:,} estimated tokens)" if estimated else "")
                + ". Split the plan into a smaller scope or choose a smaller context_level."
            )

        # The Atlas slot is the LAST section of the stable evidence prefix by
        # construction, so substitute the LAST occurrence within that boundary:
        # a wider search would match the placeholder literal quoted by the plan
        # text (dynamic tail) or inlined in a HEAD snapshot earlier in the
        # stable prefix (e.g. a plan touching plan_review.py itself).
        slot = user_content.rfind(placeholder, 0, user_stable_len)
        if slot < 0:
            return "ERROR: Failed to build review context atlas: placeholder missing."
        user_content = user_content[:slot] + atlas.text + user_content[slot + len(placeholder):]
        user_stable_len += len(atlas.text) - len(placeholder)

    estimated_tokens = estimate_tokens(system_prompt + user_content)
    if estimated_tokens > _PLAN_BUDGET_TOKEN_LIMIT and planning_handoff_raw:
        user_content = user_content.replace(planning_handoff_raw, planning_handoff_compact)
        estimated_tokens = estimate_tokens(system_prompt + user_content)
    if estimated_tokens > _PLAN_BUDGET_TOKEN_LIMIT:
        return (
            f"⚠️ PLAN_REVIEW_SKIPPED: assembled prompt too large "
            f"({estimated_tokens:,} estimated tokens, limit {_PLAN_BUDGET_TOKEN_LIMIT:,}). "
            f"Consider reducing files_to_touch or splitting the plan into smaller scopes."
        )

    # Budget admission for the whole reviewer wave (v6.69.0): declining up front
    # beats dying mid-wave with paid partial slots. Fail-open on unknowns.
    _admission = review_wave_budget_gate(
        ctx, surface="plan_review", models=models,
        prompt_chars=len(system_prompt) + len(user_content),
        max_completion_tokens=_PLAN_REVIEW_MAX_TOKENS,
    )
    if _admission is not None:
        return (
            "⚠️ PLAN_REVIEW_SKIPPED_BUDGET: the reviewer wave was declined before "
            f"dispatch — estimated cost ~${_admission.get('estimated_wave_usd')} exceeds "
            f"the remaining root budget ${_admission.get('remaining_usd')} "
            f"(limit ${_admission.get('limit_usd')}). No reviewer was called. "
            "Shrink the plan context, split the plan, or raise the per-task budget."
        )

    ctx.emit_progress_fn(
        f"📐 plan_task: running {len(models)} parallel reviewers "
        f"(context={resolved_context_level}, ~{estimated_tokens:,} tokens each)…"
    )

    raw_results = await _run_plan_review_slots(
        ctx, models, system_prompt, user_content, user_stable_len=user_stable_len,
    )
    return _finalize_plan_review_output(ctx, _PlanReviewFinalization(
        request=request,
        raw_results=raw_results,
        models=models,
        estimated_tokens=estimated_tokens,
        subject_repo=subject_repo,
        governance_repo=governance_repo,
        planning_handoffs=planning_handoffs,
        state_root=state_root,
        state_task_id=state_task_id,
        request_fingerprint=request_fingerprint,
        degraded_scout_note=degraded_scout_note,
        reviewed_result_hashes=reviewed_result_hashes,
    ))


async def _run_plan_review_slots(
    ctx: ToolContext,
    models: list[str],
    system_prompt: str,
    user_content: str,
    user_stable_len: int = 0,
) -> list[dict]:
    from ouroboros.review_substrate import ReviewRequest, ReviewSlot, run_review_request

    slots = [
        ReviewSlot(
            slot_id=f"plan_slot_{idx + 1}",
            model=str(model),
            effort=_PLAN_REVIEW_EFFORT,
            timeout_sec=_PLAN_REVIEW_SLOT_TIMEOUT_SEC,
            max_tokens=_PLAN_REVIEW_MAX_TOKENS,
            temperature=0.2,
            role_hint="plan reviewer",
        )
        for idx, model in enumerate(models)
    ]
    request = ReviewRequest(
        surface="plan_review",
        goal="Review the proposed implementation plan before code is written.",
        messages=build_plan_review_messages(system_prompt, user_content, user_stable_len),
        task_id=str(getattr(ctx, "task_id", "") or "plan_review"),
        call_type="plan_review",
        max_tokens=_PLAN_REVIEW_MAX_TOKENS,
        temperature=0.2,
        no_proxy=True,
    )
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None,
        lambda: run_review_request(
            request,
            slots=slots,
            drive_root=pathlib.Path(ctx.drive_root),
            llm=LLMClient(),
            usage_ctx=ctx,
        ),
    )
    return [_plan_raw_result_from_actor(actor, models[idx] if idx < len(models) else "") for idx, actor in enumerate(result.actors)]


def _plan_raw_result_from_actor(actor: dict, request_model: str) -> dict:
    usage = actor.get("usage") or {}
    text = actor.get("raw_text") or ""
    error = actor.get("error") or ""
    if actor.get("status") not in {"ok", "empty"} and not error:
        error = str(actor.get("status") or "review failed")
    return {
        "model": str(usage.get("resolved_model") or actor.get("model") or request_model),
        "request_model": request_model or actor.get("model") or "",
        "text": text,
        "error": error or None,
        "prompt_ref": actor.get("prompt_ref") or {},
        "response_ref": actor.get("response_ref") or {},
        "tokens_in": usage.get("prompt_tokens", 0),
        "tokens_out": usage.get("completion_tokens", 0),
        "cost": float(usage["cost"]) if usage.get("cost") is not None else None,
    }




_PLAN_CLASSES = ("self_mod", "external", "creative", "research")


def _resolve_plan_class(ctx: ToolContext, plan_class: str, files_to_touch: list) -> tuple[str, str]:
    """v6.61.0 (5.1): resolve the plan's CLASS — the agent declares it LLM-first
    (self_mod | external | creative | research), and the host STRUCTURALLY escalates
    to self_mod when the planned files resolve under the SYSTEM repo (a path fact,
    never keyword matching — P5). self_mod keeps today's full-pack review; the other
    classes get the tiered doc pack (5.2) and task-fit scout framing (5.3).
    Returns (resolved_class, escalation_note)."""
    from ouroboros.tool_access import path_is_relative_to
    from ouroboros.tools.registry import active_repo_dir_for

    declared = str(plan_class or "").strip().lower()
    if declared not in _PLAN_CLASSES:
        declared = ""
    _sys_raw = getattr(ctx, "system_repo_dir", None)
    if _sys_raw is not None and _sys_raw.__class__.__module__.startswith("unittest.mock"):
        _sys_raw = None  # same mock guard active_repo_dir_for uses
    try:
        system_repo = pathlib.Path(_sys_raw or ctx.repo_dir).resolve(strict=False)
    except (TypeError, OSError, ValueError):
        # Unresolvable ctx: fail toward the historically STRICTER class —
        # self_mod keeps the full pack + the explicit context_level contract.
        return "self_mod", ""
    try:
        active = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    except Exception:
        active = system_repo
    touches_system = False
    if files_to_touch:
        if active == system_repo:
            # Relative files_to_touch resolve against the active workspace — here
            # that IS the system repo, so the plan touches the self-body.
            touches_system = True
        else:
            for raw in files_to_touch:
                candidate = pathlib.Path(str(raw or ""))
                resolved = (candidate if candidate.is_absolute() else active / candidate).resolve(strict=False)
                if resolved == system_repo or path_is_relative_to(resolved, system_repo):
                    touches_system = True
                    break
    if touches_system:
        note = "" if (declared in ("", "self_mod")) else (
            f"plan_class escalated {declared!r} -> 'self_mod': files_to_touch resolve "
            "under the Ouroboros system repo (structural fact)."
        )
        return "self_mod", note
    if declared:
        return declared, ""
    # Undeclared: preserve today's behavior for self-repo work; a task planning in
    # an external workspace defaults to the external class.
    return ("external" if active != system_repo else "self_mod"), ""


def _resolve_plan_context_level(raw_level: str, *, plan_class: str = "self_mod") -> str:
    level = str(raw_level or "").strip().lower()
    valid = {"minimal", "localized", "broad", "constitutional"}
    if level not in valid:
        # v6.61.0 (5.2): non-self_mod classes default to `minimal` — the generated
        # Atlas is repo archaeology, which an external/creative/research plan needs
        # only on explicit request. self_mod keeps the explicit-choice contract.
        if not level and plan_class in ("external", "creative", "research"):
            return "minimal"
        allowed = ", ".join(sorted(valid))
        raise ValueError(
            "plan_task requires an explicit context_level chosen by the agent "
            f"({allowed}); do not rely on host-side auto selection."
        )
    return level


def _plan_context_target_tokens(level: str) -> int:
    return {
        "localized": 80_000,
        "broad": 350_000,
        "constitutional": 850_000,
    }.get(str(level or ""), 80_000)


def _classify_reviewer_error(exc: BaseException, model: str) -> str:
    """Return actionable reviewer failure text without swallowing details."""
    import json

    exc_type = type(exc).__name__
    exc_str = str(exc)

    # JSONDecodeError usually means provider returned a non-JSON error body.
    if isinstance(exc, json.JSONDecodeError):
        return (
            f"API error (provider returned non-JSON response body — likely oversized prompt "
            f"or HTTP error from {model}): {exc_str}"
        )

    # Import lazily so the module loads without openai installed.
    try:
        from openai import (
            APIConnectionError,
            APIStatusError,
            BadRequestError,
            RateLimitError,
        )
        if isinstance(exc, RateLimitError):
            return f"Rate limit / quota exceeded for {model} (HTTP 429): {exc_str}"
        if isinstance(exc, BadRequestError):
            return (
                f"Bad request for {model} (HTTP 400 — prompt may be too large "
                f"for this model's context window): {exc_str}"
            )
        if isinstance(exc, APIConnectionError):
            return f"API connection error for {model} (network failure): {exc_str}"
        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", "?")
            return f"API status error {status} for {model}: {exc_str}"
    except ImportError:
        pass

    # Catch-all: preserve the full unknown exception text.
    return f"{exc_type}: {exc_str}"


def _get_review_models() -> list[str]:
    """Return the configured review-model slots (arbitrary N), preserving
    explicit duplicates; fall back to the main model only when nothing is set."""
    from ouroboros import config as _cfg

    models = list(_cfg.get_review_models() or [])
    if not models:
        main = os.environ.get("OUROBOROS_MODEL", _cfg.SETTINGS_DEFAULTS["OUROBOROS_MODEL"])
        models = [main]

    return models  # honor the configured reviewer count


def _load_plan_checklist() -> str:
    """Load the Plan Review Checklist section from CHECKLISTS.md."""
    try:
        return load_checklist_section("Plan Review Checklist")
    except Exception as e:
        log.warning("Could not load Plan Review Checklist: %s", e)
        return ""
