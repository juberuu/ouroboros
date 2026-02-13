"""
Уроборос — LLM-клиент.

Единственный модуль, который общается с LLM API (OpenRouter).
Контракт: chat(), model_profile(), select_task_profile().
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple


def normalize_reasoning_effort(value: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    v = str(value or "").strip().lower()
    return v if v in allowed else default


def reasoning_rank(value: str) -> int:
    order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
    return int(order.get(str(value or "").strip().lower(), 3))


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

    def chat(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
        reasoning_effort: str = "medium",
        max_tokens: int = 16384,
        tool_choice: str = "auto",
    ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Один вызов LLM. Возвращает: (response_message_dict, usage_dict)."""
        client = self._get_client()
        effort = normalize_reasoning_effort(reasoning_effort)

        kwargs: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "extra_body": {"reasoning": {"effort": effort, "exclude": True}},
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        resp = client.chat.completions.create(**kwargs)
        resp_dict = resp.model_dump()
        usage = resp_dict.get("usage") or {}
        msg = resp_dict.get("choices", [{}])[0].get("message", {})
        return msg, usage

    def model_profile(self, profile: str) -> Dict[str, str]:
        """Возвращает {"model": ..., "effort": ...} для типа задачи.

        Профили читают env-переменные, но имеют разумные дефолты.
        Минимум env — в соответствии с Принципом 3 (Минимализм).
        """
        main_model = os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2")
        code_model = os.environ.get("OUROBOROS_MODEL_CODE", main_model)

        profiles: Dict[str, Dict[str, str]] = {
            "default_task": {"model": main_model, "effort": "medium"},
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
