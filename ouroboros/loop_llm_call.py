"""
LLM call, retry, pricing, and usage-event logic for the main loop.

Handles model pricing estimation, cost tracking, per-call retry with backoff,
and real-time usage event emission.
Extracted from loop.py to keep the main loop orchestrator focused.
"""

from __future__ import annotations

import os
import pathlib
import queue
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import logging

from ouroboros.llm import LLMClient, LocalContextTooLargeError, add_usage
from ouroboros.observability import new_call_id, new_execution_id, persist_call
from ouroboros.pricing import emit_llm_usage_event, estimate_cost, infer_model_category
from ouroboros.utils import append_jsonl, emit_log_event, sanitize_tool_result_for_log, utc_now_iso
from ouroboros.config import get_context_mode

log = logging.getLogger(__name__)

MAIN_LOOP_MAX_TOKENS = 65_536

# Per-class retry policy: TRANSIENT provider failures (finish_reason=null
# glitches, 429/5xx/overloaded) get a larger same-model attempt budget than
# permanent classes, because the owner deliberately runs single-model setups
# (all slots = one model, empty fallback) for clean measurement and a 3-attempt
# cap turned recoverable provider blips into whole-task "No viable fallback
# model configured" deaths. Permanent classes (auth/quota/bad_request/too
# large) keep failing fast. There is NO cross-model fallback here — the same
# request is retried on the SAME model.
_TRANSIENT_RETRY_KINDS = frozenset({"provider_transient", "provider_incomplete_response"})
_TRANSIENT_RETRY_DEFAULT = 6
_TRANSIENT_BACKOFF_CAP_SEC = 60.0
# Stop retrying when the remaining task deadline cannot absorb the backoff
# sleep plus a useful follow-up attempt — burning the last minutes of a task
# deadline sleeping between retries is worse than failing visibly.
_DEADLINE_RETRY_FLOOR_SEC = 10.0


def transient_retry_max(default_retries: int) -> int:
    """Attempt budget for transient provider failure classes.

    Tunable via OUROBOROS_TRANSIENT_RETRY_MAX (SSOT default in
    config.SETTINGS_DEFAULTS); never below the caller's default budget so
    misconfiguration cannot reduce existing resilience.
    """
    try:
        from ouroboros.config import SETTINGS_DEFAULTS
        default_value = int(SETTINGS_DEFAULTS.get("OUROBOROS_TRANSIENT_RETRY_MAX", _TRANSIENT_RETRY_DEFAULT))
    except Exception:
        default_value = _TRANSIENT_RETRY_DEFAULT
    raw = os.environ.get("OUROBOROS_TRANSIENT_RETRY_MAX", "").strip()
    try:
        value = int(raw) if raw else default_value
    except ValueError:
        value = default_value
    return max(int(default_retries), value)


def _empty_response_log_msg(usage: Dict[str, Any], is_provider_glitch: bool, accumulated_usage: Dict[str, Any]) -> str:
    """Honest message for an empty/incomplete LLM response. A transient provider
    body-error (OpenRouter 429/5xx inside an HTTP 200, surfaced as usage
    ``provider_error``) that a same-model reroute could not escape is classified
    as the real rate_limit/provider_transient kind instead of a blank
    finish_reason=null glitch."""
    provider_error = usage.get("provider_error") if isinstance(usage, dict) else None
    if isinstance(provider_error, dict):
        accumulated_usage["_last_llm_error_kind"] = str(provider_error.get("kind") or "provider_transient")
        return f"Provider returned a body error (code={provider_error.get('code')}): {provider_error.get('message')}"
    if is_provider_glitch:
        return "Provider returned incomplete response (finish_reason=null)"
    return "LLM returned empty response (no content, no tool_calls)"


