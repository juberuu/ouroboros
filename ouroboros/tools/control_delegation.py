"""Delegation-budget + in-task project-scoping affordances (v6.37.0).

Extracted from ``ouroboros/tools/control.py`` to keep that dispatcher module under
the module-size hard gate. These are the cyber-racing postmortem additions: the
typed child delegation-budget narrowing (C3.1) and the ``ensure_project_scope``
tool handler (C4.1). ``control.py`` imports both; ``_ensure_project_scope`` reaches
back into ``control._emit_control_event`` lazily (call-time) so there is no import
cycle.
"""

from __future__ import annotations

import pathlib
import threading
import uuid
from typing import Any, Dict

from ouroboros.contracts.task_contract import _bounded_intent_note, normalize_bool
from ouroboros.tools.registry import ToolContext
from ouroboros.utils import utc_now_iso

# FR2: ONE shared from-scratch git tree per task-tree for cooperative acting-subagent
# builds, keyed by root_task_id. Process-local cache so multiple fan-out waves of the
# SAME parent route children into the SAME tree (the parent worker schedules them all).
_COOP_SHARED_ROOTS: Dict[str, str] = {}
_COOP_LOCK = threading.Lock()


def ensure_cooperative_shared_root(ctx: Any, root_task_id: str) -> str:
    """Mint (once per task-tree) ONE shared from-scratch git tree for a cooperative
    acting-subagent build, reusing ``subagent_worktrees.provision_genesis_project``
    (a durable standalone repo under the projects root, outside repo/ and data/).
    Cached by ``root_task_id`` so multiple fan-out waves share ONE tree. Children
    write into it via ``write_surface=external_workspace``; deeper descendants inherit
    it automatically through ``parent_workspace_root``. Returns the tree path, or a
    ``⚠️`` error string for the LLM."""
    key = str(root_task_id or "").strip() or str(getattr(ctx, "task_id", "") or "").strip()
    # Hold the lock across BOTH the cache check AND the mint so two concurrent fan-out
    # waves of the same root cannot each provision a tree (no double-mint / orphan tree).
    # The mint is a one-time `git init` on an empty dir, so serializing it is cheap.
    with _COOP_LOCK:
        cached = _COOP_SHARED_ROOTS.get(key)
        if cached and pathlib.Path(cached).is_dir():
            return cached
        try:
            from ouroboros.subagent_worktrees import provision_genesis_project

            handle = provision_genesis_project(
                repo_dir=ctx.repo_dir,
                task_id=key or uuid.uuid4().hex[:8],
                parent_task_id=str(getattr(ctx, "task_id", "") or ""),
                dir_name=f"coop_{key[:12]}" if key else "coop",
            )
        except Exception as exc:
            return (
                "⚠️ COOP_WORKSPACE_ERROR: could not provision a shared cooperative workspace "
                f"({type(exc).__name__}: {exc}); pass write_root explicitly or schedule read-only children."
            )
        path = str(handle.path)
        _COOP_SHARED_ROOTS[key] = path
        return path


def resolve_cooperative_write_root(
    ctx: Any, requested_surface: str, write_root: str, workspace_root: str, metadata: Dict[str, Any]
) -> tuple[str, str, str]:
    """Resolve the effective acting write_root and the caller profile for a scheduled
    wave. A flat parent (no inherited workspace) requesting external_workspace with no
    write_root builds cooperatively from scratch, so the host mints ONE shared tree.
    Returns ``(effective_write_root, caller_profile, error_or_empty)``. Gated on the
    mutative toggle so a disabled setup falls through to the disabled message rather
    than minting an unused tree."""
    from ouroboros.tool_access import active_tool_profile

    caller_profile = active_tool_profile(ctx)
    effective = write_root
    if (
        requested_surface == "external_workspace"
        and not str(write_root or "").strip()
        and not workspace_root
        and caller_profile != "local_readonly_subagent"
    ):
        from ouroboros.config import get_allow_mutative_subagents

        if get_allow_mutative_subagents():
            key = str((metadata or {}).get("root_task_id") or getattr(ctx, "task_id", "") or "").strip()
            shared = ensure_cooperative_shared_root(ctx, key)
            if isinstance(shared, str) and shared.startswith("⚠️"):
                return "", caller_profile, shared
            effective = shared
    return effective, caller_profile, ""


