"""Shared multi-review substrate.

This module is the common cognitive primitive for migrated review surfaces and
the contract target for remaining legacy immune-system reviews. Slot identity is
separate from model identity, so duplicate model IDs are valid independent
reviewer slots.
"""

from __future__ import annotations

import asyncio
import json
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
from ouroboros.utils import sanitize_tool_result_for_log, truncate_review_artifact


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
        "acceptance criteria from the goal/spec yourself, then require that the evidence contains "
        "an EXECUTED check that MIRRORS what the real grader would run (the actual test/command "
        "and its observed output) — not a narrative that it passes. "
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
        f"Return JSON with keys: verdict (PASS|FAIL|DEGRADED){tier_keys}, findings "
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

        def _start_slot(slot: ReviewSlot) -> None:
            started_slots.append(slot)

            def _worker() -> None:
                try:
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
        # When tier classification is required, the contract is only ENFORCED if a
        # PASS without a valid outcome_tier cannot count toward a clean quorum —
        # otherwise a tier-less PASS aggregates PASS and the objective falls back
        # to the legacy mapping, defeating the required-tier prompt directive. A
        # FAIL still counts regardless of tier (conservative — never excuse a fail).
        classify_tier = bool((request.policy or {}).get("classify_outcome_tier"))
        _valid_tiers = {"solved", "best_effort", "blocked_with_evidence"}
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
            contract_ok = (
                isinstance(parsed, dict)
                and str(parsed.get("outcome_tier") or "").strip().lower() in _valid_tiers
                and bool(str(parsed.get("completion_coach") or "").strip())
            )
            if signal == "FAIL":
                fail_count += 1
            elif signal == "PASS" and classify_tier and not contract_ok:
                parse_degraded.append(f"{actor.slot_id}:missing_tier_or_coach")
            elif signal == "PASS":
                pass_count += 1
            elif signal == "DEGRADED":
                parse_degraded.append(f"{actor.slot_id}:degraded")
        min_successful = max(1, int((request.policy or {}).get("min_successful_slots") or 1))
        fail_closed_on_errors = bool((request.policy or {}).get("fail_closed_on_errors"))
        degraded_reasons = actor_errors + parse_degraded
        if fail_count:
            aggregate = "FAIL"
        elif pass_count >= min_successful and not (fail_closed_on_errors and actor_errors):
            aggregate = "PASS"
        else:
            aggregate = "DEGRADED"
            # Honest flag: DEGRADED must always carry a reason. Insufficient quorum
            # is itself the reason.
            if not degraded_reasons:
                degraded_reasons.append(
                    f"quorum_not_met: pass_count={pass_count} < min_successful={min_successful}"
                )
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
        try:
            chat_kwargs = {
                "messages": messages,
                "model": slot.model,
                "reasoning_effort": slot.effort,
                "max_tokens": int(request.max_tokens or slot.max_tokens),
                "temperature": request.temperature if request.temperature is not None else slot.temperature,
                "no_proxy": bool(request.no_proxy),
            }
            chat = getattr(self.llm, "chat", None)
            if callable(chat):
                msg, usage = chat(**chat_kwargs)
            else:
                msg, usage = asyncio.run(self.llm.chat_async(**chat_kwargs))
            raw_text = str(msg.get("content") or "")
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
