import asyncio
import json
import threading

import ouroboros.model_catalog_api as model_catalog_api


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_model_catalog_tags_provider_values(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {
        "OPENROUTER_API_KEY": "or-key",
        "OPENAI_API_KEY": "openai-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "OPENAI_COMPATIBLE_API_KEY": "compat-key",
        "OPENAI_COMPATIBLE_BASE_URL": "https://compat.example/v1",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY": "cloudru-key",
    })

    def fake_get(url, headers=None, timeout=0):
        if "openrouter.ai" in url:
            return _Response({"data": [{"id": "anthropic/claude-opus", "name": "Claude Opus"}]})
        if "api.openai.com" in url:
            return _Response({"data": [{"id": "gpt-4.1"}]})
        if "api.anthropic.com" in url:
            return _Response({"data": [{"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"}]})
        if "compat.example" in url:
            return _Response({"data": [{"id": "compatible-pro"}]})
        if "cloud.ru" in url:
            return _Response({"data": [{"id": "cloudru-pro"}]})
        raise AssertionError(url)

    monkeypatch.setattr(model_catalog_api.requests, "get", fake_get)

    response = asyncio.run(model_catalog_api.api_model_catalog(None))
    payload = json.loads(response.body.decode("utf-8"))
    values = {item["value"] for item in payload["items"]}

    assert "anthropic/claude-opus" in values
    assert "openai::gpt-4.1" in values
    assert "anthropic::claude-sonnet-4-6" in values
    assert "openai-compatible::compatible-pro" in values
    assert "cloudru::cloudru-pro" in values
    assert payload["errors"] == []


def test_model_catalog_returns_errors_nonfatally(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {
        "OPENROUTER_API_KEY": "or-key",
        "ANTHROPIC_API_KEY": "anthropic-key",
        "OPENAI_COMPATIBLE_API_KEY": "compat-key",
        "OPENAI_COMPATIBLE_BASE_URL": "https://compat.example/v1",
    })

    def fake_get(url, headers=None, timeout=0):
        if "openrouter.ai" in url:
            return _Response({"data": [{"id": "anthropic/claude-opus", "name": "Claude Opus"}]})
        if "api.anthropic.com" in url:
            raise RuntimeError("anthropic failed")
        raise RuntimeError("catalog failed")

    monkeypatch.setattr(model_catalog_api.requests, "get", fake_get)

    response = asyncio.run(model_catalog_api.api_model_catalog(None))
    payload = json.loads(response.body.decode("utf-8"))

    assert any(item["value"] == "anthropic/claude-opus" for item in payload["items"])
    assert payload["errors"] == [
        {"provider_id": "anthropic", "error": "anthropic failed"},
        {"provider_id": "openai-compatible", "error": "catalog failed"},
    ]


def test_model_catalog_runs_provider_loaders_off_event_loop_thread(monkeypatch):
    monkeypatch.setattr(model_catalog_api, "load_settings", lambda: {})
    thread_ids = []
    main_thread_id = threading.get_ident()

    def _loader():
        thread_ids.append(threading.get_ident())
        return [{"value": "provider::model", "label": "Provider Model"}]

    monkeypatch.setattr(model_catalog_api, "_provider_specs", lambda settings: [("provider", _loader)])

    response = asyncio.run(model_catalog_api.api_model_catalog(None))
    payload = json.loads(response.body.decode("utf-8"))

    assert payload["items"] == [{"value": "provider::model", "label": "Provider Model"}]
    assert payload["errors"] == []
    assert thread_ids
    assert all(thread_id != main_thread_id for thread_id in thread_ids)