def _narrow_child_delegation_budget(
    parent_budget: Dict[str, Any],
    *,
    child_depth_remaining: int,
    may_mutate: bool,
    may_fan_out: bool,
    max_children: int,
    intent_note: str,
    parent_is_subagent: bool = True,
) -> Dict[str, Any]:
    """Build a child's delegation_budget that only ever NARROWS within the parent's
    (C3.1): recursion authority (delegate/fan-out) is AND-ed with the parent's and
    max_children is capped to the parent's positive cap, so a parent that disabled
    delegation/fan-out can never hand a child MORE recursion authority than it holds.

    ``may_mutate`` is special: a ROOT task's default budget is may_mutate=False
    ("mutation is opt-in"), which is NOT an explicit read-only denial — so a root
    HONORS the per-call may_mutate grant (the agent explicitly asking for a mutative
    child). Only a SUBAGENT parent's may_mutate gates the child, so a read-only
    subagent cannot escalate by spawning a mutative descendant. (``parent_is_subagent``
    defaults True — the conservative choice for an unspecified caller.)

    Legacy contracts carry no delegation_budget, so a missing parent authority defaults
    to True (unrestricted — pre-C3.1 behavior)."""
    parent_budget = parent_budget if isinstance(parent_budget, dict) else {}
    parent_may_delegate = bool(parent_budget.get("may_delegate", True))
    parent_may_mutate = bool(parent_budget.get("may_mutate", True))
    parent_may_fan_out = bool(parent_budget.get("may_fan_out", True))
    parent_max_children = parent_budget.get("max_children")
    if isinstance(max_children, int) and max_children > 0:
        child_max_children = max_children
        if isinstance(parent_max_children, int) and parent_max_children > 0:
            child_max_children = min(child_max_children, parent_max_children)
    else:
        child_max_children = parent_max_children
    # STRICT boolean parse of the per-call grants (live-subagent contract): a tool
    # call may pass the STRING "false"/"0" — bool("false") is truthy and would
    # silently grant mutation/fan-out, so route through the same normalize_bool the
    # contract uses. The parent_* flags come from a normalized contract (real bools).
    child_may_mutate = normalize_bool(may_mutate)
    if parent_is_subagent:
        child_may_mutate = child_may_mutate and parent_may_mutate
    return {
        "may_delegate": (child_depth_remaining > 0) and parent_may_delegate,
        "may_mutate": child_may_mutate,
        "may_fan_out": normalize_bool(may_fan_out) and parent_may_fan_out,
        "depth_remaining": child_depth_remaining,
        "max_children": child_max_children,
        "intent_note": _bounded_intent_note(
            str(intent_note or "").strip() or str(parent_budget.get("intent_note") or "")
        ),
    }


def child_budget_for_schedule(
    parent_contract: Any,
    *,
    current_depth: int,
    new_depth: int,
    max_depth: int,
    may_mutate: bool,
    may_fan_out: bool,
    max_children: int,
    intent_note: str,
) -> Dict[str, Any]:
    """Resolve a child's delegation_budget at schedule time (C3.1): decrement
    depth_remaining one generation (falling back to the configured max_depth/new_depth
    gap for legacy contracts), then NARROW within the parent. A ROOT scheduler
    (current_depth 0) honors its explicit may_mutate grant; a SUBAGENT scheduler's
    may_mutate gates the child (no read-only escalation)."""
    parent_budget = parent_contract.get("delegation_budget") if isinstance(parent_contract, dict) else {}
    parent_budget = parent_budget if isinstance(parent_budget, dict) else {}
    parent_depth_remaining = parent_budget.get("depth_remaining")
    if isinstance(parent_depth_remaining, int):
        child_depth_remaining = max(0, parent_depth_remaining - 1)
    else:
        child_depth_remaining = max(0, max_depth - new_depth)
    return _narrow_child_delegation_budget(
        parent_budget,
        child_depth_remaining=child_depth_remaining,
        may_mutate=may_mutate,
        may_fan_out=may_fan_out,
        max_children=max_children,
        intent_note=intent_note,
        parent_is_subagent=current_depth > 0,
    )


