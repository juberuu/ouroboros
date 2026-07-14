"""Parallel triad + scope review orchestration for commit gates."""
from __future__ import annotations

import concurrent.futures as _cf
import hashlib
import logging

from ouroboros.utils import run_cmd
from ouroboros.tools.review_helpers import build_scope_actor_record, format_review_history_entry
from ouroboros.tools.scope_review import (
    run_scope_review,
    ScopeReviewResult,
    _get_scope_model,
)

log = logging.getLogger(__name__)


def _scope_history_entry(scope_result) -> dict:
    """Build scope history while preserving non-PASS epistemic status."""
    parts = []
    if scope_result.critical_findings:
        parts.append(
            "Critical: " + "; ".join(
                (
                    f"{f['item']} ({f.get('obligation_id')})"
                    if f.get("obligation_id") else f["item"]
                )
                for f in scope_result.critical_findings
            )
        )
    if scope_result.advisory_findings:
        parts.append(
            "Advisory: " + "; ".join(
                (
                    f"{f['item']} ({f.get('obligation_id')})"
                    if f.get("obligation_id") else f["item"]
                )
                for f in scope_result.advisory_findings
            )
        )
    status = getattr(scope_result, "status", None) or "responded"
    # Lead with non-responded status so empty findings are not misread as PASS.
    if not parts and status not in ("responded",):
        summary = f"({status})"
    else:
        summary = " | ".join(parts) if parts else "(no findings)"
    return {
        "blocked": scope_result.blocked,
        "status": status,
        "summary": summary,
        "critical_findings": scope_result.critical_findings or [],
        "advisory_findings": scope_result.advisory_findings or [],
    }


def _format_scope_advisory_msg(scope_result) -> str:
    """Format advisory scope findings as a readable message (advisory enforcement path)."""
    parts = []
    if scope_result.critical_findings:
        parts.append("Scope advisory findings (enforcement=advisory):\n" +
                     "\n".join(f"  • {f['item']}: {f.get('reason', '')}"
                                for f in scope_result.critical_findings))
    if scope_result.advisory_findings:
        parts.append("Scope advisory notes:\n" +
                     "\n".join(f"  • {f['item']}: {f.get('reason', '')}"
                                for f in scope_result.advisory_findings))
    return "---\n" + "\n".join(parts) if parts else ""
