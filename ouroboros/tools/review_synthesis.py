"""Shared pure synthesis and normalization for reviewer outputs.

Commit-review claims use the optional LLM deduplicator; plan-review helpers below
normalize its existing handoff artifact and exact follow-up contract without I/O.
"""

from __future__ import annotations

import json
import logging
import re
from hashlib import sha256
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from ouroboros.tools.plan_review import _PlanReviewRequest

from ouroboros.triad_review import extract_json_array
from ouroboros.tools.review_helpers import emit_review_usage

log = logging.getLogger(__name__)

# Bound cost and avoid mixed canonical/raw output on oversized finding sets.
_MAX_CLAIMS_FOR_SYNTHESIS = 30

_MIN_CLAIMS_FOR_SYNTHESIS = 2

_SYNTHESIS_PROMPT_TEMPLATE = (
    "You are a code-review claim synthesizer. You receive a list of raw findings\n"
    "from multiple independent reviewers (triad diff-reviewers + one Atlas-backed\n"
    "scope reviewer). Your job is to produce a deduplicated canonical list.\n"
    "\n"
    "## Rules\n"
    "\n"
    "1. Merge claims that share the same **root cause** in the same file/symbol\n"
    "   into ONE canonical entry. Use the most specific/concrete reason text.\n"
    "2. **Do NOT merge** findings about genuinely different bugs, even if they are\n"
    "   in the same file. One root cause = one canonical issue.\n"
    "3. If an incoming claim already carries an `obligation_id` that matches an\n"
    "   open obligation from a previous round (provided below), PRESERVE that\n"
    "   `obligation_id` on the canonical entry. This allows durable obligations\n"
    "   to survive across retries without ID rotation.\n"
    "4. If no existing obligation matches, leave `obligation_id` as \"\" — a new\n"
    "   obligation will be assigned downstream.\n"
    "5. Do NOT invent new findings. Only deduplicate what you have been given.\n"
    "6. For each canonical entry, list `evidence_from_reviewers`: which reviewer(s)\n"
    "   independently flagged this issue (use the `tag` or `model` field if present).\n"
    "7. Output ONLY valid JSON — a JSON array of canonical findings, no markdown fences,\n"
    "   no prose outside the array.\n"
    "\n"
    "## Output format (each element)\n"
    "\n"
    '{"item": "<checklist item name>", "severity": "critical|advisory",\n'
    ' "reason": "<most concrete reason>", "obligation_id": "<existing id or empty>",\n'
    ' "evidence_from_reviewers": ["<tag/model1>", "<tag/model2>"]}\n'
    "\n"
    "## Open obligations from previous rounds (match by item + reason similarity)\n"
    "\n"
    "OPEN_OBLIGATIONS_PLACEHOLDER\n"
    "\n"
    "## Raw reviewer claims to deduplicate\n"
    "\n"
    "CLAIMS_PLACEHOLDER\n"
    "\n"
    "Respond with ONLY the JSON array. No explanation.\n"
)


def _redact(text: str) -> str:
    """Redact secret-like values from a string before including it in an LLM prompt."""
    try:
        from ouroboros.tools.review_helpers import redact_prompt_secrets
        redacted, _ = redact_prompt_secrets(str(text or ""))
        return redacted
    except Exception:
        return ""


def _format_obligations(open_obligations: List[Any]) -> str:
    """Render open obligations as compact secret-redacted JSON."""
    if not open_obligations:
        return "[]"
    from ouroboros.utils import truncate_review_artifact
    items = []
    for o in open_obligations:
        raw_reason = str(getattr(o, "reason", "") or "")
        redacted_reason = _redact(raw_reason)
        items.append({
            "obligation_id": str(getattr(o, "obligation_id", "") or ""),
            "item": str(getattr(o, "item", "") or ""),
            "reason_excerpt": truncate_review_artifact(redacted_reason, limit=500),
        })
    try:
        return json.dumps(items, ensure_ascii=False, indent=2)
    except Exception:
        return "[]"


def _format_claims(findings: List[Dict[str, Any]]) -> str:
    """Render raw findings as compact JSON with secret-redacted reasons."""
    try:
        safe = []
        for f in findings:
            entry = dict(f)
            if "reason" in entry:
                entry["reason"] = _redact(str(entry["reason"] or ""))
            safe.append(entry)
        return json.dumps(safe, ensure_ascii=False, indent=2)
    except Exception:
        return "[]"


def _normalize_evidence(value: Any) -> List[str]:
    """Normalize evidence_from_reviewers without splitting bare strings into chars."""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if isinstance(v, str)]
    return []


