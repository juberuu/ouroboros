import json
import sys
import types

import ouroboros.tools.search as search_module


def _make_openai_module(calls: dict):
    class _Usage:
        def model_dump(self):
            return {"input_tokens": 11, "output_tokens": 7}

    class _CompletedResponse:
        usage = _Usage()

    class _FakeStream:
        """Iterable that simulates streaming events."""
        def __iter__(self):
            yield types.SimpleNamespace(type="response.web_search_call.searching",
                                        item_id="ws1", output_index=0, sequence_number=1)
            yield types.SimpleNamespace(type="response.output_text.delta",
                                        delta="fresh answer", content_index=0,
                                        item_id="m1", output_index=1, sequence_number=2,
                                        logprobs=[])
            yield types.SimpleNamespace(type="response.completed",
                                        response=_CompletedResponse(), sequence_number=3)

    class _Responses:
        def create(self, **kwargs):
            calls["kwargs"] = kwargs
            return _FakeStream()

    class _Client:
        def __init__(self, api_key=None, base_url=None):
            calls["api_key"] = api_key
            calls["base_url"] = base_url
            self.responses = _Responses()

    return types.SimpleNamespace(OpenAI=_Client)


def test_web_search_requires_official_openai_without_legacy_base(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_COMPATIBLE_API_KEY", "compat-key")

    result = json.loads(search_module._web_search(types.SimpleNamespace(pending_events=[]), "latest news"))

    assert result == {
        "error": "web_search requires the official OpenAI Responses API. Set OPENAI_API_KEY and leave OPENAI_BASE_URL empty."
    }


def test_web_search_uses_official_openai_responses(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", raising=False)

    calls = {}
    monkeypatch.setitem(sys.modules, "openai", _make_openai_module(calls))
    ctx = types.SimpleNamespace(pending_events=[])

    result = json.loads(search_module._web_search(ctx, "latest news", model="gpt-5.2"))

    assert result == {"answer": "fresh answer"}
    assert calls["api_key"] == "openai-key"
    assert calls["base_url"] is None
    assert calls["kwargs"]["model"] == "gpt-5.2"
    assert calls["kwargs"]["stream"] is True
    assert calls["kwargs"]["tools"][0]["type"] == "web_search"
    assert ctx.pending_events[0]["provider"] == "openai"
    assert ctx.pending_events[0]["model"] == "gpt-5.2"
