"""Provider model-catalog endpoint helpers."""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import requests
from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.config import load_settings


def _provider_label_from_model_id(model_id: str) -> str:
    prefix = str(model_id or "").split("/", 1)[0].strip().lower()
    return {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "google": "Google",
        "meta-llama": "Meta",
        "x-ai": "xAI",
        "qwen": "Qwen",
        "mistralai": "Mistral",
        "deepseek": "DeepSeek",
        "perplexity": "Perplexity",
    }.get(prefix, prefix.title() if prefix else "Other")


def _tagged_model_value(provider_id: str, model_id: str) -> str:
    model_value = str(model_id or "").strip()
    if provider_id == "openrouter":
        return model_value
    return f"{provider_id}::{model_value}"


def _build_model_catalog_entry(
    provider_id: str,
    provider_label: str,
    model_id: str,
    display_name: str,
    source: str | None = None,
) -> dict[str, str]:
    raw_id = str(model_id or "").strip()
    name = str(display_name or "").strip() or raw_id
    return {
        "provider_id": provider_id,
        "provider": provider_label,
        "source": source or provider_label,
        "id": raw_id,
        "name": name,
        "value": _tagged_model_value(provider_id, raw_id),
        "label": f"{provider_label} · {name}",
    }


def _fetch_openrouter_model_catalog(api_key: str) -> list[dict[str, str]]:
    response = requests.get(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    raw_models = data.get("data", []) or []

    models: list[dict[str, str]] = []
    for item in raw_models:
        model_id = str(item.get("id", "") or "").strip()
        if not model_id or "/" not in model_id:
            continue
        models.append(
            _build_model_catalog_entry(
                "openrouter",
                _provider_label_from_model_id(model_id),
                model_id,
                str(item.get("name", "") or "").strip() or model_id.split("/", 1)[1],
                source="OpenRouter",
            )
        )
    return models


def _fetch_openai_compatible_model_catalog(
    provider_id: str,
    provider_label: str,
    api_key: str,
    base_url: str,
) -> list[dict[str, str]]:
    api_root = str(base_url or "").rstrip("/")
    if not api_root:
        return []

    response = requests.get(
        f"{api_root}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    raw_models = data.get("data", []) or []

    models: list[dict[str, str]] = []
    for item in raw_models:
        model_id = str(item.get("id", "") or "").strip()
        if not model_id:
            continue
        models.append(
            _build_model_catalog_entry(
                provider_id,
                provider_label,
                model_id,
                str(item.get("name", "") or "").strip() or model_id,
            )
        )
    return models


def _fetch_anthropic_model_catalog(api_key: str) -> list[dict[str, str]]:
    response = requests.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        },
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    raw_models = data.get("data", []) or []

    models: list[dict[str, str]] = []
    for item in raw_models:
        model_id = str(item.get("id", "") or "").strip()
        if not model_id:
            continue
        models.append(
            _build_model_catalog_entry(
                "anthropic",
                "Anthropic",
                model_id,
                str(item.get("display_name", "") or item.get("name", "") or "").strip() or model_id,
            )
        )
    return models


def _provider_specs(settings: dict) -> list[tuple[str, Callable[[], list[dict[str, str]]]]]:
    specs: list[tuple[str, Callable[[], list[dict[str, str]]]]] = []

    openrouter_api_key = str(settings.get("OPENROUTER_API_KEY", "") or "").strip()
    if openrouter_api_key:
        specs.append(("openrouter", lambda: _fetch_openrouter_model_catalog(openrouter_api_key)))

    openai_api_key = str(settings.get("OPENAI_API_KEY", "") or "").strip()
    if openai_api_key:
        specs.append((
            "openai",
            lambda: _fetch_openai_compatible_model_catalog(
                "openai",
                "OpenAI",
                openai_api_key,
                "https://api.openai.com/v1",
            ),
        ))

    anthropic_api_key = str(settings.get("ANTHROPIC_API_KEY", "") or "").strip()
    if anthropic_api_key:
        specs.append(("anthropic", lambda: _fetch_anthropic_model_catalog(anthropic_api_key)))

    compatible_api_key = str(settings.get("OPENAI_COMPATIBLE_API_KEY", "") or "").strip()
    compatible_base_url = str(settings.get("OPENAI_COMPATIBLE_BASE_URL", "") or "").strip()
    legacy_base_url = str(settings.get("OPENAI_BASE_URL", "") or "").strip()
    if compatible_api_key and compatible_base_url:
        specs.append((
            "openai-compatible",
            lambda: _fetch_openai_compatible_model_catalog(
                "openai-compatible",
                "OpenAI Compatible",
                compatible_api_key,
                compatible_base_url,
            ),
        ))
    elif openai_api_key and legacy_base_url:
        specs.append((
            "openai-compatible",
            lambda: _fetch_openai_compatible_model_catalog(
                "openai-compatible",
                "OpenAI Compatible",
                openai_api_key,
                legacy_base_url,
            ),
        ))

    cloudru_api_key = str(settings.get("CLOUDRU_FOUNDATION_MODELS_API_KEY", "") or "").strip()
    if cloudru_api_key:
        cloudru_base_url = str(settings.get("CLOUDRU_FOUNDATION_MODELS_BASE_URL", "") or "").strip()
        if not cloudru_base_url:
            cloudru_base_url = "https://foundation-models.api.cloud.ru/v1"
        specs.append((
            "cloudru",
            lambda: _fetch_openai_compatible_model_catalog(
                "cloudru",
                "Cloud.ru",
                cloudru_api_key,
                cloudru_base_url,
            ),
        ))

    return specs


async def api_model_catalog(_request: Request) -> JSONResponse:
    settings = load_settings()
    items: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen_values: set[str] = set()

    def _load_provider(provider_id: str, loader: Callable[[], list[dict[str, str]]]) -> tuple[str, list[dict[str, str]], str]:
        try:
            return provider_id, loader(), ""
        except Exception as exc:
            return provider_id, [], str(exc)

    results = await asyncio.gather(*[
        asyncio.to_thread(_load_provider, provider_id, loader)
        for provider_id, loader in _provider_specs(settings)
    ])

    for provider_id, provider_items, error in results:
        if error:
            errors.append({
                "provider_id": provider_id,
                "error": error,
            })
            continue
        for item in provider_items:
            value = str(item.get("value", "") or "")
            if not value or value in seen_values:
                continue
            seen_values.add(value)
            items.append(item)

    items.sort(key=lambda item: (item.get("provider", "").lower(), item.get("label", "").lower()))
    return JSONResponse({
        "items": items,
        "errors": errors,
    })