def run_parallel_review(ctx, commit_message, *, goal="", scope="", review_rebuttal=""):
    """Run triad and scope review concurrently against the staged diff."""
    from ouroboros.tools.review import _run_unified_review

    # Reset forensic fields so prior attempts cannot bleed into early exits.
    ctx._last_scope_model = ""
    ctx._last_triad_raw_results = []
    ctx._last_scope_raw_result = {}
    ctx._last_scope_raw_results = []

    try:
        diff_bytes = run_cmd(["git", "diff", "--cached"], cwd=ctx.repo_dir).encode()
    except Exception:
        diff_bytes = b""
    snapshot_key = hashlib.sha256(diff_bytes).hexdigest()[:16]
    _stored = getattr(ctx, '_scope_review_history', None) or {}
    _scope_history = _stored.get(snapshot_key, []) if isinstance(_stored, dict) else []
    _history_snapshot = list(getattr(ctx, '_review_history', []))

    def _run_scope():
        try:
            try:
                from ouroboros.config import get_scope_review_models

                scope_models = get_scope_review_models()
            except Exception:
                scope_models = [_get_scope_model()]
            scope_models = scope_models or [_get_scope_model()]
            ctx._last_scope_model = ",".join(scope_models)
            def _run_one_scope(model: str):
                # P3 one-pass contract: one substantive scope call per configured
                # actor. Transport retry remains inside the same review substrate;
                # never launch an automatic second degraded review call here.
                return run_scope_review(
                    ctx, commit_message, goal=goal, scope=scope,
                    review_rebuttal=review_rebuttal,
                    review_history=_history_snapshot,
                    scope_review_history=_scope_history,
                    scope_model=model,
                )

            with _cf.ThreadPoolExecutor(max_workers=min(len(scope_models), 4)) as scope_pool:
                futures = [scope_pool.submit(_run_one_scope, model) for model in scope_models]
                results = [future.result() for future in futures]
            ctx._last_scope_raw_results = [
                build_scope_actor_record(
                    result,
                    fallback_model_id=getattr(result, "model_id", "") or model,
                    slot_id=f"scope_slot_{idx + 1}",
                )
                for idx, (result, model) in enumerate(zip(results, scope_models))
            ]
            # Reviewer-slot SSOT applies to scope too (Bible P3): a single configured
            # scope reviewer is honored but recorded as loud durable degraded-trust,
            # and a configured>=2-but-<quorum-responded scope run must never silently
            # pass on "any responded". Only an authoritative `responded` actor counts
            # toward quorum; budget_exceeded/advisory are a structural context-floor
            # skip, not an authoritative responder (so we never over-block them).
            from ouroboros.config import adaptive_quorum
            _scope_statuses = [str(getattr(r, "status", "") or "") for r in results]
            _responded = sum(1 for s in _scope_statuses if s == "responded")
            _required = adaptive_quorum(len(scope_models))
            _single_scope_reviewer = len(scope_models) == 1
            _scope_degraded: list = []
            if _single_scope_reviewer:
                _scope_degraded.append("single_reviewer_no_diversity")
            elif _responded < _required and not any(getattr(r, "blocked", False) for r in results):
                _scope_degraded.append(
                    f"scope_quorum_not_met: responded={_responded} < required={_required}"
                )
            _scope_quorum_manifest = {
                "scope_responded_count": _responded,
                "scope_required_quorum": _required,
                "single_reviewer_no_diversity": _single_scope_reviewer,
                "scope_degraded_reasons": _scope_degraded,
            }
            if len(results) == 1:
                only = results[0]
                only.context_manifest = {**(getattr(only, "context_manifest", {}) or {}), **_scope_quorum_manifest}
                return only
            critical = []
            advisory = []
            parsed_items = []
            blocked_messages = []
            statuses = []
            for result in results:
                statuses.append(getattr(result, "status", ""))
                critical.extend(result.critical_findings or [])
                advisory.extend(result.advisory_findings or [])
                parsed_items.extend(getattr(result, "parsed_items", []) or [])
                if result.blocked and result.block_message:
                    blocked_messages.append(result.block_message)
            blocked = bool(blocked_messages)
            block_messages = list(blocked_messages)
            _qmsg = (
                f"⚠️ SCOPE_QUORUM_NOT_MET: only {_responded} of {len(scope_models)} configured "
                f"scope reviewers returned an authoritative verdict (adaptive quorum {_required}). "
                "Cross-model scope diversity was not achieved this run."
            )
            # Bible P3 negative control: configured>=2 but a PARTIAL authoritative
            # quorum (0 < responded < required) is a loud quorum FAILURE — block vs
            # advisory FOLLOWS owner enforcement (never hardcode a block). A
            # zero-responded run is a structural floor/skip handled by the
            # per-result status, so it stays advisory-only here.
            partial_quorum_shortfall = (
                not _single_scope_reviewer and 0 < _responded < _required and not blocked
            )
            if partial_quorum_shortfall:
                from ouroboros.config import get_review_enforcement
                if get_review_enforcement() == "blocking":
                    blocked = True
                    block_messages.append(_qmsg)
            # Surface any non-blocking shortfall LOUDLY (advisory, never a silent
            # clean pass) and persist it in the manifest below.
            if _scope_degraded and not _single_scope_reviewer and not blocked:
                advisory.append({
                    "verdict": "FAIL",
                    "severity": "advisory",
                    "item": "scope_quorum_not_met",
                    "reason": _qmsg,
                })
            return ScopeReviewResult(
                blocked=blocked,
                block_message="\n\n".join(block_messages),
                critical_findings=critical,
                advisory_findings=advisory,
                parsed_items=parsed_items,
                raw_text="\n\n".join(str(r.raw_text or "") for r in results),
                model_id=",".join(scope_models),
                # Quorum-aware: only an authoritative quorum yields "responded".
                # A partial quorum (some — but <required — responded) is a loud
                # "degraded_quorum"; zero responded preserves the joined raw
                # statuses so downstream budget_exceeded/skipped detection holds.
                status=(
                    "blocked" if blocked
                    else "responded" if _responded >= _required
                    else "degraded_quorum" if _responded > 0
                    else ",".join(statuses)
                ),
                prompt_chars=sum(int(r.prompt_chars or 0) for r in results),
                tokens_in=sum(int(r.tokens_in or 0) for r in results),
                tokens_out=sum(int(r.tokens_out or 0) for r in results),
                cost_usd=sum(float(r.cost_usd or 0.0) for r in results),
                context_manifest={
                    "scope_models": scope_models,
                    "actor_count": len(results),
                    **_scope_quorum_manifest,
                    "actors": [
                        {
                            "slot_id": f"scope_slot_{idx + 1}",
                            "model": model,
                            "context_manifest": getattr(result, "context_manifest", {}) or {},
                        }
                        for idx, (result, model) in enumerate(zip(results, scope_models))
                    ],
                },
            )
        except Exception as e:
            log.warning("Scope review raised unexpected exception: %s", e)
            result = ScopeReviewResult(
                blocked=True,
                block_message=f"⚠️ SCOPE_REVIEW_BLOCKED: Scope review failed — {e}\nFix the issue and retry.",
                model_id=getattr(ctx, "_last_scope_model", "") or _get_scope_model(),
                status="error",
            )
            ctx._last_scope_raw_results = [
                build_scope_actor_record(
                    result,
                    fallback_model_id=getattr(ctx, "_last_scope_model", ""),
                    slot_id="scope_slot_error",
                )
            ]
            return result

    # Snapshot advisory state before threads mutate it.
    _advisory_snapshot_before = list(getattr(ctx, '_review_advisory', []))
    with _cf.ThreadPoolExecutor(max_workers=2) as pool:
        triad_fut = pool.submit(_run_unified_review, ctx, commit_message,
                                review_rebuttal=review_rebuttal, goal=goal, scope=scope)
        scope_fut = pool.submit(_run_scope)
        try:
            review_err = triad_fut.result()
        except Exception as e:
            log.warning("Triad review raised unexpected exception: %s", e)
            review_err = (
                f"⚠️ REVIEW_BLOCKED: Triad review crashed — {e}\nFix the issue and retry."
            )
            ctx._last_review_block_reason = 'infra_failure'
            ctx._last_review_critical_findings = []
        triad_block_reason = getattr(ctx, '_last_review_block_reason', 'critical_findings')
        triad_advisory_post = list(getattr(ctx, '_review_advisory', []))
        triad_advisory = [a for a in triad_advisory_post if a not in _advisory_snapshot_before]
        try:
            scope_result = scope_fut.result()
        except Exception as e:
            log.warning("Scope future raised unexpected exception: %s", e)
            scope_result = ScopeReviewResult(
                blocked=True,
                block_message=f"⚠️ SCOPE_REVIEW_BLOCKED: Scope review future crashed — {e}\nFix the issue and retry.",
                model_id=getattr(ctx, "_last_scope_model", "") or _get_scope_model(),
                status="error",
            )
            ctx._last_scope_raw_results = [
                build_scope_actor_record(
                    scope_result,
                    fallback_model_id=getattr(ctx, "_last_scope_model", ""),
                    slot_id="scope_slot_error",
                )
            ]

    if scope_result is not None:
        updated = _scope_history + [_scope_history_entry(scope_result)]
        existing = getattr(ctx, '_scope_review_history', None) or {}
        if not isinstance(existing, dict):
            existing = {}
        existing[snapshot_key] = updated
        ctx._scope_review_history = existing
        # Canonical scope actor record for durable CommitAttemptRecord persistence.
        raw_results = list(getattr(ctx, "_last_scope_raw_results", []) or [])
        if raw_results:
            ctx._last_scope_raw_result = {
                "status": getattr(scope_result, "status", ""),
                "model_id": getattr(scope_result, "model_id", "") or getattr(ctx, "_last_scope_model", ""),
                "context_manifest": getattr(scope_result, "context_manifest", {}) or {},
                "raw_results": raw_results,
                "raw_text": getattr(scope_result, "raw_text", ""),
                "critical_findings": getattr(scope_result, "critical_findings", []) or [],
                "advisory_findings": getattr(scope_result, "advisory_findings", []) or [],
            }
        else:
            ctx._last_scope_raw_result = build_scope_actor_record(
                scope_result,
                fallback_model_id=getattr(ctx, "_last_scope_model", ""),
            )
    else:
        ctx._last_scope_raw_result = {}

    return review_err, scope_result, triad_block_reason, triad_advisory


