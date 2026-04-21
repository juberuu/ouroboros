"""History/cost endpoints extracted from server.py."""

from __future__ import annotations

import json
import logging
import pathlib
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)


def make_cost_breakdown_endpoint(data_dir: pathlib.Path):
    async def api_cost_breakdown(_request: Request) -> JSONResponse:
        """Aggregate llm_usage events from events.jsonl into cost breakdowns."""
        events_path = data_dir / "logs" / "events.jsonl"
        by_model: Dict[str, Dict[str, Any]] = {}
        by_api_key: Dict[str, Dict[str, Any]] = {}
        by_model_category: Dict[str, Dict[str, Any]] = {}
        by_task_category: Dict[str, Dict[str, Any]] = {}
        total_cost = 0.0
        total_calls = 0

        def _acc(d, key):
            if key not in d:
                d[key] = {"cost": 0.0, "calls": 0}
            return d[key]

        try:
            if events_path.exists():
                with events_path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            evt = json.loads(line)
                        except Exception:
                            continue
                        if evt.get("type") != "llm_usage":
                            continue
                        cost = float(evt.get("cost") or 0)
                        model = str(evt.get("model") or "unknown")
                        api_key_type = str(evt.get("api_key_type") or evt.get("provider") or "openrouter")
                        model_cat = str(evt.get("model_category") or "other")
                        task_cat = str(evt.get("category") or "task")

                        total_cost += cost
                        total_calls += 1

                        _acc(by_model, model)["cost"] += cost
                        _acc(by_model, model)["calls"] += 1
                        _acc(by_api_key, api_key_type)["cost"] += cost
                        _acc(by_api_key, api_key_type)["calls"] += 1
                        _acc(by_model_category, model_cat)["cost"] += cost
                        _acc(by_model_category, model_cat)["calls"] += 1
                        _acc(by_task_category, task_cat)["cost"] += cost
                        _acc(by_task_category, task_cat)["calls"] += 1
        except Exception:
            pass

        def _sorted(d):
            return dict(sorted(d.items(), key=lambda x: x[1]["cost"], reverse=True))

        return JSONResponse({
            "total_cost": round(total_cost, 4),
            "total_calls": total_calls,
            "by_model": _sorted(by_model),
            "by_api_key": _sorted(by_api_key),
            "by_model_category": _sorted(by_model_category),
            "by_task_category": _sorted(by_task_category),
        })

    return api_cost_breakdown


def make_chat_history_endpoint(data_dir: pathlib.Path):
    async def api_chat_history(request: Request) -> JSONResponse:
        """Return recent chat, system, and progress messages merged chronologically."""
        try:
            limit = max(0, min(int(request.query_params.get("limit", 1000)), 2000))
        except (ValueError, TypeError):
            limit = 1000

        combined: list = []

        chat_path = data_dir / "logs" / "chat.jsonl"
        if chat_path.exists():
            try:
                with chat_path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        # Skip A2A virtual chat_ids (negative; start at -1001)
                        # so A2A task traffic does not appear in human chat history.
                        try:
                            if int(entry.get("chat_id", 1)) < 0:
                                continue
                        except (TypeError, ValueError):
                            pass
                        direction = str(entry.get("direction", "")).lower()
                        role = {"in": "user", "out": "assistant", "system": "system"}.get(direction)
                        if role is None:
                            continue
                        rec = {
                            "text": str(entry.get("text", "")),
                            "role": role,
                            "ts": str(entry.get("ts", "")),
                            "is_progress": False,
                            "system_type": str(entry.get("type", "")),
                            "markdown": str(entry.get("format", "")).lower() == "markdown",
                            "source": str(entry.get("source", "")),
                            "sender_label": str(entry.get("sender_label", "")),
                            "sender_session_id": str(entry.get("sender_session_id", "")),
                            "client_message_id": str(entry.get("client_message_id", "")),
                            "task_id": str(entry.get("task_id", "")),
                            "telegram_chat_id": int(entry.get("telegram_chat_id") or 0),
                        }
                        # Pass task metadata for task_summary entries so the
                        # frontend can decide whether to show a live card.
                        if entry.get("type") == "task_summary":
                            if "tool_calls" in entry:
                                rec["tool_calls"] = int(entry["tool_calls"])
                            if "rounds" in entry:
                                rec["rounds"] = int(entry["rounds"])
                        combined.append(rec)
            except Exception as exc:
                log.warning("Failed to read chat history: %s", exc)

        progress_path = data_dir / "logs" / "progress.jsonl"
        if progress_path.exists():
            try:
                with progress_path.open(encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except (json.JSONDecodeError, ValueError):
                            continue
                        # Skip A2A virtual chat_ids (negative; start at -1001)
                        try:
                            if int(entry.get("chat_id", 1)) < 0:
                                continue
                        except (TypeError, ValueError):
                            pass
                        text = str(entry.get("content", entry.get("text", "")))
                        if not text:
                            continue
                        combined.append({
                            "text": text,
                            "role": "assistant",
                            "ts": str(entry.get("ts", "")),
                            "is_progress": True,
                            "markdown": str(entry.get("format", "")).lower() == "markdown",
                            "task_id": str(entry.get("task_id", "")),
                        })
            except Exception as exc:
                log.warning("Failed to read progress log: %s", exc)

        combined.sort(key=lambda m: m.get("ts", ""))
        messages = combined[-limit:] if len(combined) > limit else combined
        return JSONResponse({"messages": messages})

    return api_chat_history
