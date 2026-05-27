"""Shared multi-review substrate.

This module is the common cognitive primitive for migrated review surfaces and
the contract target for remaining legacy immune-system reviews. Slot identity is
separate from model identity, so duplicate model IDs are valid independent
reviewer slots.
"""

from __future__ import annotations

import concurrent.futures
import json
import pathlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List

from ouroboros.config import get_review_models
from ouroboros.llm import LLMClient
from ouroboros.observability import new_call_id, persist_call
from ouroboros.triad_review import extract_json_array
from ouroboros.utils import sanitize_tool_result_for_log, utc_now_iso


@dataclass(frozen=True)
class ReviewSlot:
    slot_id: str
    model: str
    effort: str = "medium"
    timeout_sec: int = 300
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


@dataclass
class ReviewActorRecord:
    slot_id: str
    model: str
    status: str
    raw_text: str = ""
    parsed: Any = None
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
        "Return JSON with keys: verdict (PASS|FAIL|DEGRADED), findings "
        "([{severity, item, evidence, recommendation}]), and summary. "
        "If you cannot judge because evidence is missing, return DEGRADED and explain."
    )


def _parse_findings(raw_text: str) -> tuple[Any, List[Dict[str, Any]], str]:
    text = str(raw_text or "").strip()
    parsed: Any = None
    findings: List[Dict[str, Any]] = []
    signal = "UNKNOWN"
    try:
        parsed = json.loads(text)
    except Exception:
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

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(len(slots), 8))
        future_by_slot = {
            pool.submit(self._run_slot, request, slot): slot
            for slot in slots
        }
        actors: List[ReviewActorRecord] = []
        try:
            try:
                completed = concurrent.futures.as_completed(
                    future_by_slot,
                    timeout=max(1, max(int(slot.timeout_sec or 1) for slot in slots)),
                )
                for future in completed:
                    slot = future_by_slot[future]
                    try:
                        actors.append(future.result(timeout=0))
                    except Exception as exc:
                        actors.append(ReviewActorRecord(
                            slot_id=slot.slot_id,
                            model=slot.model,
                            status="error",
                            error=sanitize_tool_result_for_log(f"{type(exc).__name__}: {exc}"),
                        ))
            except concurrent.futures.TimeoutError:
                pass
            seen = {actor.slot_id for actor in actors}
            for future, slot in future_by_slot.items():
                if slot.slot_id in seen:
                    continue
                future.cancel()
                actors.append(ReviewActorRecord(
                    slot_id=slot.slot_id,
                    model=slot.model,
                    status="error",
                    error=sanitize_tool_result_for_log(f"Timeout after {slot.timeout_sec}s"),
                ))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        slot_order = {slot.slot_id: idx for idx, slot in enumerate(slots)}
        actors.sort(key=lambda actor: slot_order.get(actor.slot_id, len(slot_order)))

        all_findings: List[Dict[str, Any]] = []
        degraded_reasons: List[str] = []
        fail_count = 0
        pass_count = 0
        for actor in actors:
            if actor.status == "error":
                degraded_reasons.append(f"{actor.slot_id}:{actor.error}")
            elif actor.status != "ok":
                degraded_reasons.append(f"{actor.slot_id}:{actor.status}")
            parsed, findings, signal = _parse_findings(actor.raw_text)
            actor.parsed = parsed
            all_findings.extend({**item, "slot_id": actor.slot_id, "model": actor.model} for item in findings)
            if signal == "FAIL":
                fail_count += 1
            elif signal == "PASS":
                pass_count += 1
            elif signal == "DEGRADED":
                degraded_reasons.append(f"{actor.slot_id}:degraded")
        min_successful = max(1, int((request.policy or {}).get("min_successful_slots") or 1))
        fail_closed_on_errors = bool((request.policy or {}).get("fail_closed_on_errors"))
        if fail_count:
            aggregate = "FAIL"
        elif pass_count >= min_successful and not (fail_closed_on_errors and degraded_reasons):
            aggregate = "PASS"
        else:
            aggregate = "DEGRADED"
        return ReviewRunResult(
            request=asdict(request),
            actors=[asdict(actor) for actor in actors],
            parsed_findings=all_findings,
            aggregate_signal=aggregate,
            degraded=bool(degraded_reasons),
            degraded_reasons=degraded_reasons,
        )

    def _run_slot(self, request: ReviewRequest, slot: ReviewSlot) -> ReviewActorRecord:
        prompt = _render_prompt(request, slot)
        call_id = new_call_id(f"review_{request.surface}_{slot.slot_id}")
        prompt_ref: Dict[str, Any] = {}
        response_ref: Dict[str, Any] = {}
        start = time.time()
        try:
            prompt_ref = persist_call(
                self.drive_root,
                task_id=request.task_id or "review",
                call_id=f"{call_id}_prompt",
                call_type="review_prompt",
                payload={"request": asdict(request), "slot": asdict(slot), "prompt": prompt},
                manifest={"surface": request.surface, "slot_id": slot.slot_id, "model": slot.model},
            )
        except Exception:
            prompt_ref = {}
        try:
            msg, usage = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                model=slot.model,
                reasoning_effort=slot.effort,
                max_tokens=slot.max_tokens,
                temperature=slot.temperature,
            )
            raw_text = str(msg.get("content") or "")
            self._emit_usage(request, slot, usage, prompt_chars=len(prompt))
            try:
                response_ref = persist_call(
                    self.drive_root,
                    task_id=request.task_id or "review",
                    call_id=f"{call_id}_response",
                    call_type="review_response",
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
            return ReviewActorRecord(
                slot_id=slot.slot_id,
                model=slot.model,
                status="error",
                error=sanitize_tool_result_for_log(f"{type(exc).__name__}: {exc}"),
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
