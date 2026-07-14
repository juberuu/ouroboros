"""Shared multi-review substrate.

This module is the common cognitive primitive for migrated review surfaces and
the contract target for remaining legacy immune-system reviews. Slot identity is
separate from model identity, so duplicate model IDs are valid independent
reviewer slots.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from ouroboros.observability import new_call_id, persist_call
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
    tier = aggregate_outcome_tier(result)
    contributing = _contributing_actors(result)
    contributing_slots = {str(a.get("slot_id", "")) for a in contributing}
    coach = ""
    for actor in contributing:
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
        if contributing_slots and str(finding.get("slot_id", "")) not in contributing_slots:
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
            str(getattr(result, "aggregate_signal", "") or "").upper() == "FAIL"
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
        classify_tier = bool((request.policy or {}).get("classify_outcome_tier"))
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
