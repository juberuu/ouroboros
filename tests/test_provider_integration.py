"""
Provider integration tests — real API calls to verify each LLM provider works.

These tests are marked with @pytest.mark.integration and excluded from the
default pytest run via pyproject.toml addopts. They run only on:
  - main / ouroboros / ouroboros-stable push (CI Tier 2.5)
  - workflow_dispatch (manual)
  - tag push (v*)

Each test is individually skipped when its API key is absent, so the job
stays green even if only a subset of keys is configured.

`LLMClient.chat()` returns a `(msg_dict, usage_dict)` tuple since v4.44.0.
The shared assertion below also handles the legacy flat-dict shape so tests
do not need to track the underlying client refactor.
"""

import os
import pytest

# Skip the entire module during routine pytest runs that use addopts -m "not integration".
# The mark also works as a per-test filter.
integration = pytest.mark.integration


def _get_llm_client():
    """Lazy import to avoid breaking collection when ouroboros is not installed."""
    from ouroboros.llm import LLMClient
    return LLMClient()


def _assert_basic_response(result, expected_provider=None):
    """Shared assertion: non-empty reply, token usage present.

    In v4.44.0+ LLMClient.chat() returns a (msg_dict, usage_dict) tuple,
    not a flat dict. Handle both shapes for forward compatibility.
    """
    if isinstance(result, tuple):
        msg, usage = result
    else:
        msg, usage = result, result.get("usage", {}) if isinstance(result, dict) else {}

    text = ""
    if isinstance(msg, dict):
        text = msg.get("content", "") or ""
        # Anthropic returns content as a list of typed blocks instead of a string.
        if isinstance(text, list):
            text = " ".join(
                b.get("text", "") for b in text if isinstance(b, dict)
            )
    assert text, f"Empty response from LLM: {result}"

    assert isinstance(usage, dict), f"Usage is not a dict: {type(usage)}"
    assert usage.get("prompt_tokens", 0) > 0, f"No prompt_tokens in usage: {usage}"
    assert usage.get("completion_tokens", 0) > 0, f"No completion_tokens in usage: {usage}"

    if expected_provider:
        resolved = usage.get("provider", "") or usage.get("resolved_model", "") or ""
        assert expected_provider.lower() in resolved.lower(), (
            f"Expected provider '{expected_provider}' in resolved model, "
            f"got '{resolved}'"
        )


@integration
@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)
def test_openrouter_basic_chat():
    """Verify OpenRouter responds to a minimal chat request."""
    client = _get_llm_client()
    result = client.chat(
        messages=[{"role": "user", "content": "Respond with exactly: OK"}],
        model="anthropic/claude-sonnet-4.6",
    )
    _assert_basic_response(result, expected_provider="openrouter")


@integration
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
def test_openai_direct_basic_chat():
    """Verify official OpenAI direct routing works."""
    client = _get_llm_client()
    result = client.chat(
        messages=[{"role": "user", "content": "Respond with exactly: OK"}],
        model="openai::gpt-4o-mini",
    )
    _assert_basic_response(result, expected_provider="openai")


@integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_anthropic_direct_basic_chat():
    """Verify direct Anthropic routing works."""
    client = _get_llm_client()
    result = client.chat(
        messages=[{"role": "user", "content": "Respond with exactly: OK"}],
        model="anthropic::claude-sonnet-4-6",
    )
    _assert_basic_response(result, expected_provider="anthropic")


# Isolation tests: clear competing provider keys so LLMClient can only route
# through the single provider under test.

_COMPETING_KEYS = [
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "ANTHROPIC_API_KEY",
]


@integration
@pytest.mark.skipif(
    not os.environ.get("OPENROUTER_API_KEY"),
    reason="OPENROUTER_API_KEY not set",
)
def test_openrouter_isolation(monkeypatch):
    """OpenRouter works when it is the only configured provider."""
    for key in _COMPETING_KEYS:
        if key != "OPENROUTER_API_KEY":
            monkeypatch.delenv(key, raising=False)
    client = _get_llm_client()
    result = client.chat(
        messages=[{"role": "user", "content": "Say hello"}],
        model="anthropic/claude-sonnet-4.6",
    )
    _assert_basic_response(result)


@integration
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set",
)
def test_openai_direct_isolation(monkeypatch):
    """OpenAI direct works when it is the only configured provider."""
    for key in _COMPETING_KEYS:
        if key != "OPENAI_API_KEY":
            monkeypatch.delenv(key, raising=False)
    client = _get_llm_client()
    result = client.chat(
        messages=[{"role": "user", "content": "Say hello"}],
        model="openai::gpt-4o-mini",
    )
    _assert_basic_response(result)


@integration
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY not set",
)
def test_anthropic_direct_isolation(monkeypatch):
    """Anthropic direct works when it is the only configured provider."""
    for key in _COMPETING_KEYS:
        if key != "ANTHROPIC_API_KEY":
            monkeypatch.delenv(key, raising=False)
    client = _get_llm_client()
    result = client.chat(
        messages=[{"role": "user", "content": "Say hello"}],
        model="anthropic::claude-sonnet-4-6",
    )
    _assert_basic_response(result)
