"""Extra pro-mode review gate for protected core patches."""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

from ouroboros.tools.commit_gate import _record_commit_attempt
from ouroboros.tools.parallel_review import (
    aggregate_review_verdict as _aggregate_review_verdict,
    run_parallel_review as _run_parallel_review,
)
from ouroboros.utils import append_jsonl, run_cmd, utc_now_iso


def _emit_core_patch_event(ctx, event_type: str, **payload: Any) -> None:
    try:
        append_jsonl(ctx.drive_logs() / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": event_type,
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            **payload,
        })
    except Exception:
        pass


def run_core_patch_review_gate(
    ctx,
    commit_message: str,
    commit_start: float,
    *,
    protected_paths,
    pre_fingerprint: Dict[str, Any],
    goal: str = "",
    scope: str = "",
    review_rebuttal: str = "",
) -> Optional[Dict[str, Any]]:
    """Run the extra blocking review required for pro-mode core patches."""
    gate_goal = (
        "CORE PATCH REVIEW GATE. This staged diff modifies protected "
        "Ouroboros core/contract/release surfaces. Verify that the patch is "
        "necessary, localized, contract-compatible, and preserves safety and "
        "release invariants."
    )
    if goal:
        gate_goal += f"\n\nOriginal goal:\n{goal}"
    gate_scope = (
        "Protected paths in this staged diff:\n"
        + "\n".join(f"- {p.path} ({p.category})" for p in protected_paths)
        + "\n\nBlock on any real regression, undocumented contract break, "
        "safety invariant weakening, release/bundle provenance drift, or "
        "missing tests for the protected behavior. Advisory-only comments "
        "must not block unless they identify a correctness or safety risk."
    )
    if scope:
        gate_scope += f"\n\nOriginal scope:\n{scope}"

    old_enforcement = os.environ.get("OUROBOROS_REVIEW_ENFORCEMENT")
    os.environ["OUROBOROS_REVIEW_ENFORCEMENT"] = "blocking"
    try:
        review_err, scope_result, triad_block_reason, triad_advisory = _run_parallel_review(
            ctx,
            commit_message,
            goal=gate_goal,
            scope=gate_scope,
            review_rebuttal=review_rebuttal,
        )
        blocked, combined_msg, block_reason, combined_findings, scope_advisory = _aggregate_review_verdict(
            review_err,
            scope_result,
            triad_block_reason,
            triad_advisory,
            ctx,
            commit_message,
            commit_start,
            ctx.repo_dir,
        )
        if scope_advisory:
            advisory_list = getattr(ctx, "_review_advisory", None)
            if isinstance(advisory_list, list):
                advisory_list.extend(scope_advisory)
    finally:
        if old_enforcement is None:
            os.environ.pop("OUROBOROS_REVIEW_ENFORCEMENT", None)
        else:
            os.environ["OUROBOROS_REVIEW_ENFORCEMENT"] = old_enforcement

    scope_status = str(getattr(scope_result, "status", "") or "")
    if not blocked and scope_status != "responded":
        blocked = True
        block_reason = "core_patch_review_incomplete"
        combined_findings = [{
            "item": "core_patch_review_incomplete",
            "severity": "critical",
            "verdict": "FAIL",
            "reason": (
                "Core-patch scope review did not produce a full response "
                f"(status={scope_status or 'missing'}). Protected-surface "
                "commits require a completed extra review gate."
            ),
        }]
        combined_msg = (
            "⚠️ CORE_PATCH_REVIEW_BLOCKED: protected-surface review did not "
            f"complete (scope status={scope_status or 'missing'}). Commit has NOT been created."
        )

    if blocked:
        message = (
            "⚠️ CORE_PATCH_REVIEW_BLOCKED: pro-mode protected-surface review "
            "failed or found blocking issues. Commit has NOT been created.\n\n"
            + str(combined_msg or "")
        )
        _record_commit_attempt(
            ctx,
            commit_message,
            "blocked",
            block_reason="core_patch_review_blocked",
            block_details=message,
            duration_sec=time.time() - commit_start,
            critical_findings=combined_findings,
            phase="core_patch_review",
            pre_review_fingerprint=pre_fingerprint.get("fingerprint", ""),
            fingerprint_status="matched",
            triad_models=getattr(ctx, "_last_triad_models", []),
            scope_model=getattr(ctx, "_last_scope_model", ""),
            triad_raw_results=getattr(ctx, "_last_triad_raw_results", []),
            scope_raw_result=getattr(ctx, "_last_scope_raw_result", {}),
            degraded_reasons=list(getattr(ctx, "_review_degraded_reasons", []) or []),
        )
        try:
            run_cmd(["git", "reset", "HEAD"], cwd=ctx.repo_dir)
        except Exception:
            pass
        return {
            "status": "blocked",
            "message": message,
            "block_reason": "core_patch_review_blocked",
            "pre_fingerprint": pre_fingerprint,
            "post_fingerprint": pre_fingerprint,
        }

    _emit_core_patch_event(
        ctx,
        "core_patch_review_passed",
        protected_paths=[{"path": p.path, "category": p.category} for p in protected_paths],
        pre_review_fingerprint=pre_fingerprint.get("fingerprint", ""),
    )
    return None

