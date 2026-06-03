"""Context-window overflow → owner hint to switch to low context mode."""

from ouroboros.llm import LocalContextTooLargeError
from ouroboros.loop import _provider_recovery_hint
from ouroboros.loop_llm_call import _is_context_overflow_error


def test_is_context_overflow_error_detects_local_and_remote():
    assert _is_context_overflow_error(LocalContextTooLargeError("too big"), "")
    assert _is_context_overflow_error(Exception(), "Error 400: maximum context length exceeded")
    assert _is_context_overflow_error(Exception(), "context_length_exceeded for this model")
    # Unrelated provider errors must NOT trigger the low-mode hint.
    assert not _is_context_overflow_error(Exception(), "429 rate limit exceeded")
    assert not _is_context_overflow_error(Exception(), "401 unauthorized")


def test_recovery_hint_suggests_low_when_flagged():
    hint = _provider_recovery_hint({"context_overflow_suggest_low": True})
    assert "low context mode" in hint.lower()


def test_recovery_hint_unchanged_without_flag():
    plain = _provider_recovery_hint({"_last_llm_error": "429 rate limit"})
    assert "low context mode" not in plain.lower()
