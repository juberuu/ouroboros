"""Skill review verdict aggregation.

This module is deliberately tiny so both ``skill_review`` and
``skill_loader`` can share the same live status calculation without an
import cycle.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


CRITICAL_ITEMS = frozenset({
    "manifest_schema",
    "skill_preflight",
    "permissions_honesty",
    "no_repo_mutation",
    "path_confinement",
    "env_allowlist",
    "inject_chat_minimization",
    "event_subscription_minimization",
    "companion_process_safety",
    "host_token_handling",
})


def aggregate_skill_review_status(
    findings: List[Dict[str, Any]],
    skill_type: str,
    *,
    is_module_widget: bool = False,
    enforcement: Optional[str] = None,
) -> str:
    """Collapse per-reviewer findings into a live execution status."""
    has_critical_fail = False
    has_advisory_fail = False
    is_extension = skill_type == "extension"
    for finding in findings:
        verdict = finding.get("verdict") == "FAIL"
        if not verdict:
            continue
        item = finding.get("item")
        item_is_critical = (
            item in CRITICAL_ITEMS
            or (item == "extension_namespace_discipline" and is_extension)
            or (item == "widget_module_safety" and is_extension)
        )
        if item_is_critical:
            has_critical_fail = True
        else:
            has_advisory_fail = True
    if has_critical_fail:
        return "fail"
    if has_advisory_fail:
        if enforcement is None:
            try:
                from ouroboros.config import get_review_enforcement
                enforcement = get_review_enforcement()
            except Exception:
                enforcement = "blocking"
        return "advisory_pass" if enforcement == "advisory" else "advisory"
    return "pass"


def skill_review_gate(status: str, *, stale: bool = False, enforcement: Optional[str] = None) -> Dict[str, Any]:
    """Structured, agent-facing explanation of whether a review is executable."""
    raw_status = str(status or "").strip().lower()
    if enforcement is None:
        try:
            from ouroboros.config import get_review_enforcement
            enforcement = get_review_enforcement()
        except Exception:
            enforcement = "blocking"
    if raw_status == "pending":
        executable = False
        reason = "review_pending"
        summary = "Review is pending or did not produce an executable verdict."
    elif stale:
        executable = False
        reason = "review_stale"
        summary = "Review is stale for the current skill content; re-run review_skill."
    elif raw_status == "pass":
        executable = True
        reason = "ready"
        summary = "Review is executable: status pass."
    elif raw_status == "advisory_pass":
        if str(enforcement or "").lower() == "advisory":
            executable = True
            reason = "advisory_findings_allowed_by_advisory_enforcement"
            summary = "Review is executable: advisory findings are allowed by advisory enforcement."
        else:
            executable = False
            reason = "review_requires_revalidation_under_blocking_enforcement"
            summary = "Review was executable under advisory enforcement, but blocking enforcement now requires re-review or fixes."
    elif raw_status == "advisory":
        executable = False
        reason = "advisory_findings_under_blocking_enforcement"
        summary = "Review completed but is blocked: advisory findings are not executable under blocking enforcement."
    elif raw_status == "fail":
        executable = False
        reason = "critical_review_fail"
        summary = "Review is blocked: critical findings must be fixed."
    else:
        executable = False
        reason = "review_missing_or_unknown"
        summary = "Review status is missing or unknown; run review_skill."
    return {
        "status": raw_status or "pending",
        "stale": bool(stale),
        "executable_review": bool(executable),
        "blocking_reason": reason,
        "review_enforcement": str(enforcement or "blocking"),
        "summary": summary,
    }
