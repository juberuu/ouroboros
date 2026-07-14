"""Route-aware best-effort pricing tests."""

from __future__ import annotations

import queue
from unittest.mock import MagicMock, patch

import pytest

from ouroboros.llm import fetch_cloudru_pricing, fetch_openrouter_pricing
from ouroboros.pricing import (
    PricingSchedule,
    emit_llm_usage_event,
    estimate_cost_optional,
    get_pricing,
    infer_api_key_type,
    infer_model_category,
)


@pytest.fixture(autouse=True)
def _reset_pricing_cache():
    import ouroboros.pricing as pricing

    pricing._cached_pricing.clear()
    pricing._pricing_fetched_at.clear()
    pricing._pricing_retry_after.clear()
    pricing._pricing_fetch_in_progress.clear()
    yield
    pricing._cached_pricing.clear()
    pricing._pricing_fetched_at.clear()
    pricing._pricing_retry_after.clear()
    pricing._pricing_fetch_in_progress.clear()


def test_unknown_direct_model_cost_is_none_and_does_not_query_openrouter():
    with patch("ouroboros.llm.fetch_openrouter_pricing") as fetch:
        assert estimate_cost_optional(
            "openai::future-model", 1_000, 500, provider="openai",
        ) is None
    fetch.assert_not_called()


def test_provider_is_inferred_without_openrouter_fallback():
    with patch("ouroboros.llm.fetch_openrouter_pricing") as fetch:
        assert estimate_cost_optional(
            "openai::future-model", 1_000, 500, provider=None,
        ) is None
    fetch.assert_not_called()


def test_live_catalog_prices_exact_new_openrouter_model():
    with patch(
        "ouroboros.llm.fetch_openrouter_pricing",
        return_value={"openai/gpt-new": (2.0, 0.2, None, 8.0)},
    ) as fetch:
        cost = estimate_cost_optional(
            "openai/gpt-new", 1_000, 500, provider="openrouter",
        )
    assert cost == 0.006
    fetch.assert_called_once_with(timeout_sec=5.0)


def test_similar_model_name_does_not_inherit_prefix_price():
    with patch(
        "ouroboros.llm.fetch_openrouter_pricing",
        return_value={"openai/gpt-new": (2.0, 0.2, None, 8.0)},
    ):
        assert estimate_cost_optional(
            "openai/gpt-new:beta", 1_000, 500, provider="openrouter",
        ) is None


def test_failed_catalog_fetch_has_short_process_local_cooldown():
    with patch("ouroboros.llm.fetch_openrouter_pricing", return_value={}) as fetch:
        assert get_pricing(provider="openrouter") == {}
        assert get_pricing(provider="openrouter") == {}
    fetch.assert_called_once_with(timeout_sec=5.0)


def test_missing_cache_prices_are_not_invented():
    with patch(
        "ouroboros.llm.fetch_openrouter_pricing",
        return_value={"provider/model": (1.0, None, None, 3.0)},
    ):
        assert estimate_cost_optional(
            "provider/model", 1_000, 100, provider="openrouter",
        ) == 0.0013
        assert estimate_cost_optional(
            "provider/model", 1_000, 100, cached_tokens=10,
            provider="openrouter", allow_live_fetch=False,
        ) is None
        assert estimate_cost_optional(
            "provider/model", 1_000, 100, cache_write_tokens=10,
            provider="openrouter", allow_live_fetch=False,
        ) is None


def test_exact_prompt_tier_is_applied_without_prefix_matching():
    row = PricingSchedule(
        (1.0, 0.1, None, 3.0),
        ((100_000, (2.0, 0.2, None, 5.0)),),
    )
    with patch("ouroboros.llm.fetch_openrouter_pricing", return_value={"x/model": row}):
        assert estimate_cost_optional(
            "x/model", 100_000, 1_000, provider="openrouter",
        ) == 0.205


def test_openrouter_catalog_accepts_arbitrary_model_family():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": [{
            "id": "mistralai/brand-new",
            "pricing": {"prompt": "0.000002", "completion": "0.000006"},
        }]
    }
    with patch("requests.get", return_value=response) as request:
        rows = fetch_openrouter_pricing(timeout_sec=5.0)
    assert rows["mistralai/brand-new"] == (2.0, None, None, 6.0)
    request.assert_called_once_with("https://openrouter.ai/api/v1/models", timeout=5.0)


def test_cloudru_requires_explicit_fx_rate(monkeypatch):
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "secret")
    monkeypatch.delenv("OUROBOROS_RUB_USD_RATE", raising=False)
    with patch("requests.get") as request:
        assert fetch_cloudru_pricing(timeout_sec=5.0) == {}
    request.assert_not_called()


def test_cloudru_catalog_uses_exact_model_and_explicit_fx(monkeypatch):
    monkeypatch.setenv("CLOUDRU_FOUNDATION_MODELS_API_KEY", "secret")
    monkeypatch.setenv("OUROBOROS_RUB_USD_RATE", "100")
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"data": [{
        "id": "vendor/new-model",
        "metadata": {
            "is_billable": True,
            "prompt_tokens_cost": 100,
            "generated_tokens_cost": 500,
            "cache_read_tokens_cost": None,
            "cache_write_tokens_cost": None,
        },
    }]}
    with patch("requests.get", return_value=response):
        rows = fetch_cloudru_pricing(timeout_sec=5.0)
    assert rows["cloudru/vendor/new-model"] == (1.0, None, None, 5.0)


@pytest.mark.parametrize("provider", ["openai", "openai-compatible", "gigachat", "anthropic"])
def test_routes_without_automatic_catalog_return_empty(provider):
    assert get_pricing(provider=provider) == {}


def test_nullable_usage_event_does_not_label_unknown_as_estimated():
    events = queue.Queue()
    emit_llm_usage_event(
        events,
        "task",
        "openai::future-model",
        {"prompt_tokens": 3, "completion_tokens": 2},
        None,
        provider="openai",
    )
    event = events.get_nowait()
    assert event["cost"] is None
    assert event["cost_estimated"] is False


def test_provider_reported_zero_cost_remains_known_zero():
    events = queue.Queue()
    emit_llm_usage_event(
        events, "task", "local/model", {"cost": 0}, 0.0, provider="local",
    )
    assert events.get_nowait()["cost"] == 0.0


def test_inference_helpers_keep_route_identity(monkeypatch):
    assert infer_api_key_type("openai::gpt-x") == "openai"
    assert infer_api_key_type("mistralai/model") == "openrouter"
    monkeypatch.setenv("OUROBOROS_MODEL", "mistralai/model")
    assert infer_model_category("mistralai/model") == "main"
