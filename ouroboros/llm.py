"""
Уроборос — LLM-клиент.

Единственный модуль, который общается с LLM API (OpenRouter).
Контракт: chat(), model_profile(), select_task_profile(), add_usage().
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Tuple


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


def add_usage(total: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Accumulate usage from one LLM call into a running total."""
    for k in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "cache_write_tokens"):
        total[k] = int(total.get(k) or 0) + int(usage.get(k) or 0)
    if usage.get("cost"):
        total["cost"] = float(total.get("cost") or 0) + float(usage["cost"])


class LLMClient:
    """Обёртка над OpenRouter API. Все LLM-вызовы идут через этот класс."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://openrouter.ai/api/v1",
    ):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self._base_url = base_url
        self._client = None

    def _get_client(self):
        if self._client is None:
            from openai import OpenAI
            self._client = OpenAI(
                base_url=self._base_url,
                api_key=self._api_key,
                default_headers={
                    "HTTP-Referer": "https://colab.research.google.com/",
                    "X-Title": "Ouroboros",
                },
            )
        return self._client

    def _fetch_generation_cost(self, generation_id: str) -> Optional[float]:
        """Fetch cost from OpenRouter Generation API as fallback."""
        try:
            import requests
            url = f"{self._base_url.rstrip('/')}/generation?id={generation_id}"
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
            # Generation might not be ready yet — retry once after short delay
            time.sleep(0.5)
            resp = requests.get(url, headers={"Authorization": f"Bearer {self._api_key}"}, timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                cost = data.get("total_cost") or data.get("usage", {}).get("cost")
                if cost is not None:
                    return float(cost)
        except Exception:
            pass
        return None

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Один вызов LLM. Возвращает: (response_message_dict, usage_dict с cost)."""
        client = self._get_client()
        effort = normalize_reasoning_effort(reasoning_effort)

        extra_body: Dict[str, Any] = {
            "reasoning": {"effort": effort, "exclude": True},
        }

        # Pin Anthropic models to Anthropic provider for prompt caching
        if model.startswith("anthropic/"):
            extra_body["provider"] = {
                "order": ["Anthropic"],
                "allow_fallbacks": False,
                "require_parameters": True,
            }

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": extra_body,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        msg = resp_dict.get("choices", [{}])[0].get("message", {})

        # Extract cached_tokens from prompt_tokens_details if available
        if not usage.get("cached_tokens"):
            prompt_details = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details, dict) and prompt_details.get("cached_tokens"):
                usage["cached_tokens"] = int(prompt_details["cached_tokens"])

        # Extract cache_write_tokens from prompt_tokens_details if available
        # OpenRouter: "cache_write_tokens"
        # Native Anthropic: "cache_creation_tokens" or "cache_creation_input_tokens"
        if not usage.get("cache_write_tokens"):
            prompt_details_for_write = usage.get("prompt_tokens_details") or {}
            if isinstance(prompt_details_for_write, dict):
                cache_write = (prompt_details_for_write.get("cache_write_tokens")
                              or prompt_details_for_write.get("cache_creation_tokens")
                              or prompt_details_for_write.get("cache_creation_input_tokens"))
                if cache_write:
                    usage["cache_write_tokens"] = int(cache_write)

        # Ensure cost is present in usage (OpenRouter includes it, but fallback if missing)
        if not usage.get("cost"):
            gen_id = resp_dict.get("id") or ""
            if gen_id:
                cost = self._fetch_generation_cost(gen_id)
                if cost is not None:
                    usage["cost"] = cost

        return msg, usage

    def model_profile(self, profile: str) -> Dict[str, str]:
        """Возвращает {"model": ..., "effort": ...} для типа задачи.

        Профили читают env-переменные, но имеют разумные дефолты.
        Минимум env — в соответствии с Принципом 3 (Минимализм).

        Env vars:
        - OUROBOROS_MODEL: main model for deep_review (default: openai/gpt-5.2)
        - OUROBOROS_MODEL_CODE: model for code/evolution tasks (default: same as OUROBOROS_MODEL)
        - OUROBOROS_MODEL_LIGHT: model for simple user chat (default: same as OUROBOROS_MODEL)
        """
        main_model = os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2")
        code_model = os.environ.get("OUROBOROS_MODEL_CODE", main_model)
        light_model = os.environ.get("OUROBOROS_MODEL_LIGHT", main_model)

        profiles: Dict[str, Dict[str, str]] = {
            "default_task": {"model": light_model, "effort": "medium"},
            "code_task": {"model": code_model, "effort": "high"},
            "evolution_task": {"model": code_model, "effort": "high"},
            "deep_review": {"model": main_model, "effort": "xhigh"},
        }
        return dict(profiles.get(profile, profiles["default_task"]))

    def select_task_profile(self, task_type: str) -> str:
        """Выбирает профиль по типу задачи. Без keyword routing (LLM-first)."""
        tt = str(task_type or "").strip().lower()
        if tt == "review":
            return "deep_review"
        if tt == "evolution":
            return "evolution_task"
        return "default_task"