def _parse_synthesis_output(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Parse the synthesizer's JSON array response. Returns None on failure."""
    if not raw:
        return None
    parsed = extract_json_array(raw)
    if not isinstance(parsed, list):
        return None
    result = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        if not entry.get("item"):
            continue
        canonical = {
            "item": str(entry.get("item", "") or ""),
            # The synthesizer's INPUT is exclusively critical findings, so a
            # missing severity must stay critical — an "advisory" default
            # silently downgraded blocking findings out of the gate.
            "severity": str(entry.get("severity", "critical") or "critical"),
            "reason": str(entry.get("reason", "") or ""),
            "obligation_id": str(entry.get("obligation_id", "") or ""),
            "evidence_from_reviewers": _normalize_evidence(entry.get("evidence_from_reviewers")),
            # FAIL default ensures synthesized findings create obligations downstream.
            "verdict": str(entry.get("verdict", "") or "FAIL"),
        }
        for key in ("tag", "model"):
            if key in entry:
                canonical[key] = entry[key]
        result.append(canonical)
    return result if result else None


def synthesize_to_canonical_issues(
    critical_findings: List[Dict[str, Any]],
    *,
    open_obligations: Optional[List[Any]] = None,
    ctx: Any = None,
) -> List[Dict[str, Any]]:
    """Return deduplicated findings, or original findings on any synthesis failure."""
    if not critical_findings:
        return critical_findings

    if len(critical_findings) < _MIN_CLAIMS_FOR_SYNTHESIS:
        return critical_findings

    # Oversized sets pass through unchanged; no hybrid canonical/raw tail.
    if len(critical_findings) > _MAX_CLAIMS_FOR_SYNTHESIS:
        log.debug(
            "review_synthesis: %d claims exceeds limit %d — skipping synthesis, "
            "returning original findings unchanged",
            len(critical_findings),
            _MAX_CLAIMS_FOR_SYNTHESIS,
        )
        return critical_findings

    obligations = list(open_obligations or [])

    try:
        prompt = (
            _SYNTHESIS_PROMPT_TEMPLATE
            .replace("OPEN_OBLIGATIONS_PLACEHOLDER", _format_obligations(obligations))
            .replace("CLAIMS_PLACEHOLDER", _format_claims(critical_findings))
        )
    except Exception as exc:
        log.warning("review_synthesis: failed to build prompt: %s", exc)
        return critical_findings

    try:
        raw_response = _call_synthesis_llm(prompt, ctx=ctx)
    except Exception as exc:
        log.warning("review_synthesis: LLM call raised exception: %s — using original findings", exc)
        return critical_findings

    if raw_response is None:
        log.warning("review_synthesis: LLM call returned None — using original findings")
        return critical_findings

    canonical = _parse_synthesis_output(raw_response)
    if canonical is None:
        log.warning("review_synthesis: failed to parse LLM output — using original findings")
        return critical_findings

    log.debug(
        "review_synthesis: %d raw → %d canonical",
        len(critical_findings),
        len(canonical),
    )
    return canonical


def _call_synthesis_llm(prompt: str, *, ctx: Any = None) -> Optional[str]:
    """Call the light LLM and emit usage so synthesis spend is accounted."""
    try:
        from ouroboros.config import get_light_model
        from ouroboros.llm import LLMClient

        model = get_light_model()

        client = LLMClient()

        # no_proxy avoids macOS fork-safety crashes in worker processes.
        msg, usage = client.chat(
            messages=[{"role": "user", "content": prompt}],
            model=model,
            max_tokens=16384,
            reasoning_effort="low",
            no_proxy=True,
        )

        if _has_billable_usage(usage):
            resolved_model = str((usage or {}).get("resolved_model") or "") or model
            provider = str((usage or {}).get("provider") or "") if isinstance(usage, dict) else ""
            emit_review_usage(
                ctx,
                model=resolved_model,
                usage=usage,
                source="review_synthesis",
                provider=provider,
            )

        if not msg:
            return None
        content = msg.get("content") if isinstance(msg, dict) else None
        if not content:
            return None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            ]
            return "\n".join(t for t in texts if t) or None
        return str(content) if content else None

    except Exception as exc:
        log.warning("review_synthesis: LLM call failed: %s", exc)
        return None


def _has_billable_usage(usage: Any) -> bool:
    if not isinstance(usage, dict):
        return False
    return any(
        usage.get(key)
        for key in ("prompt_tokens", "input_tokens", "completion_tokens", "output_tokens", "cost", "total_cost")
    )


# Pure plan-review contract helpers live here so plan_review.py remains the single
# state/LLM orchestrator without crossing the repository's module-size gate.
_PLAN_SCOPE_LIST_FIELDS = ("in_scope", "invariants", "non_goals", "rejected_expansions")
PLAN_REVIEW_CONTROL_PREFIX = "PLAN_REVIEW_CONTROL_JSON: "


def normalize_plan_scope(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("scope must be an object")
    allowed = set(_PLAN_SCOPE_LIST_FIELDS) | {"selected_seam"}
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise ValueError(f"unknown scope fields: {', '.join(unknown)}")
    result: Dict[str, Any] = {key: [] for key in _PLAN_SCOPE_LIST_FIELDS}
    for key in _PLAN_SCOPE_LIST_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"scope.{key} must be an array of strings")
        items = [item.strip() for item in value if item.strip()]
        result[key] = items
    seam = raw.get("selected_seam")
    if seam is not None and not isinstance(seam, str):
        raise ValueError("scope.selected_seam must be a string")
    result["selected_seam"] = seam.strip() if isinstance(seam, str) else ""
    return result