def _ensure_project_scope(ctx: ToolContext, project_name: str = "", project_id: str = "") -> str:
    """Create (or attach to) a named Ouroboros PROJECT and scope THE CURRENT task to
    it — the in-task structural affordance for "create a project named X" once work
    is already running. promote_chat_to_task only creates a NEW task in a project;
    this binds the task you are ALREADY in (so you don't fall back to a bare mkdir).
    Idempotent for the same project; refuses to re-scope to a different one.
    Subagents inherit the parent's scope and cannot change it.
    """
    # delegation_role lives on the task metadata / contract lineage, NOT as a
    # ToolContext attribute — read it the canonical way.
    _meta = getattr(ctx, "task_metadata", {})
    _meta = _meta if isinstance(_meta, dict) else {}
    _contract = getattr(ctx, "task_contract", {})
    _contract = _contract if isinstance(_contract, dict) else {}
    _lineage = _contract.get("lineage", {}) if isinstance(_contract.get("lineage", {}), dict) else {}
    if str(_meta.get("delegation_role") or _lineage.get("delegation_role") or "").strip() == "subagent":
        return "⚠️ TOOL_ERROR (ensure_project_scope): subagents inherit the parent's project scope and cannot change it."
    from ouroboros.project_facts import (
        explicit_project_id_ok,
        project_id_from_display_name,
        sanitize_project_id,
    )
    from ouroboros.project_naming import clean_model_title

    # Run the agent-supplied name through the SAME lexical cleaner the proactive namer and
    # turn-into-project conversion use (project_naming SSOT) so every project-naming path
    # produces consistent titles (quote/emoji strip, length cap); fall back to the raw value.
    display_name = clean_model_title(project_name) or str(project_name or "").strip()
    explicit = str(project_id or "").strip()
    if explicit:
        if not explicit_project_id_ok(explicit):
            return (
                f"⚠️ TOOL_ARG_ERROR (ensure_project_scope): project_id {explicit!r} is not "
                "filesystem-clean; use lowercase alphanumeric/_/-/. (<=64 chars)"
            )
        pid = sanitize_project_id(explicit)
    elif display_name:
        pid = project_id_from_display_name(display_name)
    else:
        return "⚠️ TOOL_ARG_ERROR (ensure_project_scope): provide project_name (to create/name a project) or project_id (an existing one)."
    if not pid:
        return "⚠️ TOOL_ARG_ERROR (ensure_project_scope): could not derive a project id from the given name."

    current = sanitize_project_id(getattr(ctx, "project_id", "") or "")
    if current:
        if current == pid:
            return f"OK: this task is already scoped to project '{pid}' (no change)."
        return (
            f"⚠️ TOOL_ERROR (ensure_project_scope): this task is already scoped to project "
            f"'{current}'; it cannot be re-scoped to '{pid}'."
        )

    tid = str(getattr(ctx, "task_id", "") or "")
    # Scope the REST of this task immediately so journal_write and per-project
    # knowledge target the project now; the emitted event makes the supervisor
    # create the registry project, bind THIS task durably, and broadcast.
    ctx.project_id = pid
    evt = {
        "type": "ensure_project_scope",
        "task_id": tid,
        "project_id": pid,
        "project_name": display_name,
        "ts": utc_now_iso(),
    }
    # Lazy import avoids a control.py <-> control_delegation.py cycle (control is
    # fully loaded by the time any tool handler runs).
    from ouroboros.tools.control import _emit_control_event

    mode = _emit_control_event(ctx, evt)
    return (
        f"OK: created/attached project '{display_name or pid}' (id={pid}) and scoped this "
        f"task into it ({mode}). journal_write and project knowledge now target this "
        "project; its live progress now routes to the project thread."
    )