def _classify_empty_response(usage: Dict[str, Any], msg: Dict[str, Any]) -> Tuple[str, bool, bool]:
    """Classify an empty / no-tool-call response → (event_type, is_provider_glitch,
    permanent_body_error). A TYPED non-transient body error (WA1 kind
    ``provider_error``: auth / quota / bad_request) is PERMANENT — a same-model
    reroute already failed in the transport, so retrying here only burns the
    transient budget. Only rate_limit / provider_transient body errors and a bare
    ``finish_reason=null`` glitch are retryable."""
    finish_reason = msg.get("finish_reason") or msg.get("stop_reason")
    is_provider_glitch = finish_reason is None
    body_err = usage.get("provider_error") if isinstance(usage, dict) else None
    body_kind = str((body_err or {}).get("kind") or "") if isinstance(body_err, dict) else ""
    permanent_body_error = bool(body_err) and body_kind not in ("rate_limit", "provider_transient")
    if permanent_body_error:
        event_type = "provider_body_error"
    elif is_provider_glitch:
        event_type = "provider_incomplete_response"
    else:
        event_type = "llm_empty_response"
    return event_type, is_provider_glitch, permanent_body_error


def _sleep_within_deadline(seconds: float, deadline_ts: Optional[float]) -> bool:
    """Sleep ``seconds`` if the task deadline (epoch seconds) allows another
    attempt afterwards. Returns False — without sleeping — when the remaining
    time budget cannot absorb the backoff, signalling the caller to stop."""
    if deadline_ts:
        remaining = float(deadline_ts) - time.time()
        if remaining < float(seconds) + _DEADLINE_RETRY_FLOOR_SEC:
            return False
    time.sleep(float(seconds))
    return True


def _emit_retry_deadline_exhausted(
    drive_logs: pathlib.Path,
    *,
    task_id: str,
    execution_id: str,
    round_id: str,
    round_idx: int,
    attempt: int,
    model: str,
    error_kind: str,
) -> None:
    """Durable record that retries stopped because the deadline could not
    absorb another backoff sleep (emitted by BOTH transient failure paths)."""
    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(), "type": "llm_retry_deadline_exhausted",
        "task_id": task_id,
        "execution_id": execution_id,
        "round_id": round_id,
        "round": round_idx, "attempt": attempt + 1,
        "model": model,
        "error_kind": error_kind,
    })


@dataclass
class _LlmErrorContext:
    task_id: str
    task_type: str
    execution_id: str
    round_id: str
    llm_call_id: str
    round_idx: int
    attempt: int
    model: str
    request_ref: Optional[Dict[str, Any]]
    drive_logs: pathlib.Path
    event_queue: Optional[queue.Queue]
    accumulated_usage: Dict[str, Any]


@dataclass(frozen=True)
class LlmErrorClassification:
    kind: str
    retry_same_request: bool
    status_code: Optional[int] = None
    provider_code: str = ""


def _emit_live_log(event_queue: Optional[queue.Queue], payload: Dict[str, Any]) -> None:
    """Thin wrapper around the SSOT helper — keeps the call-site signature stable."""
    emit_log_event(
        event_queue,
        {"ts": utc_now_iso(), **payload},
        log_label="LLM live",
    )


def _short_error_text(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split()).strip()
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


_CONTEXT_OVERFLOW_MARKERS = (
    "context_length_exceeded",
    "context length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "reduce the length",
    "exceeds the context",
    "context window",
    "input is too long",
)
_NON_RETRYABLE_PROVIDER_MARKERS = {
    "quota_exhausted": (
        "insufficient credits",
        "insufficient_credit",
        "insufficient_quota",
        "quota exceeded",
        "billing",
        "payment required",
        "402",
    ),
    "auth_error": (
        "invalid_api_key",
        "unauthorized",
        "forbidden",
        "401",
        "403",
    ),
    "request_too_large": (
        "max_tokens",
        "maximum tokens",
        "output tokens",
        "maximum output",
        "too many tokens",
        "context_length_exceeded",
        "context length",
        "maximum context",
        "prompt is too long",
        "exceeds the context",
    ),
    "bad_request": (
        "badrequest",
        "bad request",
        "conversation must end with a user message",
        "prefill",
        "unsupported",
        "invalid request",
        "400",
    ),
}
_RETRYABLE_PROVIDER_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "timeout",
    "temporarily",
    "server error",
    "502",
    "503",
    "504",
)
_RATE_LIMIT_TEXT_MARKERS = (
    "rate limit",
    "rate_limit",
    "429",
    "tokens per minute",
    "requests per minute",
    "token per minute",
    "request per minute",
    "tpm",
    "rpm",
)