def plan_review_fingerprint(
    *,
    plan: str,
    goal: str,
    files_to_touch: List[str],
    context_level: str,
    context_notes: str,
    plan_class: str = "",
    scope: Optional[Dict[str, Any]] = None,
    include_tests: bool = False,
) -> str:
    payload: Dict[str, Any] = {
        "plan": plan,
        "goal": goal,
        "files_to_touch": list(files_to_touch or []),
        "context_level": context_level,
        "context_notes": context_notes or "",
        "scope": normalize_plan_scope(scope),
        "include_tests": bool(include_tests),
    }
    if str(plan_class or "").strip():
        payload["plan_class"] = str(plan_class).strip()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


def plan_text_fingerprint(plan: str) -> str:
    return sha256(plan.encode("utf-8")).hexdigest()


def parse_plan_review_signal(text: str) -> str:
    matches = re.findall(
        r"^\s*AGGREGATE\s*:\s*(GREEN|REVIEW_REQUIRED|REVISE_PLAN)\s*$",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    return matches[0].upper() if len(matches) == 1 else ""


def _strict_plan_findings_block(text: str) -> tuple[Optional[List[Any]], str]:
    """Parse the one required JSON block immediately before the final verdict."""
    markers = list(re.finditer(r"(?m)^[ \t]*PLAN_FINDINGS_JSON:[ \t]*", text))
    if len(markers) != 1:
        return None, (
            "PLAN_FINDINGS_JSON is missing"
            if not markers
            else "PLAN_FINDINGS_JSON appears more than once"
        )
    aggregates = list(re.finditer(
        r"(?im)^[ \t]*AGGREGATE[ \t]*:[ \t]*(GREEN|REVIEW_REQUIRED|REVISE_PLAN)[ \t]*$",
        text,
    ))
    if len(aggregates) != 1:
        return None, "the response must contain exactly one AGGREGATE verdict"
    marker, aggregate = markers[0], aggregates[0]
    if marker.start() > aggregate.start():
        return None, "PLAN_FINDINGS_JSON must precede AGGREGATE"
    if text[aggregate.end():].strip():
        return None, "AGGREGATE must be the final non-empty line"
    payload = text[marker.end():aggregate.start()].strip()
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return None, "PLAN_FINDINGS_JSON is not a valid standalone JSON array"
    if not isinstance(parsed, list):
        return None, "PLAN_FINDINGS_JSON is not a JSON array"
    return parsed, ""


def addressable_plan_findings(
    result: Dict[str, Any], *, reviewer_index: int, signal: str
) -> tuple[List[Dict[str, Any]], str]:
    model = str(result.get("model") or result.get("request_model") or f"Model {reviewer_index}")
    text = str(result.get("text") or "")
    if result.get("error") or not text.strip() or not signal:
        return ([{
            "finding_id": f"plan-slot-{reviewer_index}:reviewer-unavailable",
            "reviewer_slot": reviewer_index,
            "model": model,
            "level": "RISK",
            "summary": "Reviewer failed to return a usable, parseable planning verdict.",
            "recommendation": "Disposition this disclosed evidence gap explicitly before proceeding.",
        }], "reviewer_unavailable")
    parsed, parse_error = _strict_plan_findings_block(text)
    findings: List[Dict[str, Any]] = []
    local_ids: set[str] = set()
    if isinstance(parsed, list):
        for index, item in enumerate(parsed):
            if not isinstance(item, dict):
                parse_error = f"finding {index} is not an object"
                break
            local_id = str(item.get("id") or "").strip()
            level = str(item.get("level") or "").strip().upper()
            summary = str(item.get("summary") or "").strip()
            recommendation = str(item.get("recommendation") or "").strip()
            if (not local_id or local_id in local_ids or level not in {"RISK", "FAIL"}
                    or not summary or not recommendation):
                parse_error = f"finding {index} has invalid id/level/summary/recommendation"
                break
            local_ids.add(local_id)
            findings.append({
                "finding_id": f"plan-slot-{reviewer_index}:{local_id}",
                "reviewer_slot": reviewer_index,
                "model": model,
                "level": level,
                "summary": summary,
                "recommendation": recommendation,
            })
    if not parse_error and findings:
        return findings, "green_with_findings" if signal == "GREEN" else ""
    if not parse_error and parsed == [] and signal == "GREEN":
        return [], ""
    if signal in {"REVIEW_REQUIRED", "REVISE_PLAN"}:
        reason = parse_error or "PLAN_FINDINGS_JSON contains no addressable finding"
        return ([{
            "finding_id": f"plan-slot-{reviewer_index}:reviewer-response",
            "reviewer_slot": reviewer_index,
            "model": model,
            "level": "FAIL" if signal == "REVISE_PLAN" else "RISK",
            "summary": f"Reviewer declared {signal}, but {reason}.",
            "recommendation": "Disposition the full reviewer response as one fail-closed bundle.",
        }], reason)
    if parse_error:
        return ([{
            "finding_id": f"plan-slot-{reviewer_index}:findings-contract",
            "reviewer_slot": reviewer_index,
            "model": model,
            "level": "RISK",
            "summary": f"Reviewer findings projection is malformed: {parse_error}.",
            "recommendation": "Treat the reviewer as degraded and disposition the evidence gap.",
        }], parse_error)
    return [], ""


def summarize_plan_review_results(raw_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    signals: List[str] = []
    findings: List[Dict[str, Any]] = []
    projection_errors: Dict[int, str] = {}
    for index, result in enumerate(raw_results, start=1):
        text = str(result.get("text") or "")
        signal = "DEGRADED" if result.get("error") or not text.strip() else (
            parse_plan_review_signal(text) or "DEGRADED"
        )
        projected, error = addressable_plan_findings(
            result, reviewer_index=index, signal="" if signal == "DEGRADED" else signal
        )
        if error:
            projection_errors[index] = error
            if signal == "GREEN":
                signal = "DEGRADED"
        findings.extend(projected)
        signals.append(signal)
    counts = {name: signals.count(name) for name in (
        "REVISE_PLAN", "REVIEW_REQUIRED", "DEGRADED", "GREEN"
    )}
    from ouroboros.config import adaptive_quorum
    if signals and counts["REVISE_PLAN"] >= adaptive_quorum(len(signals)):
        aggregate = "REVISE_PLAN"
    elif counts["REVISE_PLAN"] == 1 or counts["REVIEW_REQUIRED"] or counts["DEGRADED"]:
        aggregate = "REVIEW_REQUIRED"
    elif signals and counts["GREEN"] == len(signals):
        aggregate = "GREEN"
    else:
        aggregate = "REVIEW_REQUIRED"
    return {
        "signals": signals,
        "findings": findings,
        "projection_errors": projection_errors,
        "aggregate_signal": aggregate,
        "revise_count": counts["REVISE_PLAN"],
        "review_required_count": counts["REVIEW_REQUIRED"],
        "degraded_count": counts["DEGRADED"],
        "green_count": counts["GREEN"],
    }


def _quote_public_plan_review_control_lines(text: str) -> str:
    """Keep reviewer prose visible without impersonating the host control footer."""
    return "".join(
        "> " + line if line.startswith(PLAN_REVIEW_CONTROL_PREFIX) else line
        for line in str(text or "").splitlines(keepends=True)
    )


def format_plan_review_output(
    raw_results: List[Dict[str, Any]], models: List[str], goal: str, estimated_tokens: int
) -> str:
    summary = summarize_plan_review_results(raw_results)
    lines = [
        "## Plan Review Results", "", f"**Goal:** {goal}",
        f"**Models:** {len(models)} parallel reviewers",
        f"**Prompt size:** ~{estimated_tokens:,} tokens per reviewer", "", "---", "",
    ]
    signals = list(summary["signals"])
    for index, result in enumerate(raw_results, start=1):
        model = result.get("model") or result.get("request_model") or f"Model {index}"
        lines.extend([f"### Reviewer {index}: {model}", ""])
        if result.get("error"):
            lines.extend([f"⚠️ **ERROR:** {result['error']}", "", "---", ""])
            continue
        text = str(result.get("text") or "").strip()
        if not text:
            lines.extend(["⚠️ **ERROR:** Empty response from reviewer.", "", "---", ""])
            continue
        lines.extend([text, ""])
        error = summary["projection_errors"].get(index)
        if error:
            lines.extend([
                f"⚠️ **FINDINGS CONTRACT:** {error}; the response is represented by a "
                "fail-closed addressable finding.", "",
            ])
        lines.extend(["---", ""])
    if not signals:
        lines.extend([
            "## Aggregate Signal", "", "❓ **REVIEW_REQUIRED**", "",
            "No reviewer responses were collected (empty reviewer list). Treat as "
            "REVIEW_REQUIRED — re-run plan_task with at least one reviewer configured.",
        ])
        return _quote_public_plan_review_control_lines("\n".join(lines))
    aggregate = str(summary["aggregate_signal"])
    emoji = {"GREEN": "✅", "REVIEW_REQUIRED": "⚠️", "REVISE_PLAN": "❌"}.get(aggregate, "❓")
    lines.extend([
        "## Aggregate Signal", "", f"{emoji} **{aggregate}**", "",
        f"Per-reviewer signals: REVISE_PLAN={summary['revise_count']}, "
        f"REVIEW_REQUIRED={summary['review_required_count']}, GREEN={summary['green_count']}, "
        f"DEGRADED={summary['degraded_count']}.",
    ])
    if len(signals) < 2:
        lines.append(
            "⚠️ single_reviewer_no_diversity: this plan review ran with a single reviewer "
            "slot — no cross-model diversity. The signal is honored but is structurally "
            "lower-confidence; configure ≥2 reviewer slots for a diverse plan review."
        )
    lines.append("")
    if aggregate == "GREEN":
        lines.append("All reviewers converged on GREEN. Read every reviewer's PROPOSALS section and proceed with implementation.")
    elif aggregate == "REVIEW_REQUIRED":
        reasons = []
        if summary["revise_count"] == 1:
            reasons.append("one reviewer dissented with REVISE_PLAN; read the dissenting response in full")
        if summary["review_required_count"]:
            reasons.append(f"{summary['review_required_count']} reviewer(s) raised RISKs or concerns")
        if summary["degraded_count"]:
            reasons.append(f"{summary['degraded_count']} reviewer(s) returned no parseable verdict")
        if reasons:
            lines.append("Reason: " + "; ".join(reasons) + ".")
        lines.append("Read every full response and disposition the addressable findings before coding.")
    else:
        lines.append(
            f"{summary['revise_count']} reviewer(s) independently flagged REVISE_PLAN; "
            "change the plan before writing code."
        )
    lines.extend([
        "", "## Findings Requiring Disposition", "", "```json",
        json.dumps(summary["findings"], ensure_ascii=False, indent=2, default=str), "```",
    ])
    return _quote_public_plan_review_control_lines("\n".join(lines))


def render_plan_review_result(review: Dict[str, Any], *, cached: bool = False) -> str:
    aggregate = str(review.get("aggregate_signal") or "REVIEW_REQUIRED")
    closed = bool(review.get("closed"))
    if aggregate == "GREEN":
        explanation = "The exact plan fingerprint was accepted by the reviewer panel."
    elif aggregate == "REVIEW_REQUIRED" and closed:
        explanation = "Every finding was dispositioned for this unchanged fingerprint; no reviewer was called."
    elif aggregate == "REVISE_PLAN":
        explanation = "The plan text must change, producing a new fingerprint and review."
    else:
        explanation = "Disposition every finding id before implementation; do not rerun reviewers."
    findings = [item for item in (review.get("findings") or []) if isinstance(item, dict)]
    return "\n".join([
        "## Plan Review Results", "",
        f"**Plan fingerprint:** `{review.get('request_fingerprint') or ''}`",
        f"**Cached exact review:** {bool(cached)}", "",
        "## Findings Requiring Disposition", "", "```json",
        json.dumps(findings, ensure_ascii=False, indent=2, default=str), "```", "",
        "## Aggregate Signal", "", f"**{aggregate}**", "", explanation, "",
        PLAN_REVIEW_CONTROL_PREFIX + json.dumps(
            {"outcome": aggregate, "closed": closed}, separators=(",", ":")
        ),
        f"PLAN_REVIEW_OUTCOME: {aggregate}", f"AGGREGATE: {aggregate}",
    ])


def validate_plan_review_disposition(
    review: Dict[str, Any], fingerprint: str, disposition: Any
) -> tuple[Optional[Dict[str, Any]], str]:
    invalid = "ERROR: PLAN_REVIEW_DISPOSITION_INVALID: "
    if not isinstance(disposition, dict):
        return None, invalid + "review_disposition must be an object"
    unknown = sorted(str(key) for key in disposition if key not in {"review_fingerprint", "items"})
    if unknown:
        return None, invalid + f"unknown fields: {', '.join(unknown)}"
    expected_fp = str(review.get("request_fingerprint") or "")
    if str(disposition.get("review_fingerprint") or "").strip() != expected_fp or fingerprint != expected_fp:
        return None, (
            "ERROR: PLAN_REVIEW_DISPOSITION_STALE: review_disposition must name the "
            "exact immediately preceding plan fingerprint."
        )
    aggregate = str(review.get("aggregate_signal") or "")
    if aggregate == "REVISE_PLAN":
        return None, "ERROR: PLAN_REVIEW_REVISION_REQUIRED: change the plan text before a fresh review."
    if aggregate == "GREEN":
        return None, invalid + "the prior result is already GREEN"
    if aggregate != "REVIEW_REQUIRED":
        return None, invalid + "the prior aggregate is missing or invalid"
    raw_items = disposition.get("items")
    if not isinstance(raw_items, list):
        return None, invalid + "items must be an array"
    expected = [
        str(item.get("finding_id") or "") for item in (review.get("findings") or [])
        if isinstance(item, dict) and str(item.get("finding_id") or "")
    ]
    if not expected:
        return None, invalid + "the prior REVIEW_REQUIRED result has no addressable findings"
    normalized = []
    seen: set[str] = set()
    allowed = {"finding_id", "decision", "rationale", "plan_revision"}
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            return None, invalid + f"items[{index}] must be an object"
        item_unknown = sorted(str(key) for key in item if key not in allowed)
        if item_unknown:
            return None, invalid + f"items[{index}] has unknown fields: {', '.join(item_unknown)}"
        finding_id = str(item.get("finding_id") or "").strip()
        decision = str(item.get("decision") or "").strip().lower()
        rationale = str(item.get("rationale") or "").strip()
        revision = str(item.get("plan_revision") or "").strip()
        if not finding_id or finding_id in seen:
            return None, invalid + f"items[{index}].finding_id is blank or duplicated"
        if finding_id not in expected:
            return None, invalid + f"unknown finding_id {finding_id!r}"
        if decision not in {"accept", "reject", "defer"}:
            return None, invalid + f"items[{index}].decision must be accept, reject, or defer"
        if not rationale:
            return None, invalid + f"items[{index}].rationale is required"
        if decision == "accept" and not revision:
            return None, invalid + f"items[{index}].plan_revision is required when accepting a finding"
        seen.add(finding_id)
        normalized.append({
            "finding_id": finding_id,
            "decision": decision,
            "rationale": rationale,
            "plan_revision": revision,
        })
    missing = [finding_id for finding_id in expected if finding_id not in seen]
    if missing:
        return None, invalid + "missing finding ids: " + ", ".join(missing)
    updated = dict(review)
    updated["disposition"] = {
        "review_fingerprint": expected_fp,
        "items": normalized,
    }
    updated["closed"] = True
    return updated, ""


def build_plan_review_system_prompt(
    checklist: str,
    bible_text: str,
    dev_md: str,
    arch_md: str,
    checklists_md: str = "",
    context_level: str = "",
    plan_class: str = "self_mod",
) -> str:
    """Build the pure, reusable plan-review system prompt."""
    atlas_note = (
        f"Repository evidence is bounded by context_level={context_level!r}: "
        "`minimal` includes governance docs, the plan, and touched-file snapshots "
        "without a generated Atlas; `localized`, `broad`, and `constitutional` add "
        "progressively larger generated Atlas context. Use only evidence actually present."
    )
    if plan_class and plan_class != "self_mod":
        atlas_note += (
            f"\nThis plan is classified plan_class={plan_class!r} (NOT a self-modification "
            "of the Ouroboros system repo): ARCHITECTURE.md is provided as a lossless "
            "navigation map (sections + line ranges) rather than inline full text — judge "
            "the plan against ITS OWN domain (the external codebase / creative deliverable / "
            "research question), and consult the map only where the plan genuinely touches "
            "the runtime's own surfaces."
        )
    parts = [(
        "You are a senior design reviewer for Ouroboros, a self-creating AI agent.\n"
        "Your job is to review a proposed implementation plan BEFORE any code is written.\n"
        "You are validating a concrete candidate plan, not brainstorming from zero. If the plan is weak, say exactly why and what boundary or contract was missed.\n"
        f"{atlas_note}\n\n"
        "## Review stance — GENERATIVE, not audit\n\n"
        "Your primary job is to CONTRIBUTE ideas the implementer may not see, using the repository evidence provided for this context level.\n"
        "Finding defects in the plan is secondary; proposing concrete alternatives, surfacing existing surfaces that already solve the goal, and flagging subtle contract breaks is primary.\n"
        "Assume the implementer has already thought through the first-pass design — you are a design PARTNER who contributes, not an auditor who rubber-stamps.\n\n"
        "## Required output structure (follow exactly)\n\n"
        "1. **Your own approach**. State what YOU would do with the available repository evidence: the concrete alternative path, the existing file/function you would reuse, or the simpler route. If after real effort you see no better approach, say so explicitly.\n"
        "2. **`## PROPOSALS` section**. Offer every material idea you judge useful; there is no issue quota. A proposal may identify:\n   - An existing function/module that already solves this (named exactly).\n   - A subtle contract break or shared-state interaction the plan likely missed.\n   - A simpler path with less surface area preserving the goal.\n   - A risk pattern visible from codebase history in your context.\n   - A BIBLE.md alignment issue with a specific principle cited.\n"
        "3. **Per-item verdicts**. For each checklist item below:\n   - **verdict**: PASS | RISK | FAIL\n   - **explanation**: enough context to make the judgment understandable\n   - **concrete fix** (if RISK or FAIL): exact file, function, or line to address\n   - **alternative approaches** (if applicable): any more elegant solution you genuinely recommend\n"
        "4. **Addressable findings block**. Immediately before the final line, write `PLAN_FINDINGS_JSON:` followed by one JSON array. Include every material RISK/FAIL as an object with unique local `id`, `level` (`RISK` or `FAIL`), `summary`, and `recommendation`. Use `[]` when GREEN. Do not invent findings to fill a quota.\n"
        "5. **Final line** (exactly one of):\n   - `AGGREGATE: GREEN` — no critical issues, implementer can proceed\n   - `AGGREGATE: REVIEW_REQUIRED` — risks or minor concerns, implementer should consider adjustments\n   - `AGGREGATE: REVISE_PLAN` — critical structural issues, plan must be revised before coding\n\n"
        "Be specific. Name exact files, functions, constants, or call sites.\nVague concerns without a concrete pointer are advisory at most.\nIf you see a simpler solution, say so directly — don't just hint.\n\n"
        "## Rules (what NOT to flag)\n\n"
        "- A minimalism/SOLID finding must name a concrete defect, duplicated authority or coupling, "
        "or a specifically smaller EXISTING extension seam. Diff size, line count, and file count are "
        "not findings by themselves.\n"
        "- Do NOT penalise missing tests, `VERSION` bumps, `README.md` changelog rows, or `docs/ARCHITECTURE.md` updates — the plan has no code yet. Focus on design correctness and elegance, not commit hygiene. Commit-gate reviewers handle that later.\n\n"
        "## Aggregate level — adaptive-quorum coordination across the configured reviewer slots\n\n"
        "- `AGGREGATE: REVISE_PLAN` should be used ONLY when you are confident the plan has a concrete structural problem that warrants a redesign. The coordinator escalates to final `REVISE_PLAN` only when a quorum of reviewer slots independently flag it (`config.adaptive_quorum`: 2-of-N for 3+ slots, both in a 2-slot setup, and a single reviewer in a 1-slot setup) — a lone dissenting `REVISE_PLAN` in a multi-reviewer setup will surface as `REVIEW_REQUIRED` with your dissent noted. Use `REVIEW_REQUIRED` for real but non-structural risks; reserve `REVISE_PLAN` for defects that require changed plan text and a fresh fingerprint review.\n\n---\n"
    )]
    if checklist and not checklists_md:
        parts.append(f"## Plan Review Checklist\n\n{checklist}\n\n---\n")
    for title, body in (
        ("## BIBLE.md (Constitution — highest priority)", bible_text),
        ("## DEVELOPMENT.md (Engineering handbook)", dev_md),
        ("## ARCHITECTURE.md (Current system structure)", arch_md),
    ):
        if body:
            parts.append(f"{title}\n\n{body}\n\n---\n")
    if checklists_md:
        parts.append(
            "## CHECKLISTS.md (review contracts and critical thresholds)\n\n"
            "Use the `## Plan Review Checklist` section inside this file as the per-item matrix for this plan review.\n\n"
            f"{checklists_md}\n\n---\n"
        )
    return "\n".join(parts)


def build_plan_review_user_content(
    request: _PlanReviewRequest,
    head_snapshots: str,
    repo_pack: str,
    omitted_note: str,
) -> str:
    """Build the pure, reusable plan-review user prompt."""
    plan = request.plan
    goal = request.goal
    files_to_touch = request.files_to_touch
    context_level = request.context_level
    context_notes = request.context_notes
    include_tests = request.include_tests
    scope = request.scope
    parts = [
        (
            "## Implementation Plan Under Review\n\n"
            "### Goal and Scope\n\n"
            "```json\n"
            + json.dumps(
                {"goal": goal, "scope": normalize_plan_scope(scope)},
                ensure_ascii=False,
                indent=2,
            )
            + "\n```\n\n"
            f"**Proposed Plan:**\n{plan}\n"
        ),
        (
            "## Plan Context Contract\n\n"
            f"**Context level:** {context_level}\n"
            f"**Include tests in generated Atlas:** {bool(include_tests)}\n"
        ),
    ]
    if context_notes:
        parts.append(f"**Agent context notes:** {context_notes}\n")
    if files_to_touch:
        parts.append(f"**Files planned to touch:** {', '.join(files_to_touch)}\n")
    if head_snapshots:
        parts.append(f"## Current State of Planned-Touch Files (HEAD)\n\n{head_snapshots}\n")
    if repo_pack:
        parts.append(f"## Generated Repository Atlas (for cross-module analysis)\n\n{repo_pack}")
    if omitted_note:
        parts.append(omitted_note)
    return "\n".join(parts)


def planning_swarm_context(
    *, plan: str, goal: str, files_to_touch: List[str], context_level: str,
    context_notes: str, scope: Optional[Dict[str, Any]] = None,
) -> str:
    return "\n".join([
        "Review this proposed implementation plan before any edits are made.", "",
        "[GOAL_AND_SCOPE]",
        json.dumps({"goal": goal, "scope": normalize_plan_scope(scope)}, ensure_ascii=False, indent=2),
        "", "[PLAN]", plan, "", "[FILES_TO_TOUCH]",
        json.dumps(files_to_touch or [], ensure_ascii=False, indent=2),
        "", "[CONTEXT_LEVEL]", context_level, "", "[CONTEXT_NOTES]",
        context_notes or "(none)",
    ])


def planning_scout_framing(plan_class: str) -> tuple[str, str]:
    if plan_class == "self_mod" or not plan_class:
        return (
            "Independently review the proposed implementation plan before code edits. "
            "Inspect repo/docs/logs if useful. Focus on missing touchpoints, hidden "
            "contracts, sequencing risks, and simpler alternatives. Do not implement.",
            "Readonly planning only. Do not edit files, commit, run shell, or request review "
            "gates. Use concrete file/symbol references when possible.",
        )
    domain = {
        "external": "the external codebase/workspace this plan targets",
        "creative": "the creative deliverable (content, design, UX, audience fit)",
        "research": "the research question (sources, method, evidence quality)",
    }.get(plan_class, "the task's own domain")
    return (
        f"Independently review the proposed plan before execution. This is a {plan_class} "
        f"plan: scout {domain} — pick the angle that most improves THIS plan, NOT Ouroboros-"
        "repo archaeology. Focus on missing requirements, risks, sequencing, and simpler "
        "alternatives. Do not implement.",
        "Readonly planning only. Do not edit files, commit, run shell, or request review gates. "
        "Ground findings in the plan's own domain and cite concrete references when possible.",
    )


def bounded_planning_reason(value: Any, *, limit: int = 600) -> str:
    """Redact and bound diagnostics with an explicit omission disclosure."""
    from ouroboros.observability import redact_projection
    from ouroboros.utils import truncate_review_artifact

    if value in (None, "", {}, []):
        return ""
    redacted_value = redact_projection(value).value
    if isinstance(redacted_value, str):
        text = redacted_value
    else:
        text = json.dumps(redacted_value, ensure_ascii=False, default=str, sort_keys=True)
    redacted = str(text).strip()
    if len(redacted) <= limit:
        return redacted

    # ``truncate_review_artifact`` is the existing disclosed-omission seam.  Its
    # limit covers the preview, so shrink that preview until the complete value
    # (including the omission metadata) fits this projection's bound.  Returning
    # the disclosure intact is more important than honoring an unrealistically
    # tiny caller limit.
    preview_limit = max(0, int(limit))
    while True:
        disclosed = truncate_review_artifact(redacted, limit=preview_limit)
        if len(disclosed) <= limit or preview_limit == 0:
            return disclosed
        preview_limit = max(0, preview_limit - max(1, len(disclosed) - limit))


def planning_handoff_selection(
    intended_scouts: List[Dict[str, Any]], tasks: Dict[str, Any], stop_reason: str
) -> tuple[List[str], List[Dict[str, Any]]]:
    """Select usable results and emit exactly one omission per unfulfilled intent."""
    included: List[str] = []
    omissions: List[Dict[str, Any]] = []
    terminal_statuses = {"completed", "failed", "cancelled", "rejected_duplicate"}
    for attempt in intended_scouts:
        role = str(attempt.get("role") or "")
        schedule_status = str(attempt.get("schedule_status") or "pending")
        task_ids = [str(item) for item in attempt.get("task_ids") or [] if str(item)]
        if schedule_status != "started" or not task_ids:
            reason = "schedule_failed" if schedule_status == "failed" else "schedule_outcome_unknown"
            omission = {"task_id": "", "role": role, "status": "not_started", "reason": reason}
            detail = bounded_planning_reason(attempt.get("schedule_reason"))
            if detail:
                omission["detail"] = detail
            omissions.append(omission)
            continue

        missing: List[tuple[str, Dict[str, Any]]] = []
        for task_id in task_ids:
            row = tasks.get(task_id) if isinstance(tasks, dict) else None
            row = row if isinstance(row, dict) else {}
            status = str(row.get("status") or "unknown").strip().lower() or "unknown"
            if status == "completed" and str(row.get("result") or "").strip():
                included.append(task_id)
            else:
                missing.append((task_id, row))
        if not missing:
            continue
        task_id, row = missing[0]
        status = str(row.get("status") or "unknown").strip().lower() or "unknown"
        if status not in terminal_statuses:
            reason = f"not_terminal_at_review_cutoff:{stop_reason or 'unknown'}"
        elif status == "completed":
            reason = "completed_without_nonempty_handoff"
        else:
            reason = f"terminal_without_usable_handoff:{status}"
        omission = {"task_id": task_id, "role": role, "status": status, "reason": reason}
        detail = bounded_planning_reason({
            key: row.get(key) for key in ("reason_code", "error", "result", "trace_summary") if row.get(key)
        })
        if detail:
            omission["detail"] = detail
        if len(missing) > 1:
            omission["task_ids"] = [item[0] for item in missing]
        omissions.append(omission)
    return list(dict.fromkeys(included)), omissions


def completed_planning_handoffs(tasks: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [
        row for row in (tasks or {}).values()
        if isinstance(row, dict)
        and str(row.get("status") or "").strip().lower() == "completed"
        and str(row.get("result") or "").strip()
    ]


def all_planning_tasks_terminal(task_ids: List[str], tasks: Dict[str, Any]) -> bool:
    terminal = {"completed", "failed", "cancelled", "rejected_duplicate"}
    return bool(task_ids) and all(
        isinstance(tasks.get(task_id), dict)
        and str(tasks[task_id].get("status") or "").strip().lower() in terminal
        for task_id in task_ids
    )


def format_planning_handoffs(handoffs: Dict[str, Any], *, raw: bool) -> str:
    if not handoffs:
        return ""
    wait = handoffs.get("wait") if isinstance(handoffs.get("wait"), dict) else {}
    tasks = (wait or {}).get("tasks") if isinstance((wait or {}).get("tasks"), dict) else {}
    included = [str(item) for item in (handoffs.get("included_task_ids") or [])]
    if not included:  # legacy schema: infer the same terminal non-empty subset
        included = [
            str(task_id) for task_id, row in tasks.items() if isinstance(row, dict)
            and str(row.get("status") or "").lower() == "completed"
            and str(row.get("result") or "").strip()
        ]
    if raw:
        selected = {task_id: tasks[task_id] for task_id in included if isinstance(tasks.get(task_id), dict)}
    else:
        selected = {
            task_id: {key: row.get(key) for key in (
                "status", "role", "result", "subagent_envelope"
            )}
            for task_id in included for row in [tasks.get(task_id)] if isinstance(row, dict)
        }
    payload = {
        "schema_version": handoffs.get("schema_version", 1),
        "included_task_ids": included,
        "consumed_task_ids": handoffs.get("consumed_task_ids") or [],
        "omissions": handoffs.get("omissions") or [],
        "timed_out": (wait or {}).get("timed_out"),
        "wait_stop_reason": handoffs.get("wait_stop_reason") or "",
        "wait_elapsed_sec": handoffs.get("wait_elapsed_sec"),
        "tasks": selected,
        "artifact": handoffs.get("artifact") or {},
    }
    return (
        "## Planning Subagent Handoffs\n\nEvery terminal non-empty scout handoff selected "
        "before the shared cutoff is reviewer evidence. Every other scout is listed under "
        "omissions; a later result is audit-only and cannot reopen this review.\n\n```json\n"
        + json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n```"
    )
