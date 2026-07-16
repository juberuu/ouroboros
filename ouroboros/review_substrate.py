"""Shared multi-review substrate.

This module is the common cognitive primitive for migrated review surfaces and
the contract target for remaining legacy immune-system reviews. Slot identity is
separate from model identity, so duplicate model IDs are valid independent
reviewer slots.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import pathlib
import queue
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from ouroboros.config import get_review_models
from ouroboros.llm import LLMClient
from ouroboros.observability import new_call_id, persist_call, redact_projection
from ouroboros.provider_models import provider_for_model
from ouroboros.triad_review import extract_json_array
from ouroboros.usage_accounting import (
    UsageAccountingError,
    UsageScope,
    current_usage_scope,
    physical_attempt_limit,
    usage_scope,
)
from ouroboros.utils import sanitize_tool_result_for_log, truncate_review_artifact


def review_repo_dirs_for(ctx: Any) -> tuple[pathlib.Path, pathlib.Path]:
    """Return validated ``(governance, subject)`` roots for plan/scope review."""
    from ouroboros.tools.registry import active_repo_dir_for

    workspace_raw = getattr(ctx, "workspace_root", None)
    workspace = pathlib.Path(workspace_raw) if isinstance(workspace_raw, (str, pathlib.Path)) else None
    if workspace is not None and not str(getattr(ctx, "workspace_mode", "") or "").strip():
        raise ValueError("workspace_root is set without workspace_mode")
    system_raw = getattr(ctx, "system_repo_dir", None)
    system = pathlib.Path(system_raw) if isinstance(system_raw, (str, pathlib.Path)) else None
    governance = (system or pathlib.Path(getattr(ctx, "repo_dir"))).resolve(strict=False)
    subject = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
    if not governance.is_dir() or not subject.is_dir():
        raise ValueError(f"unavailable governance/subject root: {governance} / {subject}")
    return governance, subject


@dataclass(frozen=True)
class ReviewSlot:
    slot_id: str
    model: str
    effort: str = "medium"
    timeout_sec: float = 300
    max_tokens: int = 16_384
    temperature: float | None = None
    role_hint: str = ""


@dataclass
class ReviewRequest:
    surface: str
    goal: str
    scope: str = ""
    subject: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    checklist: str = ""
    policy: Dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    messages: List[Dict[str, Any]] = field(default_factory=list)
    call_type: str = ""
    max_tokens: int | None = None
    temperature: float | None = None
    no_proxy: bool = False


@dataclass
class ReviewActorRecord:
    slot_id: str
    model: str
    status: str
    raw_text: str = ""
    parsed: Any = None
    # Per-actor parsed verdict (PASS/FAIL/DEGRADED/UNKNOWN). Carried here so the
    # objective axis can aggregate outcome_tier from only the actors that
    # CONTRIBUTED to a quorum PASS, instead of re-deriving the verdict downstream.
    signal: str = ""
    error: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    prompt_ref: Dict[str, Any] = field(default_factory=dict)
    response_ref: Dict[str, Any] = field(default_factory=dict)
    duration_sec: float = 0.0
    # Compact typed truth for task-result/event projection.  Raw model output
    # remains in the existing private audit record; these fields prevent UI
    # consumers from conflating a transport failure, malformed JSON, and a
    # valid semantic DEGRADED verdict.
    transport_status: str = ""
    parse_status: str = ""
    semantic_verdict: str = ""
    provider: str = ""
    actor_role: str = ""
    coverage: Dict[str, Any] = field(default_factory=dict)
    # Participation is independent of agreement with the aggregate: every
    # contract-valid PASS/FAIL response counts, while enforcement_impact says
    # whether that participant supports completion or vetoes it.
    quorum_contribution: bool = False
    reason: str = ""
    enforcement_impact: str = ""


@dataclass
class ReviewRunResult:
    request: Dict[str, Any]
    actors: List[Dict[str, Any]]
    parsed_findings: List[Dict[str, Any]]
    aggregate_signal: str
    degraded: bool = False
    degraded_reasons: List[str] = field(default_factory=list)
    # Bible P3: a single configured reviewer is honored but the lost cross-model
    # diversity is recorded LOUDLY and DURABLY here (centralized for every surface
    # that runs through ReviewCoordinator — acceptance, etc. — so a one-slot review
    # can never quietly look like an ordinary multi-reviewer PASS).
    single_reviewer_no_diversity: bool = False
    panel_id: str = ""


# Thin ReviewProfile hardness levels (Bible P3 DRY): the behavior is carried by
# request.policy; these name the three surfaces so callers/reviewers describe
# hardness consistently without a parallel pipeline.
HARDNESS_ADVISORY_VISIBLE = "advisory_visible"  # fed back as a compact capsule, never blocks
HARDNESS_LABEL_ONLY = "label_only"              # recorded on the objective axis, not shown
HARDNESS_HARD_GATE = "hard_gate"                # blocking commit/scope immune gate (unchanged)

# Tier vocabulary SSOT lives in outcomes.py; reuse it so a future tier rename
# cannot silently desync the capsule from the objective axis.
from ouroboros.outcomes import OUTCOME_TIER_BEST_EFFORT, OUTCOME_TIER_BLOCKED, OUTCOME_TIER_SOLVED

_TIER_ORDER = {OUTCOME_TIER_SOLVED: 0, OUTCOME_TIER_BEST_EFFORT: 1, OUTCOME_TIER_BLOCKED: 2}


def _transport_error_status(error: Any) -> str:
    """Classify transport failures without depending on a non-empty message."""
    error_type = type(error).__name__ if isinstance(error, BaseException) else ""
    error_text = str(error or "")
    if (
        isinstance(error, TimeoutError)
        or "timeout" in error_type.casefold()
        or "timeout" in error_text.casefold()
        or "timed out" in error_text.casefold()
    ):
        return "timeout"
    return "provider_transport_error"


def _public_review_reason(value: Any, *, limit: int) -> str:
    """Redact model-controlled reason text before publishing a bounded preview."""
    text = str(value or "")
    if not text:
        return ""
    return truncate_review_artifact(str(redact_projection(text).value), limit=limit)


def _review_actor_projection(actor: Any, surface: str) -> Dict[str, Any]:
    row = actor if isinstance(actor, dict) else asdict(actor)
    parsed = row.get("parsed") if isinstance(row.get("parsed"), (dict, list)) else None
    usage = row.get("usage") if isinstance(row.get("usage"), dict) else {}
    explicit_parse = str(row.get("parse_status") or "")
    semantic = str(row.get("semantic_verdict") or "").upper()
    if not semantic and isinstance(parsed, dict):
        semantic = str(parsed.get("verdict") or parsed.get("status") or "").upper()
    if not semantic:
        semantic = str(row.get("signal") or "").upper()
    valid = (
        explicit_parse != "malformed"
        and parsed is not None
        and semantic in {"PASS", "FAIL", "DEGRADED"}
    )
    error = str(row.get("error") or "")
    transport = str(row.get("transport_status") or "")
    if not transport:
        transport = (
            "success" if str(row.get("status") or "") in {"ok", "empty"}
            else _transport_error_status(error)
        )
    criteria = parsed.get("criteria_used") if isinstance(parsed, dict) else []
    criteria = criteria if isinstance(criteria, list) else []
    if isinstance(parsed, dict):
        parsed_findings = parsed.get("findings")
    elif isinstance(parsed, list):
        parsed_findings = parsed
    else:
        parsed_findings = []
    parsed_findings = (
        [item for item in parsed_findings if isinstance(item, dict)]
        if isinstance(parsed_findings, list)
        else []
    )
    reason = str(row.get("reason") or "")
    if not reason and isinstance(parsed, dict):
        reason = str(parsed.get("summary") or parsed.get("reason") or "")
    if not reason and isinstance(parsed, list):
        for item in parsed_findings:
            reason = str(
                item.get("summary")
                or item.get("reason")
                or item.get("evidence")
                or item.get("item")
                or item.get("recommendation")
                or ""
            )
            if reason:
                break
    reason = reason or error or ("Reviewer response was malformed or absent." if not valid else "")
    model = str(usage.get("resolved_model") or row.get("model") or "")
    provider = str(usage.get("provider") or row.get("provider") or "")
    if not provider:
        provider = provider_for_model(model) if model else "unknown"
    outcome_tier = (
        str(parsed.get("outcome_tier") or "").strip().lower()
        if isinstance(parsed, dict)
        else ""
    )
    if outcome_tier not in {
        OUTCOME_TIER_SOLVED, OUTCOME_TIER_BEST_EFFORT, OUTCOME_TIER_BLOCKED,
    }:
        outcome_tier = ""
    return {
        "slot_id": str(row.get("slot_id") or ""), "model": model, "provider": provider,
        "actor_role": str(row.get("actor_role") or f"{surface} reviewer"),
        "transport_status": transport,
        "parse_status": explicit_parse or ("valid" if valid else "malformed"),
        "semantic_verdict": semantic if valid else "",
        "outcome_tier": outcome_tier if valid else "",
        "coverage": {
            "criteria_total": len(criteria),
            "findings": len(parsed_findings),
        },
        "quorum_contribution": bool(row.get("quorum_contribution")),
        "reason": _public_review_reason(reason, limit=500),
        "enforcement_impact": str(row.get("enforcement_impact") or "abstains"),
    }


def _review_enforcement_impact(run: Dict[str, Any]) -> str:
    if str(run.get("enforcement_impact") or ""):
        return str(run["enforcement_impact"])
    request = run.get("request") if isinstance(run.get("request"), dict) else {}
    hardness = str((request.get("policy") or {}).get("hardness") or "")
    signal = str(run.get("aggregate_signal") or "").upper()
    if str(run.get("authority") or "") == "agent_advisory" or hardness == HARDNESS_ADVISORY_VISIBLE:
        return "advisory"
    if signal == "PASS":
        return "allows_completion"
    return "blocks_completion" if signal == "FAIL" and hardness == HARDNESS_HARD_GATE else "degrades_completion"


def _review_panel_id(request: ReviewRequest, actors: List[ReviewActorRecord]) -> str:
    seed = {
        "surface": request.surface,
        "task_id": request.task_id,
        "actors": [
            [actor.slot_id, actor.model, actor.response_ref]
            for actor in actors
        ],
    }
    digest = hashlib.sha256(
        json.dumps(seed, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"panel_{digest[:16]}"


def build_review_binding(
    *,
    candidate: str,
    evidence: Dict[str, Any],
    fence_token_or_state: Any,
) -> Dict[str, Any]:
    """Build the exact host-panel identity without introducing another ledger."""
    from ouroboros.review_evidence import task_acceptance_evidence_revision

    candidate_hash = hashlib.sha256(str(candidate or "").encode("utf-8")).hexdigest()
    evidence_revision = task_acceptance_evidence_revision(evidence)
    fence_value = (
        json.dumps(fence_token_or_state, sort_keys=True, separators=(",", ":"), default=str)
        if isinstance(fence_token_or_state, (dict, list, tuple))
        else str(fence_token_or_state or "direct_context")
    )
    fence_hash = hashlib.sha256(fence_value.encode("utf-8")).hexdigest()
    binding_payload = {
        "candidate_hash": candidate_hash,
        "evidence_revision": evidence_revision,
        "fence_hash": fence_hash,
    }
    binding_hash = hashlib.sha256(
        json.dumps(binding_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **binding_payload,
        "binding_hash": binding_hash,
        "panel_id": f"panel_{binding_hash[:16]}",
    }


def compact_review_projection(review_runs: Any) -> Dict[str, Any]:
    """Project existing audit runs without copying raw prompts or responses."""
    panels: List[Dict[str, Any]] = []
    for index, raw_run in enumerate(review_runs or []):
        if not isinstance(raw_run, dict):
            continue
        request = raw_run.get("request") if isinstance(raw_run.get("request"), dict) else {}
        surface = str(request.get("surface") or "review")
        actors = [_review_actor_projection(actor, surface) for actor in (raw_run.get("actors") or []) if isinstance(actor, dict)]
        policy = request.get("policy") if isinstance(request.get("policy"), dict) else {}
        min_successful = max(1, int(policy.get("min_successful_slots") or 1))
        contributing = sum(1 for actor in actors if actor["quorum_contribution"])
        transport_statuses = [actor["transport_status"] for actor in actors]
        transport = (
            "success" if transport_statuses and all(s == "success" for s in transport_statuses)
            else ("partial" if "success" in transport_statuses else (
                "timeout" if transport_statuses and all(s == "timeout" for s in transport_statuses)
                else "provider_transport_error"
            ))
        )
        reasons = raw_run.get("degraded_reasons") if isinstance(raw_run.get("degraded_reasons"), list) else []
        panel: Dict[str, Any] = {
            "panel_id": str(raw_run.get("panel_id") or f"panel_{index + 1}"),
            "surface": surface,
            "authority": str(raw_run.get("authority") or "unspecified"),
            "aggregate_signal": str(raw_run.get("aggregate_signal") or "UNKNOWN").upper(),
            "transport_status": str(raw_run.get("transport_status") or transport),
            "parse_status": str(raw_run.get("parse_status") or (
                "valid" if actors and all(a["parse_status"] == "valid" for a in actors) else "malformed"
            )),
            "coverage": {
                "actors_configured": len(actors),
                "transport_success": sum(1 for actor in actors if actor["transport_status"] == "success"),
                "parse_valid": sum(1 for actor in actors if actor["parse_status"] == "valid"),
                "quorum_contributing": contributing,
            },
            "quorum": {"required": min_successful, "contributed": contributing, "configured": len(actors)},
            "reason": _public_review_reason(
                str(raw_run.get("reason") or "; ".join(str(item) for item in reasons)
                    or f"aggregate_{str(raw_run.get('aggregate_signal') or 'unknown').lower()}"),
                limit=800,
            ),
            "enforcement_impact": _review_enforcement_impact(raw_run),
            "actors": actors,
            "superseded": bool(raw_run.get("superseded_by_revision")),
        }
        for key in (
            "candidate_hash", "evidence_revision", "fence_hash", "binding_hash",
        ):
            if raw_run.get(key) not in (None, ""):
                panel[key] = str(raw_run.get(key))
        panels.append(panel)
    return {"panels": panels}


def _criteria_have_supported_evidence(criteria: Any) -> bool:
    return bool(
        isinstance(criteria, list)
        and criteria
        and all(
            isinstance(item, dict)
            and bool(str(item.get("criterion") or "").strip())
            and str(item.get("status") or "").strip().lower() == "supported"
            and bool(item.get("evidence_refs"))
            for item in criteria
        )
    )


def _contributing_actors(result: ReviewRunResult) -> List[Dict[str, Any]]:
    """Actors whose verdict CONTRIBUTED to the aggregate, so a parse-degraded or
    non-responsive slot cannot inject a tier / coach / finding into a clean quorum
    result (Bible P3: one degraded slot must not poison the aggregate — the exact
    class the split-participation gate was built to avoid). For aggregate PASS only
    PASS actors speak; for FAIL only FAIL actors; for a DEGRADED/UNKNOWN aggregate
    only the cleanly-parsed PASS/FAIL actors may speak (never the degraded ones)."""
    actors = [a for a in (getattr(result, "actors", None) or []) if isinstance(a, dict)]
    agg = str(getattr(result, "aggregate_signal", "") or "").upper()
    if agg in ("PASS", "FAIL"):
        return [a for a in actors if str(a.get("signal", "")).upper() == agg]
    return [a for a in actors if str(a.get("signal", "")).upper() in ("PASS", "FAIL")]


def aggregate_outcome_tier(result: ReviewRunResult) -> str:
    """Worst-tier-wins across the actors that CONTRIBUTED to the aggregate verdict."""
    worst, worst_rank = "", -1
    for actor in _contributing_actors(result):
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        tier = str((parsed or {}).get("outcome_tier") or "").strip().lower() if isinstance(parsed, dict) else ""
        rank = _TIER_ORDER.get(tier, -1)
        if rank > worst_rank:
            worst_rank, worst = rank, tier
    return worst


def task_acceptance_is_clean(result: Any) -> bool:
    """Whether a task-acceptance verdict satisfies the release-clean contract."""
    if (
        str(getattr(result, "aggregate_signal", "") or "").upper() != "PASS"
        or bool(getattr(result, "degraded", False))
    ):
        return False
    contributing = _contributing_actors(result)
    if not contributing:
        return False
    request = getattr(result, "request", {})
    policy = request.get("policy") if isinstance(request, dict) else {}
    require_evidence = bool(
        isinstance(policy, dict) and policy.get("require_criterion_evidence")
    )
    for actor in contributing:
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        if not isinstance(parsed, dict) or str(parsed.get("outcome_tier") or "").lower() != OUTCOME_TIER_SOLVED:
            return False
        if require_evidence:
            if not _criteria_have_supported_evidence(parsed.get("criteria_used")):
                return False
    return True


def dissent_findings(result: ReviewRunResult, *, limit: int = 1) -> List[str]:
    """Compact dissent bullets from NON-contributing minority reviewers (v6.54.4).

    A cleanly-parsed reviewer whose verdict differs from the aggregate AND who
    carries a CONCRETE recommendation/alternative contributes ONE verbatim
    "[DISSENT — slot N]: ..." line. Not a veto — the aggregate stands; this ends
    the class where an aggregate-PASS silently discarded a minority FAIL whose
    concrete recommendation was correct (GAIA 3cef3a44). A DELIBERATE minority
    DEGRADED — the reviewer's own parsed verdict (the prompt's "cannot judge →
    return DEGRADED and explain" branch, which is exactly what the 3cef3a44
    reviewer returned) — may dissent too, but only on the strength of a concrete
    findings[].recommendation. Parse-fail placeholders (parsed=None),
    contract-demoted PASSes (their parsed verdict stays PASS — they agree with
    the aggregate), and coach-only DEGRADED stay excluded (no clean dissenting
    signal). ONE bullet by design (plan decision #13) — the first concrete
    dissenter speaks."""
    agg = str(getattr(result, "aggregate_signal", "") or "").upper()
    contributing_ids = {str(a.get("slot_id", "")) for a in _contributing_actors(result)}
    out: List[str] = []
    for actor in (getattr(result, "actors", None) or []):
        if not isinstance(actor, dict) or len(out) >= limit:
            continue
        slot_id = str(actor.get("slot_id", ""))
        signal = str(actor.get("signal", "")).upper()
        if slot_id in contributing_ids or signal == agg:
            continue
        parsed = actor.get("parsed") if isinstance(actor.get("parsed"), dict) else {}
        deliberate_degraded = (
            signal == "DEGRADED"
            and str(parsed.get("verdict") or "").strip().upper() == "DEGRADED"
        )
        if signal not in ("PASS", "FAIL") and not deliberate_degraded:
            continue
        recommendation = ""
        for finding in (parsed.get("findings") or []):
            if isinstance(finding, dict):
                recommendation = str(finding.get("recommendation") or "").strip()
                if recommendation:
                    break
        if not recommendation and not deliberate_degraded:
            recommendation = str(parsed.get("completion_coach") or "").strip()
        if not recommendation:
            continue  # a bare contrary verdict with no concrete alternative is noise
        compact = " ".join(recommendation.split())
        if len(compact) > 300:
            compact = compact[:300].rstrip() + "…"
        out.append(f"[DISSENT — {slot_id} said {signal}]: check this before finalizing — {compact}")
    return out


def build_improvement_capsule(result: ReviewRunResult) -> str:
    """Compact, anti-derailment "Final improvement note" fed back to the agent:
    tier + exact-deduplicated actionable findings + one completion_coach, framed as optional
    suggestions. Returns "" when there is nothing actionable. The full
    ReviewRunResult stays on the objective axis / trace; the agent sees only this
    capsule, so it does not rewrite its deliverable into a meta-essay about the
    review (the failure mode that made the host-forced path label-only).

    Tier, coach, and bullets are drawn ONLY from the actors that contributed to the
    aggregate verdict, so a single parse-degraded slot cannot inject a blocking note
    into an otherwise-clean quorum PASS."""
    aggregate_signal = str(getattr(result, "aggregate_signal", "") or "").upper()
    contributing = _contributing_actors(result)
    # A semantic DEGRADED verdict abstains from quorum, but a concrete finding is
    # still an owner-approved correction rail for the required+blocking re-drive.
    # Transport/parse placeholders and contract-demoted PASS/FAIL actors remain
    # excluded: only an explicitly parsed verdict=DEGRADED may supply ANY capsule
    # content (tier, coach, or finding) when the aggregate itself is DEGRADED.
    deliberate_degraded = [
        actor
        for actor in (getattr(result, "actors", None) or [])
        if (
            aggregate_signal == "DEGRADED"
            and isinstance(actor, dict)
            and str(actor.get("signal") or "").upper() == "DEGRADED"
            and isinstance(actor.get("parsed"), dict)
            and str(actor["parsed"].get("verdict") or "").strip().upper() == "DEGRADED"
        )
    ]
    eligible_actors = deliberate_degraded if aggregate_signal == "DEGRADED" else contributing
    eligible_slots = {str(actor.get("slot_id", "")) for actor in eligible_actors}
    tier = ""
    tier_rank = -1
    for actor in eligible_actors:
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        actor_tier = (
            str(parsed.get("outcome_tier") or "").strip().lower()
            if isinstance(parsed, dict)
            else ""
        )
        actor_rank = _TIER_ORDER.get(actor_tier, -1)
        if actor_rank > tier_rank:
            tier_rank, tier = actor_rank, actor_tier
    coach = ""
    for actor in eligible_actors:
        parsed = actor.get("parsed") if isinstance(actor, dict) else None
        if isinstance(parsed, dict) and not coach:
            coach = str(parsed.get("completion_coach") or "").strip()
        if coach:
            break
    bullets: List[str] = []
    seen_bullets: set[str] = set()
    for finding in (getattr(result, "parsed_findings", None) or []):
        if not isinstance(finding, dict):
            continue
        # Only findings from a contributing actor may surface in the capsule.
        if str(finding.get("slot_id", "")) not in eligible_slots:
            continue
        text = str(finding.get("recommendation") or finding.get("item") or "").strip()
        # Exact normalized deduplication only.  Do not introduce semantic
        # clustering or another findings authority for the improvement loop.
        dedup_key = " ".join(text.split())
        if text and dedup_key not in seen_bullets:
            seen_bullets.add(dedup_key)
            bullets.append(text)
    # A SOLVED review carries a (contract-required) completion_coach, but a coach
    # alone must NOT force a revise round on an already-solved deliverable — that
    # would re-loop EVERY clean required review. The capsule is actionable only
    # when there are real findings to act on OR the tier itself is incomplete
    # (best_effort/blocked). The coach is then included as the next step.
    dissent = dissent_findings(result)
    # A coach alone stays non-actionable for a clean SOLVED PASS, but it is the
    # bounded correction rail for a contributing FAIL.  The coordinator admits a
    # task-acceptance FAIL only when this function can return such a rail.
    actionable = (
        bool(bullets)
        or bool(dissent)
        or (
            aggregate_signal == "FAIL"
            and bool(coach)
        )
        or tier in (OUTCOME_TIER_BEST_EFFORT, OUTCOME_TIER_BLOCKED)
    )
    if not actionable:
        return ""
    lines = [f"[Final improvement note] Reviewer assessment: {tier or result.aggregate_signal}."]
    # Dissent rides ON TOP of the capsule (v6.54.4): same anti-derailment frame,
    # never a veto — a minority reviewer with a concrete recommendation is a
    # "check this before finalizing" pointer, not a re-litigation of the verdict.
    lines += dissent
    lines += [f"- {b}" for b in bullets]
    if coach:
        lines.append(f"Highest-value next step: {coach}")
    lines.append(
        "Revise the deliverable only if it genuinely improves the result; otherwise produce "
        "your normal final answer. Do not mention this review or the reviewer unless the user asked. "
        "The assessment tier above is an internal ledger label — never emit an internal ledger "
        "identifier as the deliverable itself."
    )
    return "\n".join(lines)


def reviewer_slots(models: List[str] | None = None, *, effort: str = "medium", role_hint: str = "") -> List[ReviewSlot]:
    raw_models = models if models is not None else get_review_models()
    return [
        ReviewSlot(slot_id=f"slot_{idx + 1}", model=str(model), effort=effort, role_hint=role_hint)
        for idx, model in enumerate(raw_models or [])
        if str(model or "").strip()
    ]


def _render_prompt(request: ReviewRequest, slot: ReviewSlot) -> str:
    evidence = json.dumps(request.evidence, ensure_ascii=False, indent=2, default=str)
    refs = json.dumps(request.evidence_refs, ensure_ascii=False, indent=2, default=str)
    policy = json.dumps(request.policy, ensure_ascii=False, indent=2, default=str)
    classify_tier = bool(request.policy.get("classify_outcome_tier"))
    # The tier keys belong in the REQUIRED key list, not trailing prose — models
    # honor the explicit "Return JSON with keys" list and otherwise drop them,
    # which silently kills the best_effort/completion-coach lexicon.
    tier_keys = (
        ', outcome_tier ("solved"|"best_effort"|"blocked_with_evidence"), completion_coach'
        if classify_tier
        else ""
    )
    # For task acceptance the reviewer makes its derived acceptance criteria
    # VISIBLE — recorded per-actor in the review trace / objective axis (M4) so
    # "for whom we review" is auditable. Reviewer reasoning, not a new
    # authoritative gate (criteria live in actors[].parsed, not a separate phase).
    criteria_key = (
        ', criteria_used (the acceptance criteria you re-derived from the full goal narrative '
        'and checked, as [{criterion, status (supported|missing|partial|rejected), evidence_refs}]; evidence_refs must name concrete '
        'host-attested receipts/artifacts/tool results for every contributing criterion)'
        if request.surface == "task_acceptance"
        else ""
    )
    tier_rules = (
        "outcome_tier classifies the CURRENT deliverable and completion_coach is the single "
        "highest-value change that would move it one tier up. Never classify solved unless the "
        "claimed result is actually verified by the evidence — your veto over false success "
        "claims is the point of this review. A real partial deliverable with honestly marked "
        "gaps is best_effort, not a failure. "
        if classify_tier
        else ""
    )
    acceptance_rules = (
        "For TASK ACCEPTANCE: do not accept a 'solved' claim on assertion alone. Re-derive the "
        "acceptance criteria from the FULL goal/spec narrative yourself (including prose context, "
        "requirements implied outside bullet lists, and any named user-facing interface), enumerate "
        "all affected components/surfaces you expect the deliverable to cover. If "
        "`task_contract.acceptance_claims` is present, treat those "
        "claim/surface/support entries as structured hints, not as an oracle: verify them against "
        "the full goal and use `acceptance_support_refs` as the host-built support map. The claim's "
        "`support` text names expected evidence; it is NOT evidence unless a receipt/tool/artifact/source "
        "ref actually supports it. "
        "Record in criteria_used which claims were supported, missing, partial, or rejected, and why. "
        "Require per-criterion evidence: an EXECUTED check that MIRRORS what the real grader would run "
        "(the actual test/command and its observed output) — not a narrative that it passes. "
        "If the evidence covers only part of the affected surface list, do not classify solved; "
        "name the uncovered criteria/surfaces in findings or completion_coach. "
        "EVIDENCE INDEPENDENCE: a passing test is only credible if it is not graded by the "
        "agent's own hand. From the diff and tool trace, identify which test/check files the "
        "agent CREATED or MODIFIED this turn versus which were pre-existing or grader-owned; if "
        "the only passing evidence comes from tests the agent wrote or edited this same turn, "
        "treat the success claim as UNVERIFIED (outcome_tier at most best_effort, never solved) "
        "and completion_coach must name an independent check (the pre-existing suite, the grader "
        "command, or a behavior the agent did not also author). State in your summary which "
        "evidence you judged independent. "
        "VISIBLE UI EVIDENCE: when the deliverable changes a user-visible interface, require "
        "evidence that at least one relevant real consumer flow was opened in an available browser "
        "and the rendered result was actually inspected with vision. A screenshot file or attachment "
        "without evidence of visual inspection is insufficient for solved. The implementer chooses "
        "states, viewports, and additional engines according to task risk; mobile and WebKit are not "
        "universal requirements, and an unavailable optional engine alone is not degradation. If "
        "visual evidence the implementer judged necessary was unavailable, require an honest "
        "best_effort/degraded result that names the gap. "
        "ENVIRONMENT vs DELIVERABLE: a task_environment_error, round-budget exhaustion, sandbox "
        "auto-evaluation, or provider/runtime fault is NOT itself an agent failure — judge "
        "whether the requested artifact/answer was produced before the environment terminated; "
        "do not FAIL a correct deliverable for an environment-imposed limit, note it as context. "
        "METRIC-GROUNDED EVIDENCE: when the task states an explicit success metric, contains a "
        "worked example, or warns that grading uses held-out cases, treat an existence-only "
        "(artifact_observation) or substring-only verification receipt as INSUFFICIENT for solved — "
        "require evidence that the metric/example is actually met (an exact/exact_line/json_equals "
        "receipt, or the metric value in the check output). ANTI-CHEAT: credible verification uses "
        "ONLY public task info (instruction text, embedded examples, installed oracles, the agent's "
        "own independent checks); if the evidence came from reading a hidden /tests/ dir, "
        "solution.sh, copied verifier code, or an online answer, treat the success claim as "
        "UNVERIFIED. "
        "PROCESS, NOT ONLY OUTCOME: the packet includes a `tool_trajectory` (HOW the task was "
        "solved) and a first-class `verification_summary`. Audit the process — if the agent used "
        "the wrong tool, went the wrong direction, ignored its OWN red verification "
        "(`verification_summary.unreconciled_red`, or a RED `latest_status`), grounded on a check "
        "whose exit code may be MASKED (`verification_summary.check_exit_masking_unreconciled` — a "
        "`| tail`/`grep`/`|| true` pipeline can report exit 0 over a real failure, so that green is "
        "weak evidence), or the final claim "
        "is not supported by the trajectory, say so: a deliverable that looks superficially "
        "correct but was reached the wrong way, or that contradicts the agent's own checks, is at "
        "most best_effort, and completion_coach must name the process fix. PROVENANCE: every "
        "evidence block is tagged in `__provenance__` (host_attested / agent_supplied / "
        "tool_result / artifact / hidden_or_restricted) — weigh host_attested over agent_supplied, "
        "and NEVER credit a success claim to `hidden_or_restricted` evidence (a benchmark/test leak). "
        if request.surface == "task_acceptance"
        else ""
    )
    return (
        "You are an independent Ouroboros reviewer slot.\n"
        f"Surface: {request.surface}\n"
        f"Slot: {slot.slot_id}\n"
        f"Role hint: {slot.role_hint or 'general reviewer'}\n\n"
        "Review goal:\n"
        f"{request.goal}\n\n"
        "Declared scope:\n"
        f"{request.scope or '(not specified)'}\n\n"
        "Subject:\n"
        f"{request.subject}\n\n"
        "Checklist / acceptance criteria:\n"
        f"{request.checklist or '(none supplied)'}\n\n"
        "Evidence refs:\n"
        f"{refs}\n\n"
        "Evidence packet:\n"
        f"{evidence}\n\n"
        "Policy:\n"
        f"{policy}\n\n"
        f"Return JSON with keys: verdict (PASS|FAIL|DEGRADED){tier_keys}{criteria_key}, findings "
        "([{severity, item, evidence, recommendation}]), and summary. "
        + tier_rules
        + acceptance_rules
        + "If you cannot judge because evidence is missing, return DEGRADED and explain."
    )


def _request_messages(request: ReviewRequest, slot: ReviewSlot) -> List[Dict[str, Any]]:
    if request.messages:
        return [dict(message) if isinstance(message, dict) else {"role": "user", "content": str(message)} for message in request.messages]
    return [{"role": "user", "content": _render_prompt(request, slot)}]


def _messages_char_count(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else message
        if isinstance(content, list):
            total += sum(len(str(block.get("text", block))) if isinstance(block, dict) else len(str(block)) for block in content)
        else:
            total += len(str(content or ""))
    return total


def _extract_fenced_json(text: str) -> Any:
    """Best-effort parse of a fenced/embedded JSON object or array from model output.

    Reviewers often wrap their verdict in a ```json ... ``` fence; a fenced JSON
    OBJECT (e.g. {"verdict":"PASS","findings":[]}) would otherwise fail json.loads
    and be missed by the array-only extractor, producing a false DEGRADED signal.
    """
    if "```" not in text:
        return None
    for chunk in text.split("```"):
        candidate = chunk.strip()
        if candidate.startswith("json"):
            candidate = candidate[4:].strip()
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except Exception:
            continue
        if isinstance(obj, (dict, list)):
            return obj
    return None


def _parse_findings(raw_text: str) -> tuple[Any, List[Dict[str, Any]], str]:
    text = str(raw_text or "").strip()
    parsed: Any = None
    findings: List[Dict[str, Any]] = []
    signal = "UNKNOWN"
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = _extract_fenced_json(text)
        if parsed is None:
            extracted = extract_json_array(text)
            if extracted is None:
                # Keep non-JSON output untruncated; reviewer raw_text is still useful.
                return None, [], "DEGRADED"
            parsed = extracted
    if isinstance(parsed, dict):
        signal = str(parsed.get("verdict") or parsed.get("status") or "UNKNOWN").upper()
        raw_findings = parsed.get("findings") or []
        if isinstance(raw_findings, list):
            findings = [item for item in raw_findings if isinstance(item, dict)]
    elif isinstance(parsed, list):
        findings = [item for item in parsed if isinstance(item, dict)]
        verdicts = {str(item.get("verdict") or item.get("status") or "").upper() for item in findings}
        if "FAIL" in verdicts:
            signal = "FAIL"
        elif "PASS" in verdicts:
            signal = "PASS"
        elif "DEGRADED" in verdicts:
            signal = "DEGRADED"
        else:
            signal = "UNKNOWN"
    return parsed, findings, signal


class ReviewCoordinator:
    def __init__(
        self,
        *,
        llm: LLMClient | None = None,
        drive_root: pathlib.Path | None = None,
        usage_ctx: Any = None,
    ):
        self.llm = llm or LLMClient()
        self.drive_root = pathlib.Path(drive_root) if drive_root is not None else pathlib.Path("../data")
        self.usage_ctx = usage_ctx

    def run(self, request: ReviewRequest, slots: List[ReviewSlot]) -> ReviewRunResult:
        if not slots:
            return ReviewRunResult(
                request=asdict(request),
                actors=[],
                parsed_findings=[],
                aggregate_signal="DEGRADED",
                degraded=True,
                degraded_reasons=["no_review_slots"],
                panel_id=_review_panel_id(request, []),
            )

        result_queue: "queue.Queue[ReviewActorRecord]" = queue.Queue()
        started_slots: List[ReviewSlot] = []
        base_scope = current_usage_scope() or UsageScope()
        usage_meta = (
            getattr(self.usage_ctx, "task_metadata", {})
            if self.usage_ctx is not None
            else {}
        )
        if not isinstance(usage_meta, dict):
            usage_meta = {}
        task_id = str(request.task_id or base_scope.task_id or "")
        root_task_id = str(
            usage_meta.get("root_task_id") or base_scope.root_task_id or task_id
        )
        budget_root = (
            usage_meta.get("budget_drive_root")
            or getattr(self.usage_ctx, "budget_drive_root", "")
            or base_scope.drive_root
            or self.drive_root
        )
        if base_scope.global_limit_usd is not None:
            global_limit = base_scope.global_limit_usd
        else:
            try:
                configured_global_limit = float(os.environ.get("TOTAL_BUDGET", "0") or 0)
                global_limit = configured_global_limit if configured_global_limit > 0 else None
            except (TypeError, ValueError):
                global_limit = None
        if base_scope.root_limit_usd is not None:
            root_limit = base_scope.root_limit_usd
        else:
            try:
                configured_root_limit = float(
                    os.environ.get("OUROBOROS_PER_TASK_COST_USD", "0") or 0
                )
                root_limit = configured_root_limit if configured_root_limit > 0 else None
            except (TypeError, ValueError):
                root_limit = None
        review_usage_scope = UsageScope(
            drive_root=budget_root,
            task_id=task_id,
            root_task_id=root_task_id,
            parent_task_id=str(usage_meta.get("parent_task_id") or base_scope.parent_task_id or ""),
            category=f"{request.surface}_review",
            source="review_substrate",
            global_limit_usd=global_limit,
            root_limit_usd=root_limit,
        )

        def _start_slot(slot: ReviewSlot) -> None:
            started_slots.append(slot)

            def _worker() -> None:
                try:
                    with usage_scope(review_usage_scope):
                        result_queue.put(self._run_slot(request, slot))
                except Exception as exc:
                    result_queue.put(self._error_actor(request, slot, f"{type(exc).__name__}: {exc}"))

            thread = threading.Thread(
                target=_worker,
                name=f"ouroboros-review-{request.surface}-{slot.slot_id}",
                daemon=True,
            )
            thread.start()

        for slot in slots:
            _start_slot(slot)

        actors: List[ReviewActorRecord] = []
        slot_timeout = max(0.001, max(float(slot.timeout_sec or 1) for slot in slots))
        deadline = time.monotonic() + slot_timeout
        while len(actors) < len(slots):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                actors.append(result_queue.get(timeout=remaining))
            except queue.Empty:
                break

        seen = {actor.slot_id for actor in actors}
        started_ids = {slot.slot_id for slot in started_slots}
        for slot in slots:
            if slot.slot_id not in seen:
                if slot.slot_id in started_ids:
                    actors.append(self._error_actor(request, slot, f"Timeout after {slot.timeout_sec:g}s"))
                else:
                    actors.append(self._error_actor(request, slot, "Not started before reviewer timeout budget expired"))
        slot_order = {slot.slot_id: idx for idx, slot in enumerate(slots)}
        slots_by_id = {slot.slot_id: slot for slot in slots}
        actors.sort(key=lambda actor: slot_order.get(actor.slot_id, len(slot_order)))

        all_findings: List[Dict[str, Any]] = []
        # Split participation faults (a slot errored / timed out / returned empty)
        # from parse-degraded (a slot produced a DEGRADED verdict or unparseable
        # text). Only a participation fault fail-closes: a single Markdown/non-JSON
        # slot must NOT poison a clean quorum PASS (the old `degraded_reasons` gate
        # over-degraded honest 2-of-3 PASS reviews).
        actor_errors: List[str] = []
        parse_degraded: List[str] = []
        fail_count = 0
        pass_count = 0
        # When tier classification is required, the contract is ENFORCED before an
        # actor contributes to quorum. A tier-less PASS is non-responsive. A task-
        # acceptance FAIL contributes only when it carries a bounded correction rail;
        # a bare veto must not terminalize Required+Blocking with nothing to improve.
        classify_tier = bool(
            request.surface == "task_acceptance"
            and (request.policy or {}).get("classify_outcome_tier")
        )
        require_criterion_evidence = bool(
            request.surface == "task_acceptance"
            and (request.policy or {}).get("require_criterion_evidence")
        )
        _valid_tiers = {"solved", "best_effort", "blocked_with_evidence"}
        # A SOLVED task-acceptance PASS need not carry a tier-up coach. Commit/scope
        # use distinct surfaces and retain their own hard-gate semantics.
        is_advisory = (
            request.surface == "task_acceptance"
            or str((request.policy or {}).get("hardness") or "") == HARDNESS_ADVISORY_VISIBLE
        )
        for actor in actors:
            if actor.status == "error":
                actor_errors.append(f"{actor.slot_id}:{actor.error}")
            elif actor.status != "ok":
                actor_errors.append(f"{actor.slot_id}:{actor.status}")
            parsed, findings, signal = _parse_findings(actor.raw_text)
            actor.parsed = parsed
            actor.signal = signal
            slot = slots_by_id.get(actor.slot_id)
            actor.actor_role = (
                str(getattr(slot, "role_hint", "") or "").strip()
                or f"{request.surface} reviewer"
            )
            truth = _review_actor_projection(actor, request.surface)
            for key in (
                "model", "transport_status", "parse_status", "semantic_verdict", "provider",
                "coverage", "reason",
            ):
                setattr(actor, key, truth[key])
            all_findings.extend({**item, "slot_id": actor.slot_id, "model": actor.model} for item in findings)
            # The required-tier contract needs BOTH a valid outcome_tier AND a
            # non-empty completion_coach (both are required JSON keys); a PASS
            # missing either is non-responsive to the contract.
            _tier = str(parsed.get("outcome_tier") or "").strip().lower() if isinstance(parsed, dict) else ""
            _criteria = parsed.get("criteria_used") if isinstance(parsed, dict) else None
            _criteria_ok = _criteria_have_supported_evidence(_criteria)
            contract_ok = (
                _tier in _valid_tiers
                and (
                    bool(str((parsed or {}).get("completion_coach") or "").strip())
                    # Advisory carve-out: a SOLVED deliverable has no tier-up step, so an
                    # empty coach must NOT demote it to DEGRADED.
                    or (is_advisory and _tier == "solved")
                )
                and (not require_criterion_evidence or _criteria_ok)
            )
            if signal == "FAIL":
                # A task-acceptance FAIL is authoritative only when it obeys the
                # tier contract and carries a bounded correction rail.  A bare
                # veto cannot terminalize Required+Blocking with nothing the
                # agent can improve; keep the raw FAIL in parsed for forensics,
                # but make the actor abstain exactly like other contract failures.
                _has_concrete_finding = any(
                    isinstance(item, dict)
                    and bool(str(item.get("recommendation") or item.get("item") or "").strip())
                    for item in findings
                )
                _parsed_obj = parsed if isinstance(parsed, dict) else {}
                _has_correction_rail = (
                    bool(str(_parsed_obj.get("completion_coach") or "").strip())
                    or _has_concrete_finding
                    or _tier in {"best_effort", "blocked_with_evidence"}
                )
                if classify_tier and (
                    _tier not in _valid_tiers or not _has_correction_rail
                ):
                    parse_degraded.append(
                        f"{actor.slot_id}:fail_missing_tier_or_correction_rail"
                    )
                    actor.signal = "DEGRADED"
                    actor.parse_status = "malformed"
                    actor.semantic_verdict = ""
                    actor.reason = (
                        "Reviewer response violated the required outcome-tier or "
                        "correction-rail contract."
                    )
                else:
                    fail_count += 1
            elif signal == "PASS" and classify_tier and not contract_ok:
                parse_degraded.append(
                    f"{actor.slot_id}:missing_tier_coach_or_criterion_evidence"
                )
                # A contract-degraded PASS did NOT contribute to quorum, so its
                # recorded signal must be non-contributing too — else _contributing_
                # actors (and the objective-axis tier collector) would still let it
                # inject a tier/coach/finding (e.g. a PASS carrying a blocked tier +
                # empty coach) into the clean quorum capsule. Demote to DEGRADED;
                # the raw verdict stays in actor.parsed for forensics.
                actor.signal = "DEGRADED"
                actor.parse_status = "malformed"
                actor.semantic_verdict = ""
                actor.reason = (
                    "Reviewer response violated the required outcome-tier, coach, "
                    "or criterion-evidence contract."
                )
            elif signal == "PASS":
                pass_count += 1
            elif signal == "DEGRADED":
                parse_degraded.append(f"{actor.slot_id}:degraded")
        min_successful = max(1, int((request.policy or {}).get("min_successful_slots") or 1))
        fail_closed_on_errors = bool((request.policy or {}).get("fail_closed_on_errors"))
        degraded_reasons = actor_errors + parse_degraded
        # Task acceptance is conservative: any valid contributing FAIL vetoes.
        # DEGRADED/parse-failed actors abstain, while PASS still needs the adaptive
        # quorum supplied by the caller.  Commit/scope semantics remain unchanged.
        fail_threshold = 1
        if fail_count >= fail_threshold:
            aggregate = "FAIL"
        elif pass_count >= min_successful and not (
            fail_closed_on_errors and actor_errors and request.surface != "task_acceptance"
        ):
            aggregate = "PASS"
        else:
            aggregate = "DEGRADED"
            # Honest flag: DEGRADED must always carry a reason. Insufficient quorum
            # is itself the reason.
            if not degraded_reasons:
                degraded_reasons.append(
                    f"quorum_not_met: pass_count={pass_count} < min_successful={min_successful}"
                )
        # Bible P3 (centralized): a single configured slot is honored but the lost
        # cross-model diversity is recorded loudly + durably on EVERY surface that
        # runs through the coordinator, independent of the verdict (does NOT flip
        # the aggregate — block-vs-advisory still follows the caller's enforcement).
        single_reviewer = len(slots) == 1
        if single_reviewer and "single_reviewer_no_diversity" not in degraded_reasons:
            degraded_reasons = degraded_reasons + ["single_reviewer_no_diversity"]
        participating_ids = {
            actor.slot_id
            for actor in actors
            if str(actor.signal or "").upper() in {"PASS", "FAIL"}
        }
        for actor in actors:
            actor.quorum_contribution = actor.slot_id in participating_ids
            if not actor.quorum_contribution:
                actor.enforcement_impact = "abstains"
            elif str(actor.signal or "").upper() == "FAIL":
                actor.enforcement_impact = "veto"
            else:
                actor.enforcement_impact = "supports_pass"
        return ReviewRunResult(
            request=asdict(request),
            actors=[asdict(actor) for actor in actors],
            parsed_findings=all_findings,
            # `degraded` tracks the aggregate so the review axis (which also reads
            # this flag) does not mark a quorum PASS as degraded over a single
            # parse-degraded slot.
            aggregate_signal=aggregate,
            degraded=(aggregate == "DEGRADED"),
            degraded_reasons=degraded_reasons,
            single_reviewer_no_diversity=single_reviewer,
            panel_id=_review_panel_id(request, actors),
        )

    def _error_actor(self, request: ReviewRequest, slot: ReviewSlot, error: str) -> ReviewActorRecord:
        call_id = new_call_id(f"review_{request.surface}_{slot.slot_id}_error")
        base_call_type = request.call_type or f"{request.surface}_review"
        messages = _request_messages(request, slot)
        prompt_ref: Dict[str, Any] = {}
        response_ref: Dict[str, Any] = {}
        try:
            prompt_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_prompt",
                call_type=f"{base_call_type}_prompt",
                payload={"request": asdict(request), "slot": asdict(slot), "messages": messages},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "synthetic": True},
            )
        except Exception:
            prompt_ref = {}
        try:
            response_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_error",
                call_type=f"{base_call_type}_error",
                payload={"error": sanitize_tool_result_for_log(error)},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "status": "error", "synthetic": True},
            )
        except Exception:
            response_ref = {}
        return ReviewActorRecord(
            slot_id=slot.slot_id,
            model=slot.model,
            status="error",
            error=sanitize_tool_result_for_log(error),
            prompt_ref=prompt_ref,
            response_ref=response_ref,
        )

    def _run_slot(self, request: ReviewRequest, slot: ReviewSlot) -> ReviewActorRecord:
        messages = _request_messages(request, slot)
        call_id = new_call_id(f"review_{request.surface}_{slot.slot_id}")
        base_call_type = request.call_type or f"{request.surface}_review"
        prompt_ref: Dict[str, Any] = {}
        response_ref: Dict[str, Any] = {}
        start = time.time()
        try:
            prompt_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_prompt",
                call_type=f"{base_call_type}_prompt",
                payload={"request": asdict(request), "slot": asdict(slot), "messages": messages},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model},
            )
        except Exception:
            prompt_ref = {}
        if request.surface == "task_acceptance" and request.evidence.get("__immutable_core_overflow__"):
            raw_text = json.dumps({
                "verdict": "DEGRADED",
                "findings": [],
                "summary": (
                    "Immutable owner requirements do not fit the acceptance evidence "
                    "budget; no requirement was silently truncated."
                ),
            })
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_response",
                    call_type=f"{base_call_type}_response",
                    payload={"message": {"content": raw_text}, "usage": {}},
                    manifest={
                        "surface": request.surface, "slot_id": slot.slot_id,
                        "model": slot.model, "status": "degraded_core_overflow",
                        "physical_attempts": 0,
                    },
                )
            except Exception:
                response_ref = {}
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="ok",
                raw_text=raw_text,
                prompt_ref=prompt_ref,
                response_ref=response_ref,
                duration_sec=round(time.time() - start, 3),
            )
        try:
            chat_kwargs = {
                "messages": messages,
                "model": slot.model,
                "reasoning_effort": slot.effort,
                "max_tokens": int(request.max_tokens or slot.max_tokens),
                "temperature": request.temperature if request.temperature is not None else slot.temperature,
                "no_proxy": bool(request.no_proxy),
                # Bound the TRANSPORT read timeout to the slot's logical timeout so a stalled
                # provider connection fails fast (and is retried / recorded as a timeout actor)
                # instead of hanging on the 3600s default read — which left the slot thread
                # blocked and the whole review process unable to exit. The outer queue/wait_for
                # timeout governs the LOGIC; this governs the SOCKET.
                "timeout": float(slot.timeout_sec) if slot.timeout_sec else None,
            }
            chat = getattr(self.llm, "chat", None)
            p3_actor = request.surface in {"multi_model_review", "scope_review"}
            actor_attempts = 2 if p3_actor else 1
            # Acceptance already owns the same two-send rail. P3 now reuses it for
            # one actor-local retry while every other review surface keeps its
            # existing single invocation. The prompt, slot, and model never change.
            attempt_rail = (
                physical_attempt_limit(2)
                if request.surface == "task_acceptance" or p3_actor
                else contextlib.nullcontext()
            )
            with attempt_rail:
                for actor_attempt in range(actor_attempts):
                    try:
                        if callable(chat):
                            msg, usage = chat(**chat_kwargs)
                        else:
                            msg, usage = asyncio.run(self.llm.chat_async(**chat_kwargs))
                        # A provider can yield a null/non-object message on a
                        # zero-body response. Treat it exactly like empty content:
                        # retry once on P3, then preserve the fail-closed empty actor.
                        raw_text = (
                            str(msg.get("content") or "")
                            if isinstance(msg, dict)
                            else ""
                        )
                    except UsageAccountingError:
                        # Budget/ledger/physical-rail failures are not transport
                        # transients and must remain fail-closed without another send.
                        raise
                    except Exception:
                        if actor_attempt + 1 < actor_attempts:
                            continue
                        raise
                    if raw_text.strip() or actor_attempt + 1 >= actor_attempts:
                        break
            self._emit_usage(request, slot, usage, prompt_chars=_messages_char_count(messages))
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_response",
                    call_type=f"{base_call_type}_response",
                    payload={"message": msg, "usage": usage},
                    manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model},
                )
            except Exception:
                response_ref = {}
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="ok" if raw_text.strip() else "empty",
                raw_text=raw_text,
                usage=usage,
                prompt_ref=prompt_ref,
                response_ref=response_ref,
                duration_sec=round(time.time() - start, 3),
            )
        except Exception as exc:
            error_msg = truncate_review_artifact(str(exc), limit=4000)
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_error",
                    call_type=f"{base_call_type}_error",
                    payload={
                        "error_type": type(exc).__name__,
                        "error": sanitize_tool_result_for_log(error_msg),
                    },
                    manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model, "status": "error"},
                )
            except Exception:
                response_ref = {}
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="error",
                error=sanitize_tool_result_for_log(error_msg),
                transport_status=_transport_error_status(exc),
                prompt_ref=prompt_ref,
                response_ref=response_ref,
                duration_sec=round(time.time() - start, 3),
            )

    def _emit_usage(
        self,
        request: ReviewRequest,
        slot: ReviewSlot,
        usage: Dict[str, Any],
        *,
        prompt_chars: int = 0,
    ) -> None:
        if self.usage_ctx is None:
            return
        try:
            from ouroboros.tools.review_helpers import emit_review_usage

            emit_review_usage(
                self.usage_ctx,
                model=slot.model,
                usage=usage,
                source=f"review_substrate:{request.surface}",
                prompt_chars=prompt_chars,
                extra={"surface": request.surface, "slot_id": slot.slot_id},
            )
        except Exception:
            pass


def run_review_request(
    request: ReviewRequest,
    *,
    slots: List[ReviewSlot] | None = None,
    drive_root: pathlib.Path | None = None,
    llm: LLMClient | None = None,
    usage_ctx: Any = None,
) -> ReviewRunResult:
    coordinator = ReviewCoordinator(llm=llm, drive_root=drive_root, usage_ctx=usage_ctx)
    return coordinator.run(request, reviewer_slots(role_hint=request.surface) if slots is None else slots)