def _is_rate_limit_text(text: str) -> bool:
    low = str(text or "").lower()
    return any(marker in low for marker in _RATE_LIMIT_TEXT_MARKERS)


def _is_context_overflow_error(exc: Exception, safe_error: str) -> bool:
    """Classify local/remote context-window overflow (drives the low-mode hint)."""
    if isinstance(exc, LocalContextTooLargeError):
        return True
    low = str(safe_error or "").lower()
    if _is_rate_limit_text(low):
        return False
    return any(marker in low for marker in _CONTEXT_OVERFLOW_MARKERS)


def _exception_status_code(exc: Exception) -> Optional[int]:
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            try:
                return int(value)
            except ValueError:
                pass
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _exception_provider_code(exc: Exception, safe_error: str) -> str:
    for attr in ("code", "type"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        for key in ("code", "type"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = body.get("error")
        if isinstance(nested, dict):
            for key in ("code", "type"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return ""


def _provider_code_kind(provider_code: str) -> str:
    code = str(provider_code or "").strip().lower()
    if not code:
        return ""
    for kind, markers in _NON_RETRYABLE_PROVIDER_MARKERS.items():
        if code == kind or any(code == str(marker).lower() or str(marker).lower() in code for marker in markers):
            return kind
    return ""


def classify_llm_exception(exc: Exception, safe_error: str = "") -> LlmErrorClassification:
    """Classify provider errors without changing model/request semantics."""

    safe = safe_error or sanitize_tool_result_for_log(repr(exc))
    if isinstance(exc, LocalContextTooLargeError):
        return LlmErrorClassification("context_overflow", False)
    status_code = _exception_status_code(exc)
    provider_code = _exception_provider_code(exc, safe)
    low = str(safe or "").lower()
    provider_kind = _provider_code_kind(provider_code)
    if provider_kind:
        return LlmErrorClassification(provider_kind, False, status_code, provider_code)
    if status_code == 429:
        return LlmErrorClassification("provider_transient", True, status_code, provider_code)
    if _is_rate_limit_text(low):
        return LlmErrorClassification("provider_transient", True, status_code, provider_code)
    if _is_context_overflow_error(exc, safe):
        return LlmErrorClassification("context_overflow", False, status_code, provider_code)
    for kind, markers in _NON_RETRYABLE_PROVIDER_MARKERS.items():
        if any(marker in low for marker in markers):
            return LlmErrorClassification(kind, False, status_code, provider_code)
    if status_code in {400, 401, 402, 403, 413, 422}:
        kind = {
            400: "bad_request",
            401: "auth_error",
            402: "quota_exhausted",
            403: "auth_error",
            413: "request_too_large",
            422: "bad_request",
        }[status_code]
        return LlmErrorClassification(kind, False, status_code, provider_code)
    if status_code in {408, 500, 502, 503, 504}:
        return LlmErrorClassification("provider_transient", True, status_code, provider_code)
    if any(marker in low for marker in _RETRYABLE_PROVIDER_MARKERS):
        return LlmErrorClassification("provider_transient", True, status_code, provider_code)
    return LlmErrorClassification("provider_error", True, status_code, provider_code)


def _remember_llm_call(
    usage: Dict[str, Any],
    *,
    llm_call_id: str,
    execution_id: str,
    round_id: str,
    round_idx: int,
    attempt: int,
    model: str,
    display_model: str,
    provider: str,
    request_ref: Dict[str, Any],
    response_ref: Dict[str, Any],
) -> None:
    call_meta = {
        "llm_call_id": llm_call_id,
        "execution_id": execution_id,
        "round_id": round_id,
        "round": round_idx,
        "attempt": attempt,
        "model": model,
        "resolved_model": display_model,
        "provider": provider,
        "request_ref": request_ref.get("manifest_ref") if request_ref else None,
        "response_ref": response_ref.get("manifest_ref") if response_ref else None,
    }
    usage["_last_llm_call_meta"] = call_meta
    usage.setdefault("llm_call_refs", []).append(call_meta)


def _normalize_usage_cost(
    usage: Dict[str, Any],
    *,
    model: str,
    use_local: bool,
) -> tuple[float, str, str, bool]:
    provider_reported_cost = bool(usage.get("cost"))
    cost = float(usage.get("cost") or 0)
    display_model = str(usage.get("resolved_model") or model)
    provider = "local" if use_local else str(usage.get("provider") or "openrouter")
    if use_local:
        cost = 0.0
        display_model = f"{model} (local)"
    elif cost == 0.0:
        cost = estimate_cost(
            display_model,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
            int(usage.get("cached_tokens") or 0),
            int(usage.get("cache_write_tokens") or 0),
            usage.get("prompt_cache_ttl"),
        )
    usage["cost"] = cost
    cost_estimated = bool(usage.get("cost_estimated")) or (bool(cost) and not provider_reported_cost)
    return cost, display_model, provider, cost_estimated


def _record_llm_call_error(
    error: Exception,
    ctx: _LlmErrorContext,
) -> bool:
    """Record and classify an LLM-round exception.

    Emits the live ``llm_round_error`` log and the durable ``llm_api_error``
    event, marks the usage as infra-failed, and writes context-overflow
    diagnostics. A remote-context overflow outside low context mode sets the
    one-time owner hint (``context_overflow_suggest_low``). Returns True for a
    local context overflow, signalling the caller to stop retrying.
    """
    safe_error = sanitize_tool_result_for_log(repr(error))
    classification = classify_llm_exception(error, safe_error)
    _emit_live_log(ctx.event_queue, {
        "type": "llm_round_error",
        "task_id": ctx.task_id,
        "task_type": ctx.task_type,
        "execution_id": ctx.execution_id,
        "round_id": ctx.round_id,
        "llm_call_id": ctx.llm_call_id,
        "round": ctx.round_idx,
        "attempt": ctx.attempt + 1,
        "model": ctx.model,
        "error": safe_error,
        "error_kind": classification.kind,
        "retry_same_request": classification.retry_same_request,
    })
    append_jsonl(ctx.drive_logs / "events.jsonl", {
        "ts": utc_now_iso(), "type": "llm_api_error",
        "task_id": ctx.task_id,
        "execution_id": ctx.execution_id,
        "round_id": ctx.round_id,
        "llm_call_id": ctx.llm_call_id,
        "round": ctx.round_idx, "attempt": ctx.attempt + 1,
        "model": ctx.model, "error": safe_error,
        "error_kind": classification.kind,
        "retry_same_request": classification.retry_same_request,
        "status_code": classification.status_code,
        "provider_code": classification.provider_code,
        "request_ref": ctx.request_ref.get("manifest_ref") if ctx.request_ref else None,
    })
    ctx.accumulated_usage["_last_llm_error"] = _short_error_text(safe_error)
    ctx.accumulated_usage["_last_llm_error_kind"] = classification.kind
    ctx.accumulated_usage["_last_llm_retry_same_request"] = classification.retry_same_request
    if classification.status_code:
        ctx.accumulated_usage["_last_llm_status_code"] = classification.status_code
    if classification.provider_code:
        ctx.accumulated_usage["_last_llm_provider_code"] = classification.provider_code
    ctx.accumulated_usage["execution_status"] = "infra_failed"
    ctx.accumulated_usage["reason_code"] = "llm_api_error"
    # Context-window overflow while NOT already in low: surface a one-time owner
    # hint to switch to low context mode (rendered by the recovery-hint helper).
    if get_context_mode() != "low" and classification.kind == "context_overflow":
        ctx.accumulated_usage["context_overflow_suggest_low"] = True
        append_jsonl(ctx.drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "context_overflow_suggest_low",
            "task_id": ctx.task_id,
            "execution_id": ctx.execution_id,
            "round": ctx.round_idx,
            "attempt": ctx.attempt + 1,
            "model": ctx.model,
            "error": safe_error,
        })
    if classification.kind == "context_overflow":
        overflow_event_type = "local_context_overflow" if isinstance(error, LocalContextTooLargeError) else "remote_context_overflow"
        append_jsonl(ctx.drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": overflow_event_type,
            "task_id": ctx.task_id,
            "execution_id": ctx.execution_id,
            "round_id": ctx.round_id,
            "llm_call_id": ctx.llm_call_id,
            "round": ctx.round_idx,
            "attempt": ctx.attempt + 1,
            "model": ctx.model,
            "error": safe_error,
        })
        return True
    if not classification.retry_same_request:
        append_jsonl(ctx.drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "llm_non_retryable_same_request",
            "task_id": ctx.task_id,
            "execution_id": ctx.execution_id,
            "round_id": ctx.round_id,
            "llm_call_id": ctx.llm_call_id,
            "round": ctx.round_idx,
            "attempt": ctx.attempt + 1,
            "model": ctx.model,
            "error_kind": classification.kind,
            "status_code": classification.status_code,
            "provider_code": classification.provider_code,
        })
        return True
    return False