def aggregate_review_verdict(review_err, scope_result, triad_block_reason, triad_advisory,
                              ctx, commit_message, commit_start, repo_dir):
    """Aggregate triad/scope result and return block state plus advisory items."""
    _combined_blocked = False
    _combined_messages = []
    _combined_findings = []
    _scope_advisory_items = []

    if scope_result is not None:
        for f in (scope_result.critical_findings or []):
            item = {
                "severity": "critical",
                "tag": "scope",
                "item": str(f.get("item", "") or ""),
                "reason": str(f.get("reason", "") or ""),
                "verdict": "FAIL",
            }
            if f.get("obligation_id"):
                item["obligation_id"] = str(f.get("obligation_id"))
            _scope_advisory_items.append(item)
        for f in (scope_result.advisory_findings or []):
            item = {
                "severity": "advisory",
                "tag": "scope",
                "item": str(f.get("item", "") or ""),
                "reason": str(f.get("reason", "") or ""),
                "verdict": "FAIL",
            }
            if f.get("obligation_id"):
                item["obligation_id"] = str(f.get("obligation_id"))
            _scope_advisory_items.append(item)

    if review_err:
        _combined_blocked = True
        _combined_messages.append(review_err)
        _combined_findings.extend(getattr(ctx, '_last_review_critical_findings', []))
    if scope_result is not None:
        if scope_result.blocked:
            _combined_blocked = True
            _combined_messages.append(scope_result.block_message)
            _combined_findings.extend(scope_result.critical_findings or [])
        elif scope_result.advisory_findings or scope_result.critical_findings:
            _advisory_msg = _format_scope_advisory_msg(scope_result)
            if _advisory_msg and _combined_blocked:
                _combined_messages.append(_advisory_msg)

    if not _combined_blocked:
        return False, None, '', _combined_findings, _scope_advisory_items

    if review_err and (scope_result is None or not scope_result.blocked):
        block_reason = triad_block_reason
    elif scope_result is not None and scope_result.blocked and not review_err:
        block_reason = "scope_blocked"
    else:
        block_reason = triad_block_reason

    if len(_combined_messages) > 1:
        combined_msg = "\n\n".join(_combined_messages)
        if review_err and scope_result is not None and scope_result.blocked:
            combined_msg += "\n\n---\n⚠️ Note: Both triad review AND scope review found issues (shown above)."
    else:
        combined_msg = _combined_messages[0]

    if triad_advisory and not review_err:
        adv_text = "\n".join(
            f"  ⚠️ Advisory: {format_review_history_entry(a)}"
            for a in triad_advisory
        )
        combined_msg += f"\n\n---\nTriad advisory findings:\n{adv_text}"

    return True, combined_msg, block_reason, _combined_findings, _scope_advisory_items
