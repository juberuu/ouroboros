from unittest.mock import patch

from ouroboros.loop import _provider_failure_hint
from ouroboros.loop_llm_call import call_llm_with_retry, classify_llm_exception


class _FailingLLM:
    def chat(self, **kwargs):
        raise RuntimeError("AuthenticationError('401 invalid_api_key')")


class _QuotaFailingLLM:
    calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise RuntimeError("Provider returned 402 insufficient credits")


class _SuccessfulLLM:
    def chat(self, **kwargs):
        return {"content": "ok"}, {"provider": "anthropic", "resolved_model": "anthropic/claude-sonnet-4-6"}


class _ProviderError(Exception):
    def __init__(self, message, *, status_code=None, code=None):
        super().__init__(message)
        self.status_code = status_code
        if code is not None:
            self.code = code


def test_call_llm_with_retry_records_last_error(tmp_path):
    usage = {}

    msg, cost = call_llm_with_retry(
        _FailingLLM(),
        [{"role": "user", "content": "hi"}],
        "openai::gpt-5.5",
        None,
        "medium",
        1,
        tmp_path,
        "task-1",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg is None
    assert cost == 0.0
    assert "invalid_api_key" in usage["_last_llm_error"]
    assert usage["_last_llm_error_kind"] == "auth_error"
    assert usage["_last_llm_retry_same_request"] is False


def test_call_llm_with_retry_clears_stale_last_error_on_success(tmp_path):
    usage = {
        "_last_llm_error": "old error",
        "_last_llm_error_kind": "auth_error",
        "context_overflow_suggest_low": True,
    }

    msg, _cost = call_llm_with_retry(
        _SuccessfulLLM(),
        [{"role": "user", "content": "hi"}],
        "anthropic::claude-sonnet-4-6",
        None,
        "medium",
        1,
        tmp_path,
        "task-2",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg == {"content": "ok"}
    assert "_last_llm_error" not in usage
    assert "_last_llm_error_kind" not in usage
    assert "context_overflow_suggest_low" not in usage


def test_call_llm_with_retry_stops_non_retryable_same_request(tmp_path):
    usage = {}
    llm = _QuotaFailingLLM()

    msg, cost = call_llm_with_retry(
        llm,
        [{"role": "user", "content": "hi"}],
        "google/gemini-3.5-flash",
        None,
        "medium",
        3,
        tmp_path,
        "task-quota",
        1,
        None,
        usage,
        "task",
        False,
    )

    assert msg is None
    assert cost == 0.0
    assert llm.calls == 1
    assert usage["_last_llm_error_kind"] == "quota_exhausted"
    assert usage["_last_llm_retry_same_request"] is False


def test_classify_llm_exception_distinguishes_retryable_rate_limit():
    rate = classify_llm_exception(RuntimeError("429 rate limit exceeded"))
    quota = classify_llm_exception(RuntimeError("402 insufficient credits"))

    assert rate.kind == "provider_transient"
    assert rate.retry_same_request is True
    assert quota.kind == "quota_exhausted"
    assert quota.retry_same_request is False


def test_classify_llm_exception_uses_provider_code_before_429_status():
    quota = classify_llm_exception(
        _ProviderError("rate limit transport status", status_code=429, code="insufficient_quota")
    )

    assert quota.kind == "quota_exhausted"
    assert quota.retry_same_request is False
    assert quota.status_code == 429
    assert quota.provider_code == "insufficient_quota"


def test_classify_llm_exception_keeps_429_token_rate_retryable():
    rate = classify_llm_exception(
        _ProviderError("429 too many tokens per minute", status_code=429)
    )

    assert rate.kind == "provider_transient"
    assert rate.retry_same_request is True


def test_classify_llm_exception_keeps_text_only_token_rate_retryable():
    rate = classify_llm_exception(RuntimeError("Rate limit reached: too many tokens per minute"))
    plain_429 = classify_llm_exception(RuntimeError("429 too many tokens per minute"))

    assert rate.kind == "provider_transient"
    assert rate.retry_same_request is True
    assert plain_429.kind == "provider_transient"
    assert plain_429.retry_same_request is True


def test_provider_failure_hint_formats_detail():
    hint = _provider_failure_hint({"_last_llm_error": "  AuthenticationError('401 invalid_api_key')  "})

    assert hint == " Last provider error: AuthenticationError('401 invalid_api_key')"


def test_provider_failure_hint_empty_without_error():
    assert _provider_failure_hint({}) == ""


def test_call_llm_with_retry_accumulates_estimated_cost(tmp_path):
    import queue

    class _EstimatedCostLLM:
        def chat(self, **kwargs):
            return (
                {"content": "ok"},
                {
                    "provider": "openai",
                    "resolved_model": "openai/gpt-5.5",
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "cached_tokens": 0,
                    "cache_write_tokens": 0,
                    "cost": 0.0,
                },
            )

    usage = {}
    event_queue = queue.Queue()
    with patch("ouroboros.loop_llm_call.estimate_cost", return_value=0.123456):
        _msg, _cost = call_llm_with_retry(
            _EstimatedCostLLM(),
            [{"role": "user", "content": "hi"}],
            "openai::gpt-5.5",
            None,
            "medium",
            1,
            tmp_path,
            "task-3",
            1,
            event_queue,
            usage,
            "task",
            False,
        )

    assert usage["cost"] == 0.123456
    events = [event_queue.get_nowait() for _ in range(event_queue.qsize())]
    usage_event = next(evt for evt in events if evt.get("type") == "llm_usage")
    assert usage_event["cost_estimated"] is True
