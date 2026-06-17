"""v6.37.0 regression guards (C1.1/C1.2): cross-FAMILY model switches must not
replay provider-private reasoning/thinking blocks to a different upstream family.

The forensic failure: GLM (z-ai) reasoning + Anthropic-shaped ``thinking`` content
blocks were replayed verbatim into the ``anthropic/claude-sonnet-4.6`` fallback,
which 400'd with "Invalid `signature` in `thinking` block" — and the old recovery
keyed on an error-STRING allowlist that did not contain that phrase, so it never
fired. These guards lock in the structural fix."""


def _thinking_history():
    return [
        {
            "role": "assistant",
            "reasoning_details": [{"type": "reasoning.text", "text": "glm thoughts"}],
            "response_id": "resp_123",
            "content": [
                {"type": "thinking", "thinking": "secret chain", "signature": "glm-sig-xyz"},
                {"type": "text", "text": "the answer"},
            ],
        },
        {"role": "user", "content": "hi"},
    ]


def test_model_family_boundary():
    from ouroboros.llm import LLMClient
    assert LLMClient._model_family("z-ai/glm-5.2") == "z-ai"
    assert LLMClient._model_family("anthropic/claude-sonnet-4.6") == "anthropic"
    assert LLMClient._model_family("openai/gpt-5.5") == "openai"
    # bare/local id -> itself (still a usable boundary)
    assert LLMClient._model_family("local-model") == "local-model"


def test_has_replayed_reasoning_metadata_detects_all_shapes():
    from ouroboros.llm import LLMClient
    assert LLMClient._has_replayed_reasoning_metadata(_thinking_history()) is True
    assert LLMClient._has_replayed_reasoning_metadata(
        [{"role": "assistant", "reasoning_details": [{"x": 1}], "content": "p"}]
    ) is True
    assert LLMClient._has_replayed_reasoning_metadata(
        [{"role": "assistant", "content": [{"type": "thinking", "signature": "s"}]}]
    ) is True
    # clean transcript -> no replayed reasoning
    assert LLMClient._has_replayed_reasoning_metadata(
        [{"role": "assistant", "content": "plain"}, {"role": "user", "content": "x"}]
    ) is False


def test_strip_removes_thinking_blocks_and_signatures_keeps_text():
    from ouroboros.llm import LLMClient
    out = LLMClient._strip_openrouter_roundtrip_metadata(_thinking_history())
    assert LLMClient._has_replayed_reasoning_metadata(out) is False
    asst = out[0]
    assert "reasoning_details" not in asst and "response_id" not in asst
    # the thinking block is gone; the real text answer survives
    assert asst["content"] == [{"type": "text", "text": "the answer"}]
    # canonical input is NOT mutated (deep copy)
    assert any(
        isinstance(b, dict) and b.get("type") == "thinking"
        for b in _thinking_history()[0]["content"]
    )


def test_is_http_status_structural():
    from ouroboros.llm import LLMClient

    class _BadReq(Exception):
        status_code = 400

    assert LLMClient._is_http_status(_BadReq("boom"), 400) is True
    assert LLMClient._is_http_status(_BadReq("boom"), 429) is False
    # fallback to OpenAI-SDK message shape when no status_code attribute
    assert LLMClient._is_http_status(Exception("Error code: 400 - bad"), 400) is True
    assert LLMClient._is_http_status(Exception("nope"), 400) is False


def test_signature_retry_fires_on_400_with_thinking_block_no_string_marker():
    """The exact forensic failure: a 400 whose message ('Invalid signature in
    thinking block') matches NONE of the old markers must STILL trigger the
    strip-and-retry, because the trigger is now structural (400 + request carried
    replayed reasoning)."""
    from ouroboros.llm import LLMClient

    class _BadReq(Exception):
        status_code = 400

    inst = LLMClient.__new__(LLMClient)
    target = {"supports_openrouter_extensions": True}
    kwargs = {
        "messages": _thinking_history(),
        "extra_body": {"provider": {"allow_fallbacks": False}},
    }
    exc = _BadReq("messages.1.content.0: Invalid `signature` in `thinking` block")
    out = inst._openrouter_signature_retry_kwargs(target, kwargs, exc)
    assert out is not None
    assert LLMClient._has_replayed_reasoning_metadata(out["messages"]) is False

    # a genuine 400 WITHOUT replayed reasoning is left alone (re-raised upstream)
    clean = {"messages": [{"role": "user", "content": "x"}]}
    assert inst._openrouter_signature_retry_kwargs(target, clean, exc) is None
    # a non-400 error never triggers the reasoning strip
    assert inst._openrouter_signature_retry_kwargs(target, kwargs, Exception("Error code: 429")) is None


def test_sanitize_on_model_switch_strips_cross_family_preserves_same_family():
    from ouroboros.llm import LLMClient
    hist = _thinking_history()
    # cross-family (z-ai -> anthropic): sanitized copy, reasoning gone
    cross = LLMClient.sanitize_reasoning_on_model_switch(hist, "z-ai/glm-5.2", "anthropic/claude-sonnet-4.6")
    assert cross is not hist
    assert LLMClient._has_replayed_reasoning_metadata(cross) is False
    # same family (anthropic -> anthropic): identity, continuity preserved
    same = LLMClient.sanitize_reasoning_on_model_switch(hist, "anthropic/claude-opus-4.8", "anthropic/claude-sonnet-4.6")
    assert same is hist
