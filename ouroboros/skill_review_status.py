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
