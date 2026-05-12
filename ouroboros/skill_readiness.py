"""Unified skill readiness checks shared by loop and runtime gates."""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ouroboros.skill_review_status import skill_review_gate


@dataclass
class SkillReadiness:
    """Execution/finalization readiness plus human-readable blockers."""

    ready: bool
    blockers: List[str] = field(default_factory=list)
    agent_fixable_blockers: List[str] = field(default_factory=list)
    owner_action_blockers: List[str] = field(default_factory=list)
    review_gate: Dict[str, Any] = field(default_factory=dict)
    grant_status: Dict[str, Any] = field(default_factory=dict)


def skill_readiness_for_execution(
    drive_root: pathlib.Path,
    skill: Any,
    *,
    require_enabled: bool = True,
    require_grants: bool = True,
) -> SkillReadiness:
    """Return the single readiness verdict for skill execution/finalization."""
    blockers: List[str] = []
    agent_fixable: List[str] = []
    owner_action: List[str] = []

    if getattr(skill, "load_error", ""):
        msg = f"load_error={skill.load_error!r}"
        blockers.append(msg)
        agent_fixable.append(msg)

    stale = skill.review.is_stale_for(skill.content_hash)
    gate = skill_review_gate(skill.review.status, stale=stale)
    if stale:
        blockers.append("review_stale")
        agent_fixable.append("review_stale")
    elif not gate.get("executable_review"):
        reason = str(gate.get("blocking_reason") or "review_not_executable")
        msg = f"review_not_executable:{reason}"
        blockers.append(msg)
        agent_fixable.append(msg)

    if require_enabled and not getattr(skill, "enabled", False):
        blockers.append("skill_disabled")
        owner_action.append("skill_disabled")

    grants: Dict[str, Any] = {}
    if require_grants:
        from ouroboros.skill_loader import grant_status_for_skill

        grants = grant_status_for_skill(pathlib.Path(drive_root), skill)
        if not grants.get("all_granted", True):
            missing_keys = grants.get("missing_keys") or []
            missing_permissions = grants.get("missing_permissions") or []
            msg = f"missing_grants:keys={missing_keys},permissions={missing_permissions}"
            blockers.append(msg)
            owner_action.append(msg)

    return SkillReadiness(
        ready=not blockers,
        blockers=blockers,
        agent_fixable_blockers=agent_fixable,
        owner_action_blockers=owner_action,
        review_gate=gate,
        grant_status=grants,
    )
