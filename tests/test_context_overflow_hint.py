"""Context-window overflow → owner hint to switch to low context mode."""

from ouroboros.llm import LocalContextTooLargeError
from ouroboros.loop import _provider_recovery_hint
from ouroboros.loop_llm_call import _LlmErrorContext, _is_context_overflow_error, _record_llm_call_error


def test_is_context_overflow_error_detects_local_and_remote():
    assert _is_context_overflow_error(LocalContextTooLargeError("too big"), "")
    assert _is_context_overflow_error(Exception(), "Error 400: maximum context length exceeded")
    assert _is_context_overflow_error(Exception(), "context_length_exceeded for this model")
    # Unrelated provider errors must NOT trigger the low-mode hint.
    assert not _is_context_overflow_error(Exception(), "429 rate limit exceeded")
    assert not _is_context_overflow_error(Exception(), "Rate limit reached: too many tokens per minute")
    assert not _is_context_overflow_error(Exception(), "401 unauthorized")


def test_recovery_hint_suggests_low_when_flagged():
    hint = _provider_recovery_hint({"context_overflow_suggest_low": True})
    assert "low context mode" in hint.lower()


def test_recovery_hint_unchanged_without_flag():
    plain = _provider_recovery_hint({"_last_llm_error": "429 rate limit"})
    assert "low context mode" not in plain.lower()


def test_remote_context_overflow_is_not_logged_as_local(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.loop_llm_call.get_context_mode", lambda: "advanced")
    usage = {}
    ctx = _LlmErrorContext(
        task_id="task-ctx",
        task_type="task",
        execution_id="exec-1",
        round_id="round-1",
        llm_call_id="call-1",
        round_idx=1,
        attempt=0,
        model="provider/model",
        request_ref=None,
        drive_logs=tmp_path,
        event_queue=None,
        accumulated_usage=usage,
    )

    stop_retry = _record_llm_call_error(RuntimeError("maximum context length exceeded"), ctx)
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8")

    assert stop_retry is True
    assert '"type": "remote_context_overflow"' in lines
    assert '"type": "local_context_overflow"' not in lines
    assert usage["context_overflow_suggest_low"] is True
