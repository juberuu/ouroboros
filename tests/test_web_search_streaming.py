"""Tests for web_search streaming implementation."""

import json
import sys
import types
from unittest.mock import MagicMock, patch

import pytest


def _make_event(etype: str, **kwargs):
    """Create a mock streaming event with .type and arbitrary attrs."""
    ev = types.SimpleNamespace(type=etype, **kwargs)
    return ev


def _make_completed_event(input_tokens=100, output_tokens=50):
    """Create a response.completed event with nested usage."""
    usage_obj = MagicMock()
    usage_obj.model_dump.return_value = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    resp_obj = MagicMock()
    resp_obj.usage = usage_obj
    return _make_event("response.completed", response=resp_obj)


class _FakeStream:
    """Iterable that yields pre-built events."""

    def __init__(self, events):
        self._events = events

    def __iter__(self):
        return iter(self._events)


@pytest.fixture
def ctx():
    """Minimal ToolContext-like object."""
    c = MagicMock()
    c.pending_events = []
    c.emit_progress_fn = MagicMock()
    return c


@pytest.fixture
def patch_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


@pytest.fixture
def mock_openai():
    """Inject a fake openai module so the lazy import inside _web_search works."""
    mock_client = MagicMock()
    mock_module = MagicMock()
    mock_module.OpenAI.return_value = mock_client
    with patch.dict(sys.modules, {"openai": mock_module}):
        yield mock_client


def test_streaming_emits_progress_on_search(ctx, patch_env, mock_openai):
    """Progress callback fires when web search starts."""
    events = [
        _make_event("response.web_search_call.in_progress", item_id="ws1", output_index=0, sequence_number=1),
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=2),
        _make_event("response.output_text.delta", delta="Hello ", content_index=0, item_id="m1", output_index=1, sequence_number=3, logprobs=[]),
        _make_event("response.output_text.delta", delta="world", content_index=0, item_id="m1", output_index=1, sequence_number=4, logprobs=[]),
        _make_completed_event(200, 80),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    from ouroboros.tools.search import _web_search
    result = _web_search(ctx, "test query")

    # Progress was emitted exactly once
    ctx.emit_progress_fn.assert_called_once()
    call_text = ctx.emit_progress_fn.call_args[0][0]
    assert "test query" in call_text

    # Text assembled correctly
    data = json.loads(result)
    assert data["answer"] == "Hello world"

    # stream=True was passed
    mock_openai.responses.create.assert_called_once()
    call_kwargs = mock_openai.responses.create.call_args[1]
    assert call_kwargs["stream"] is True


def test_streaming_cost_tracking(ctx, patch_env, mock_openai):
    """Usage from response.completed flows into pending_events."""
    events = [
        _make_event("response.output_text.delta", delta="Answer", content_index=0, item_id="m1", output_index=0, sequence_number=1, logprobs=[]),
        _make_completed_event(500, 100),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    from ouroboros.tools.search import _web_search
    result = _web_search(ctx, "cost test")

    assert len(ctx.pending_events) == 1
    ev = ctx.pending_events[0]
    assert ev["type"] == "llm_usage"
    assert ev["prompt_tokens"] == 500
    assert ev["completion_tokens"] == 100
    assert ev["model_category"] == "websearch"
    assert ev["cost"] > 0


def test_streaming_no_progress_without_search_events(ctx, patch_env, mock_openai):
    """If no web_search_call events arrive, progress is not emitted."""
    events = [
        _make_event("response.output_text.delta", delta="Direct answer", content_index=0, item_id="m1", output_index=0, sequence_number=1, logprobs=[]),
        _make_completed_event(50, 20),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    from ouroboros.tools.search import _web_search
    result = _web_search(ctx, "simple query")

    ctx.emit_progress_fn.assert_not_called()
    data = json.loads(result)
    assert data["answer"] == "Direct answer"


def test_streaming_empty_text_fallback(ctx, patch_env, mock_openai):
    """If no text deltas arrive, returns '(no answer)'."""
    events = [
        _make_completed_event(10, 0),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    from ouroboros.tools.search import _web_search
    result = _web_search(ctx, "empty query")

    data = json.loads(result)
    assert data["answer"] == "(no answer)"


def test_streaming_progress_fires_only_once(ctx, patch_env, mock_openai):
    """Multiple web_search_call events only trigger one progress call."""
    events = [
        _make_event("response.web_search_call.in_progress", item_id="ws1", output_index=0, sequence_number=1),
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=2),
        _make_event("response.web_search_call.searching", item_id="ws1", output_index=0, sequence_number=3),
        _make_event("response.output_text.delta", delta="Result", content_index=0, item_id="m1", output_index=1, sequence_number=4, logprobs=[]),
        _make_completed_event(100, 50),
    ]
    mock_openai.responses.create.return_value = _FakeStream(events)

    from ouroboros.tools.search import _web_search
    _web_search(ctx, "multi-search query")

    # Only one progress call despite multiple search events
    assert ctx.emit_progress_fn.call_count == 1
