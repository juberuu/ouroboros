from __future__ import annotations

import asyncio
from types import SimpleNamespace


def test_query_model_timeout_becomes_error_actor(monkeypatch):
    from ouroboros.tools.review import _query_model

    class HangingClient:
        async def chat_async(self, **_kwargs):
            await asyncio.sleep(1)
            return {"content": "late"}, {}

    monkeypatch.setenv("OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC", "0.01")

    model, result, headers = asyncio.run(
        _query_model(HangingClient(), "fake/reviewer", [], asyncio.Semaphore(1))
    )

    assert model == "fake/reviewer"
    assert headers is None
    assert result["error"] == "Error: Timeout after 0.01s"
    assert result["prompt_ref"]["manifest_ref"]["path"]
    assert result["response_ref"]["manifest_ref"]["path"]


def test_query_model_uses_configured_review_effort(monkeypatch):
    from ouroboros.tools.review import _query_model
    import ouroboros.review_substrate as substrate

    captured_efforts = []

    def fake_run_review_request(_request, slots, **_kwargs):
        captured_efforts.append(slots[0].effort)
        return SimpleNamespace(
            actors=[{
                "status": "ok",
                "raw_text": "[]",
                "usage": {},
                "prompt_ref": {},
                "response_ref": {},
            }]
        )

    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "high")
    monkeypatch.setattr(substrate, "run_review_request", fake_run_review_request)

    model, result, headers = asyncio.run(
        _query_model(object(), "fake/reviewer", [], asyncio.Semaphore(1))
    )

    assert model == "fake/reviewer"
    assert headers is None
    assert result["choices"][0]["message"]["content"] == "[]"
    assert captured_efforts == ["high"]
