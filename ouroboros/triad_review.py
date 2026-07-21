"""Shared tri-model review primitives.

Both repo commit review and skill review ask multiple reviewer models to
return a JSON array of checklist findings. Keep parsing, quorum accounting,
and observability in one place so future review entrypoints do not re-learn
the same truncation / parse-failure bugs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ouroboros.utils import append_jsonl, utc_now_iso


@dataclass
class ReviewActorRecord:
    model_id: str
    status: str
    raw_text: str
    parsed_items: List[Dict[str, Any]] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    slot: int = 0
    prompt_ref: Dict[str, Any] = field(default_factory=dict)
    response_ref: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "status": self.status,
            "raw_text": self.raw_text,
            "parsed_items": list(self.parsed_items),
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": self.cost_usd,
            "slot": self.slot,
            "slot_id": f"slot_{self.slot}" if self.slot else "",
            "prompt_ref": dict(self.prompt_ref),
            "response_ref": dict(self.response_ref),
        }


@dataclass
class ParsedTriadReview:
    findings: List[Dict[str, Any]]
    responsive_models: List[str]
    actor_records: List[ReviewActorRecord]
    errors: List[str] = field(default_factory=list)

    @property
    def quorum_met(self) -> bool:
        from ouroboros.config import adaptive_quorum
        # actor_records holds ALL dispatched reviewers (responsive + errored), so
        # this honors the configured count: configured=1 & responded=1 -> met;
        # configured=3 & responded=1 -> NOT met (loud degraded surfaces).
        return len(self.responsive_models) >= adaptive_quorum(len(self.actor_records))

    @property
    def degraded_reasons(self) -> List[str]:
        degraded = [r for r in self.actor_records if r.status in {"error", "parse_failure", "partial"}]
        if not degraded or not self.quorum_met:
            return []
        reasons = [f"{r.model_id}={r.status}" for r in degraded]
        return [f"DEGRADED: {', '.join(reasons)} (quorum still met)"]


def _actor_record(
    actor: Dict[str, Any],
    *,
    idx: int,
    model_label: str,
    status: str,
    raw_text: str,
    parsed_items: Optional[List[Dict[str, Any]]] = None,
) -> ReviewActorRecord:
    return ReviewActorRecord(
        model_id=model_label,
        status=status,
        raw_text=raw_text,
        parsed_items=parsed_items or [],
        tokens_in=int(actor.get("tokens_in", 0) or 0),
        tokens_out=int(actor.get("tokens_out", 0) or 0),
        cost_usd=float(actor.get("cost_estimate", 0.0) or 0.0),
        slot=idx + 1,
        prompt_ref=dict(actor.get("prompt_ref") or {}),
        response_ref=dict(actor.get("response_ref") or {}),
    )


def extract_json_array(
    raw: str,
    *,
    normalize: bool = False,
    unwrap_result: bool = False,
    validate_fn: Optional[Callable[[List[Any]], bool]] = None,
) -> Optional[List[Any]]:
    """Best-effort extraction of a JSON array from model output."""
    text = str(raw or "").strip()
    candidates = [text]
    if "```" in text:
        for chunk in text.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk:
                candidates.append(chunk)

    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if unwrap_result and isinstance(obj, dict) and "result" in obj:
                candidate = str(obj["result"]).strip()
                obj = json.loads(candidate)
            if isinstance(obj, list):
                return _accepted_json_array(obj, normalize=normalize, validate_fn=validate_fn)
        except (json.JSONDecodeError, ValueError):
            pass
        except TypeError:
            pass
        ends: List[int] = []
        search_from = 0
        while True:
            pos = candidate.find("]", search_from)
            if pos == -1:
                break
            ends.append(pos)
            search_from = pos + 1
        for end in reversed(ends):
            starts: List[int] = []
            search_from = 0
            while True:
                pos = candidate.find("[", search_from)
                if pos == -1 or pos > end:
                    break
                starts.append(pos)
                search_from = pos + 1
            for start in reversed(starts):
                try:
                    obj = json.loads(candidate[start:end + 1])
                    if isinstance(obj, list):
                        accepted = _accepted_json_array(obj, normalize=normalize, validate_fn=validate_fn)
                        if accepted is not None:
                            return accepted
                except (json.JSONDecodeError, ValueError):
                    continue
    return None


def _accepted_json_array(
    obj: List[Any],
    *,
    normalize: bool,
    validate_fn: Optional[Callable[[List[Any]], bool]],
) -> Optional[List[Any]]:
    if validate_fn is not None and not validate_fn(obj):
        return None
    return _normalize_items(obj) if normalize else obj


def _normalize_items(items: List[Any]) -> List[Any]:
    try:
        from ouroboros.tools.review_helpers import normalize_reviewer_items
        return normalize_reviewer_items(items)
    except Exception:
        return items


def _empty_array_is_verified_clean(raw_text: str) -> bool:
    """True when an EMPTY findings array is a verifiable clean verdict.

    Accepted markers: the explicit ``NO_FINDINGS`` sentinel anywhere in the
    response, or a response whose entire body (modulo code fences) is the bare
    array itself.
    """
    text = str(raw_text or "")
    if "NO_FINDINGS" in text:
        return True
    stripped = text.strip()
    if "```" in stripped:
        # Unwrap a single fenced block: ```json\n[]\n``` or ```\n[]\n```.
        parts = [chunk.strip() for chunk in stripped.split("```") if chunk.strip()]
        if len(parts) == 1:
            body = parts[0]
            if body.startswith("json"):
                body = body[4:].strip()
            stripped = body
    return stripped == "[]"


def parse_model_review_results(
    result_json: Dict[str, Any],
    *,
    required_items: Optional[Sequence[str]] = None,
) -> ParsedTriadReview:
    """Parse model result envelopes into normalized findings and actor records.

    ``required_items`` enforces the skill-review matrix contract: a reviewer
    that omits a checklist item is non-responsive for quorum.
    """
    findings: List[Dict[str, Any]] = []
    responsive: List[str] = []
    records: List[ReviewActorRecord] = []
    required = set(required_items or [])
    for idx, actor in enumerate(result_json.get("results") or []):
        if not isinstance(actor, dict):
            continue
        model = str(actor.get("model") or actor.get("request_model") or "").strip()
        raw_text = str(actor.get("text") or "")
        model_label = model or "reviewer"
        if str(actor.get("verdict") or "").upper() == "ERROR":
            records.append(_actor_record(actor, idx=idx, model_label=model_label, status="error", raw_text=raw_text))
            continue
        parsed = extract_json_array(raw_text, normalize=not required)
        if parsed is None:
            records.append(_actor_record(actor, idx=idx, model_label=model_label, status="parse_failure", raw_text=raw_text))
            continue
        if not required and not parsed and not _empty_array_is_verified_clean(raw_text):
            # Anti-refusal coverage contract: an empty array counts as a real
            # "no findings" verdict only with the explicit NO_FINDINGS sentinel
            # (or a bare `[]`-only response). A `[]` buried in refusal prose
            # ("I cannot review this diff... []") must not enter the quorum as
            # a clean PASS.
            records.append(_actor_record(actor, idx=idx, model_label=model_label, status="parse_failure", raw_text=raw_text))
            continue
        actor_findings: List[Dict[str, Any]] = []
        covered_items: set[str] = set()
        for entry in parsed:
            if not isinstance(entry, dict):
                continue
            item = str(entry.get("item") or "")
            verdict = str(entry.get("verdict") or "").upper()
            if not item or verdict not in {"PASS", "FAIL"}:
                continue
            covered_items.add(item)
            actor_findings.append({
                "item": item,
                "verdict": verdict,
                "severity": str(entry.get("severity") or "advisory").lower(),
                "reason": str(entry.get("reason") or "").strip(),
                "model": model_label,
                **({"obligation_id": str(entry.get("obligation_id") or "")} if entry.get("obligation_id") else {}),
            })
        if required and not required.issubset(covered_items):
            records.append(_actor_record(actor, idx=idx, model_label=model_label, status="partial", raw_text=raw_text, parsed_items=actor_findings))
            continue
        findings.extend(actor_findings)
        responsive.append(f"{model_label}#{idx + 1}")
        records.append(_actor_record(actor, idx=idx, model_label=model_label, status="responded", raw_text=raw_text, parsed_items=actor_findings))
    return ParsedTriadReview(findings=findings, responsive_models=responsive, actor_records=records)


def emit_review_model_error_events(ctx: Any, parsed: ParsedTriadReview, *, source: str, skill_name: str = "") -> None:
    """Persist model error / parse-failure events for observability."""
    try:
        log_path = ctx.drive_logs() / "events.jsonl"
    except Exception:
        return
    for record in parsed.actor_records:
        if record.status not in {"error", "parse_failure", "partial"}:
            continue
        if source == "skill_review":
            note = (
                "Full raw response preserved in review.json raw_actor_records "
                "when quorum succeeds; otherwise in review_history.jsonl."
            )
        else:
            note = "Full raw response preserved in triad_raw_results."
        try:
            append_jsonl(log_path, {
                "ts": utc_now_iso(),
                "type": "review_model_error",
                "source": source,
                "skill": skill_name,
                "model": record.model_id,
                "status": record.status,
                "error_note": note,
            })
        except Exception:
            pass


# ── Reviewer prompt rule texts (byte-stable governance, cache-marked) ─────────
# Moved from review_substrate._render_prompt_parts in v6.74.0: the substrate
# crossed the 1600-line module gate, and these shared reviewer rule texts are
# multi-model review primitives — this module's charter. They MUST stay
# byte-stable across a surface's repeat calls (they live inside the cache-marked
# governance segment).

TIER_CLASSIFICATION_RULES = (
        "outcome_tier classifies the CURRENT deliverable and completion_coach is the single "
        "highest-value change that would move it one tier up. Never classify solved unless the "
        "claimed result is actually verified by the evidence — your veto over false success "
        "claims is the point of this review. A real partial deliverable with honestly marked "
        "gaps is best_effort, not a failure. "
)

ACCEPTANCE_SURFACE_RULES = (
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
        "OBLIGATION REBUTTALS: `acceptance_obligations` (host_attested) lists the id/item/"
        "recommendation of each obligation a prior review round raised; the agent's per-obligation "
        "dispositions and rebuttal reasons arrive under `agent_supplied.agent_decision."
        "obligation_dispositions`, joinable by id. Treat a 'rejected' disposition as a rebuttal to "
        "that finding: if the argument is genuinely valid, do not re-raise the same finding; if it "
        "is not, re-raise the finding and explain why the rebuttal fails. A rebuttal may dismiss or "
        "reframe an obligation, but it is NEVER itself evidence for a criterion — 'supported' still "
        "requires an independent host/tool/artifact receipt and 'solved' still requires the real "
        "grader-mirroring check to pass. "
        "REVIEW REGISTER: you are an outside perspective helping the task land, not a gate of last "
        "resort. Concrete, reachable-now defects block; hypotheticals, style preferences, and hygiene "
        "are advisory findings, not blockers. "
        "REACHABILITY: a requirement that cannot be satisfied in THIS environment (a missing "
        "credential/UI/network/tool, an owner-side dependency) is unavailable here — classify the "
        "tier honestly, name the gap, and do not re-raise it as a blocking finding. "
        "OBLIGATION IDENTITY: every finding carries disposition_kind — \"re_raise\" when it maintains "
        "an obligation already listed in `acceptance_obligations` (then obligation_id MUST be that "
        "exact id), \"new\" otherwise. A re_raise with a missing or unknown obligation_id is recorded "
        "as new, so name ids precisely. When the agent's disposition rejected an obligation, "
        "adjudicate the rebuttal: if the argument is valid, retire the finding — do not re-raise it; "
        "if it is not, re_raise it and state in `evidence` why the argument fails. "
        "DIALOGUE STATUS: dialogue_status is your typed judgement about the review DIALOGUE itself — "
        "\"continue_actionable\" (a concrete, reachable-now improvement exists), \"unreachable_here\" "
        "(the remaining gap cannot be closed in this environment), or \"stable_disagreement\" (both "
        "positions are fully argued and further rounds will not change the outcome). An honest "
        "terminal judgement ends the loop and surfaces both positions to the owner; it is not a "
        "concession. "
)