def _emit_empty_response_events(
    event_type: str,
    *,
    event_queue: Optional[queue.Queue],
    drive_logs: pathlib.Path,
    base: Dict[str, Any],
    task_type: str,
    details: Dict[str, Any],
) -> None:
    """Emit the live log + durable event for an empty/incomplete LLM response.

    ``details`` carries the durable-event-only payload: content, tool_calls,
    request_ref, response_ref.
    """
    content = details.get("content")
    tool_calls = details.get("tool_calls")
    request_ref = details.get("request_ref") or {}
    response_ref = details.get("response_ref") or {}
    _emit_live_log(event_queue, {"type": event_type, "task_type": task_type, **base})
    append_jsonl(drive_logs / "events.jsonl", {
        "ts": utc_now_iso(), "type": event_type,
        **base,
        "raw_content": repr(content)[:500] if content else None,
        "raw_tool_calls": repr(tool_calls)[:500] if tool_calls else None,
        "request_ref": request_ref.get("manifest_ref") if request_ref else None,
        "response_ref": response_ref.get("manifest_ref") if response_ref else None,
    })


def call_llm_with_retry(
    llm: LLMClient,
    messages: List[Dict[str, Any]],
    model: str,
    tools: Optional[List[Dict[str, Any]]],
    effort: str,
    max_retries: int,
    drive_logs: pathlib.Path,
    task_id: str,
    round_idx: int,
    event_queue: Optional[queue.Queue],
    accumulated_usage: Dict[str, Any],
    task_type: str = "",
    use_local: bool = False,
    deadline_ts: Optional[float] = None,
) -> Tuple[Optional[Dict[str, Any]], float]:
    """
    Call LLM with retry logic, usage tracking, and event emission.

    Retry budgets are per failure class: transient provider failures
    (finish_reason=null, 429/5xx/overloaded) may use up to
    ``transient_retry_max(max_retries)`` same-model attempts; every other
    retryable class keeps the caller's ``max_retries``. ``deadline_ts``
    (epoch seconds) bounds backoff sleeps so retries never eat the remaining
    task deadline. No cross-model fallback happens here.

    Returns:
        (response_message, cost) on success
        (None, 0.0) on failure after the class's attempt budget
    """
    msg = None
    drive_root = pathlib.Path(drive_logs).parent
    execution_id = str(accumulated_usage.setdefault("execution_id", new_execution_id()))
    round_id = f"{execution_id}:round:{round_idx}"
    transient_budget = transient_retry_max(max_retries)

    for attempt in range(transient_budget):
        accumulated_usage["_llm_attempts_used"] = attempt + 1
        llm_call_id = new_call_id("llm")
        request_ref: Dict[str, Any] = {}
        try:
            _emit_live_log(event_queue, {
                "type": "llm_round_started",
                "task_id": task_id,
                "task_type": task_type,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": model,
                "reasoning_effort": effort,
                "use_local": bool(use_local),
            })
            kwargs = {
                "messages": messages,
                "model": model,
                "reasoning_effort": effort,
                "max_tokens": MAIN_LOOP_MAX_TOKENS,
                "use_local": use_local,
            }
            if tools:
                kwargs["tools"] = tools
            try:
                request_ref = persist_call(
                    drive_root,
                    task_id=task_id,
                    call_id=f"{llm_call_id}_request",
                    call_type="llm_request",
                    payload={
                        "messages": messages,
                        "tools": tools or [],
                        "model": model,
                        "reasoning_effort": effort,
                        "max_tokens": MAIN_LOOP_MAX_TOKENS,
                        "use_local": bool(use_local),
                    },
                    manifest={
                        "execution_id": execution_id,
                        "round_id": round_id,
                        "llm_call_id": llm_call_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "reasoning_effort": effort,
                    },
                )
            except Exception:
                log.debug("Failed to persist LLM request observability payload", exc_info=True)
            resp_msg, usage = llm.chat(**kwargs)
            msg = resp_msg
            accumulated_usage.pop("_last_llm_error", None)
            accumulated_usage.pop("_last_llm_error_kind", None)
            accumulated_usage.pop("_last_llm_retry_same_request", None)
            accumulated_usage.pop("_last_llm_status_code", None)
            accumulated_usage.pop("_last_llm_provider_code", None)
            accumulated_usage.pop("context_overflow_suggest_low", None)

            cost, display_model, provider, cost_estimated = _normalize_usage_cost(
                usage,
                model=model,
                use_local=use_local,
            )
            add_usage(accumulated_usage, usage)
            response_ref: Dict[str, Any] = {}
            try:
                response_ref = persist_call(
                    drive_root,
                    task_id=task_id,
                    call_id=f"{llm_call_id}_response",
                    call_type="llm_response",
                    payload={
                        "message": msg,
                        "usage": usage,
                    },
                    manifest={
                        "execution_id": execution_id,
                        "round_id": round_id,
                        "llm_call_id": llm_call_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "resolved_model": display_model,
                        "provider": provider,
                    },
                )
            except Exception:
                log.debug("Failed to persist LLM response observability payload", exc_info=True)
            _remember_llm_call(
                accumulated_usage,
                llm_call_id=llm_call_id,
                execution_id=execution_id,
                round_id=round_id,
                round_idx=round_idx,
                attempt=attempt + 1,
                model=model,
                display_model=display_model,
                provider=provider,
                request_ref=request_ref,
                response_ref=response_ref,
            )

            category = task_type if task_type in ("evolution", "consciousness", "review", "summarize") else "task"
            emit_llm_usage_event(
                event_queue,
                task_id,
                display_model,
                usage,
                cost,
                category,
                provider=provider,
                source="loop",
                cost_estimated=cost_estimated,
            )

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")
            if not tool_calls and (not content or not content.strip()):
                finish_reason = msg.get("finish_reason") or msg.get("stop_reason")
                event_type, is_provider_glitch, permanent_body_error = _classify_empty_response(usage, msg)
                log_msg = _empty_response_log_msg(usage, is_provider_glitch, accumulated_usage)
                log.warning("%s, attempt %d/%d", log_msg, attempt + 1, transient_budget)
                _emit_empty_response_events(
                    event_type,
                    event_queue=event_queue,
                    drive_logs=drive_logs,
                    base={
                        "task_id": task_id,
                        "execution_id": execution_id,
                        "round_id": round_id,
                        "llm_call_id": llm_call_id,
                        "round": round_idx,
                        "attempt": attempt + 1,
                        "model": model,
                        "finish_reason": finish_reason,
                    },
                    task_type=task_type,
                    details={"content": content, "tool_calls": tool_calls,
                             "request_ref": request_ref, "response_ref": response_ref},
                )
                accumulated_usage["_last_llm_error"] = _short_error_text(log_msg)
                accumulated_usage["execution_status"] = (
                    "infra_failed" if (is_provider_glitch and not permanent_body_error) else "failed"
                )
                accumulated_usage["reason_code"] = event_type

                # Transient response glitches (and transient body errors) retry the
                # SAME model within the transient budget, deadline-bounded. A
                # PERMANENT body error (auth/quota/bad_request) fails fast — no
                # retry — so the loop unifies into best-effort terminalization
                # instead of burning the budget on a request that cannot succeed.
                if not permanent_body_error and attempt < transient_budget - 1:
                    if _sleep_within_deadline(
                        min(2.0 ** attempt, _TRANSIENT_BACKOFF_CAP_SEC), deadline_ts
                    ):
                        continue
                    _emit_retry_deadline_exhausted(
                        drive_logs, task_id=task_id, execution_id=execution_id,
                        round_id=round_id, round_idx=round_idx, attempt=attempt,
                        model=model, error_kind=event_type,
                    )
                return None, cost

            accumulated_usage.pop("execution_status", None)
            accumulated_usage.pop("result_status", None)
            accumulated_usage.pop("reason_code", None)
            accumulated_usage["rounds"] = accumulated_usage.get("rounds", 0) + 1

            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = int(usage.get("cached_tokens") or 0)
            cache_write_tokens = int(usage.get("cache_write_tokens") or 0)
            prompt_cache_ttl = str(usage.get("prompt_cache_ttl") or "")
            cache_hit_rate = (cached_tokens / prompt_tokens) if prompt_tokens > 0 else 0.0
            _round_event = {
                "ts": utc_now_iso(), "type": "llm_round",
                "task_id": task_id,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx, "model": display_model,
                "reasoning_effort": effort,
                "provider": provider,
                "source": "loop",
                "model_category": infer_model_category(display_model),
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "cache_write_tokens": cache_write_tokens,
                "prompt_cache_ttl": prompt_cache_ttl,
                "cache_hit_rate": cache_hit_rate,
                "cost_usd": cost,
                "request_ref": request_ref.get("manifest_ref") if request_ref else None,
                "response_ref": response_ref.get("manifest_ref") if response_ref else None,
            }
            _emit_live_log(event_queue, {
                "type": "llm_round_finished",
                "task_id": task_id,
                "task_type": task_type,
                "execution_id": execution_id,
                "round_id": round_id,
                "llm_call_id": llm_call_id,
                "round": round_idx,
                "attempt": attempt + 1,
                "model": display_model,
                "reasoning_effort": effort,
                "prompt_tokens": _round_event["prompt_tokens"],
                "completion_tokens": _round_event["completion_tokens"],
                "cached_tokens": _round_event["cached_tokens"],
                "cache_write_tokens": _round_event["cache_write_tokens"],
                "prompt_cache_ttl": _round_event["prompt_cache_ttl"],
                "cost_usd": cost,
                "response_kind": "tool_calls" if tool_calls else "message",
                "tool_call_count": len(tool_calls),
                "has_text": bool(content and str(content).strip()),
            })
            append_jsonl(drive_logs / "events.jsonl", _round_event)
            return msg, cost

        except Exception as e:
            if _record_llm_call_error(
                e,
                _LlmErrorContext(
                    task_id=task_id,
                    task_type=task_type,
                    execution_id=execution_id,
                    round_id=round_id,
                    llm_call_id=llm_call_id,
                    round_idx=round_idx,
                    attempt=attempt,
                    model=model,
                    request_ref=request_ref,
                    drive_logs=drive_logs,
                    event_queue=event_queue,
                    accumulated_usage=accumulated_usage,
                ),
            ):
                break
            error_kind = str(accumulated_usage.get("_last_llm_error_kind") or "")
            is_transient = error_kind in _TRANSIENT_RETRY_KINDS
            attempt_budget = transient_budget if is_transient else max_retries
            if attempt >= attempt_budget - 1:
                break
            backoff = min(
                2.0 ** attempt * 4,
                _TRANSIENT_BACKOFF_CAP_SEC if is_transient else 30.0,
            )
            if not _sleep_within_deadline(backoff, deadline_ts):
                _emit_retry_deadline_exhausted(
                    drive_logs, task_id=task_id, execution_id=execution_id,
                    round_id=round_id, round_idx=round_idx, attempt=attempt,
                    model=model, error_kind=error_kind,
                )
                break

    return None, 0.0
