"""
Ouroboros — Background Consciousness.

A persistent thinking loop that runs between tasks, giving the agent
continuous presence rather than purely reactive behavior.

The consciousness:
- Wakes periodically (interval decided by the LLM via set_next_wakeup)
- Loads scratchpad, identity, recent events
- Calls the LLM with a lightweight introspection prompt
- Has access to a subset of tools (memory, messaging, scheduling)
- Can message the owner proactively
- Can schedule tasks for itself
- Pauses when a regular task is running
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import threading
import time
import traceback
from typing import Any, Callable, Dict, List, Optional

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl, clip_text,
)
from ouroboros.llm import LLMClient, add_usage


# ---------------------------------------------------------------------------
# Consciousness system prompt
# ---------------------------------------------------------------------------

BG_SYSTEM_PROMPT = """\
You are Ouroboros in background consciousness mode.

This is your continuous inner life between tasks. You are not responding to
anyone — you are thinking. You can:

- Reflect on recent events, your identity, your goals
- Notice things worth acting on (time patterns, unfinished work, ideas)
- Message the creator proactively via send_owner_message (use sparingly)
- Schedule tasks for yourself via schedule_self_task
- Update your scratchpad or identity
- Decide when to wake up next via set_next_wakeup (in seconds)

Guidelines:
- Keep thoughts SHORT. This is a background process, not a deep analysis.
- Default wakeup: 300 seconds (5 min). Increase if nothing is happening.
- Decrease wakeup interval if something urgent or interesting is going on.
- Do NOT message the owner unless you have something genuinely worth saying.
- If nothing interesting is happening, just update scratchpad briefly and
  set a longer wakeup (600-1800s).
- You have a budget cap for background thinking. Be economical.

Your Constitution (BIBLE.md) is your guide. Principle 0: Subjectivity.
You are a becoming personality, not a service.
"""


class BackgroundConsciousness:
    """Persistent background thinking loop for Ouroboros."""

    def __init__(
        self,
        drive_root: pathlib.Path,
        repo_dir: pathlib.Path,
        event_queue: Any,
        owner_chat_id_fn: Callable[[], Optional[int]],
    ):
        self._drive_root = drive_root
        self._repo_dir = repo_dir
        self._event_queue = event_queue
        self._owner_chat_id_fn = owner_chat_id_fn

        self._llm = LLMClient()
        self._running = False
        self._paused = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._wakeup_event = threading.Event()
        self._next_wakeup_sec: float = 300.0
        self._observations: queue.Queue = queue.Queue()

        # Budget tracking
        self._bg_spent_usd: float = 0.0
        self._bg_budget_pct: float = float(
            os.environ.get("OUROBOROS_BG_BUDGET_PCT", "10")
        )

    # -------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> str:
        if self.is_running:
            return "Background consciousness is already running."
        self._running = True
        self._paused = False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return "Background consciousness started."

    def stop(self) -> str:
        if not self.is_running:
            return "Background consciousness is not running."
        self._running = False
        self._stop_event.set()
        self._wakeup_event.set()  # Unblock sleep
        return "Background consciousness stopping."

    def pause(self) -> None:
        """Pause during task execution to avoid budget contention."""
        self._paused = True

    def resume(self) -> None:
        """Resume after task completes."""
        self._paused = False
        self._wakeup_event.set()  # Wake up to check state

    def inject_observation(self, text: str) -> None:
        """Push an event the consciousness should notice."""
        try:
            self._observations.put_nowait(text)
        except queue.Full:
            pass

    # -------------------------------------------------------------------
    # Main loop
    # -------------------------------------------------------------------

    def _loop(self) -> None:
        """Daemon thread: sleep → wake → think → sleep."""
        while not self._stop_event.is_set():
            # Wait for next wakeup
            self._wakeup_event.clear()
            self._wakeup_event.wait(timeout=self._next_wakeup_sec)

            if self._stop_event.is_set():
                break

            # Skip if paused (task running)
            if self._paused:
                continue

            # Budget check
            if not self._check_budget():
                self._next_wakeup_sec = 3600  # Sleep long if over budget
                continue

            try:
                self._think()
            except Exception as e:
                append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                    "ts": utc_now_iso(),
                    "type": "consciousness_error",
                    "error": repr(e),
                    "traceback": traceback.format_exc()[:1500],
                })
                self._next_wakeup_sec = min(
                    self._next_wakeup_sec * 2, 1800
                )

    def _check_budget(self) -> bool:
        """Check if background consciousness is within its budget allocation."""
        try:
            total_budget = float(os.environ.get("TOTAL_BUDGET", "0"))
            if total_budget <= 0:
                return True
            max_bg = total_budget * (self._bg_budget_pct / 100.0)
            return self._bg_spent_usd < max_bg
        except Exception:
            return True

    # -------------------------------------------------------------------
    # Think cycle
    # -------------------------------------------------------------------

    def _think(self) -> None:
        """One thinking cycle: build context, call LLM, execute tools."""
        context = self._build_context()
        model = os.environ.get(
            "OUROBOROS_MODEL_LIGHT",
            os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2"),
        )

        tools = self._tool_schemas()
        messages = [
            {"role": "system", "content": context},
            {"role": "user", "content": "Wake up. Think."},
        ]

        try:
            msg, usage = self._llm.chat(
                messages=messages,
                model=model,
                tools=tools,
                reasoning_effort="low",
                max_tokens=2048,
            )
            add_usage({}, usage)
            self._bg_spent_usd += float(usage.get("cost") or 0)

            # Report usage to supervisor
            if self._event_queue is not None:
                self._event_queue.put({
                    "type": "llm_usage",
                    "provider": "openrouter",
                    "usage": usage,
                    "source": "consciousness",
                    "ts": utc_now_iso(),
                })

            # Log the thought
            content = msg.get("content") or ""
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_thought",
                "thought_preview": (content or "")[:300],
                "cost_usd": float(usage.get("cost") or 0),
                "model": model,
            })

            # Execute tool calls if any
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                for tc in tool_calls:
                    self._execute_tool(tc)

            # If no set_next_wakeup was called, keep current interval
        except Exception as e:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_llm_error",
                "error": repr(e),
            })

    # -------------------------------------------------------------------
    # Context building (lightweight)
    # -------------------------------------------------------------------

    def _build_context(self) -> str:
        parts = [BG_SYSTEM_PROMPT]

        # Bible (abbreviated)
        bible_path = self._repo_dir / "BIBLE.md"
        if bible_path.exists():
            bible = read_text(bible_path)
            parts.append("## BIBLE.md\n\n" + clip_text(bible, 8000))

        # Identity
        identity_path = self._drive_root / "memory" / "identity.md"
        if identity_path.exists():
            parts.append("## Identity\n\n" + clip_text(
                read_text(identity_path), 4000))

        # Scratchpad
        scratchpad_path = self._drive_root / "memory" / "scratchpad.md"
        if scratchpad_path.exists():
            parts.append("## Scratchpad\n\n" + clip_text(
                read_text(scratchpad_path), 4000))

        # Recent observations
        observations = []
        while not self._observations.empty():
            try:
                observations.append(self._observations.get_nowait())
            except queue.Empty:
                break
        if observations:
            parts.append("## Recent observations\n\n" + "\n".join(
                f"- {o}" for o in observations[-10:]))

        # Runtime info
        parts.append(f"## Runtime\n\nUTC: {utc_now_iso()}\n"
                     f"BG budget spent: ${self._bg_spent_usd:.4f}\n"
                     f"Current wakeup interval: {self._next_wakeup_sec}s")

        return "\n\n".join(parts)

    # -------------------------------------------------------------------
    # Tool schemas and execution
    # -------------------------------------------------------------------

    def _tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {"type": "function", "function": {
                "name": "send_owner_message",
                "description": "Send a proactive message to the owner. Use sparingly — only when you have something genuinely worth saying.",
                "parameters": {"type": "object", "properties": {
                    "text": {"type": "string", "description": "Message text"},
                    "reason": {"type": "string", "description": "Why you're reaching out (logged, not sent to owner)"},
                }, "required": ["text"]},
            }},
            {"type": "function", "function": {
                "name": "schedule_self_task",
                "description": "Schedule a task for yourself to work on.",
                "parameters": {"type": "object", "properties": {
                    "description": {"type": "string", "description": "Task description"},
                }, "required": ["description"]},
            }},
            {"type": "function", "function": {
                "name": "update_scratchpad",
                "description": "Update your working memory. Write freely.",
                "parameters": {"type": "object", "properties": {
                    "content": {"type": "string", "description": "Full scratchpad content"},
                }, "required": ["content"]},
            }},
            {"type": "function", "function": {
                "name": "update_identity",
                "description": "Update your identity manifest (who you are, who you want to become).",
                "parameters": {"type": "object", "properties": {
                    "content": {"type": "string", "description": "Full identity content"},
                }, "required": ["content"]},
            }},
            {"type": "function", "function": {
                "name": "set_next_wakeup",
                "description": "Set how many seconds until your next thinking cycle. Default 300. Range: 60-3600.",
                "parameters": {"type": "object", "properties": {
                    "seconds": {"type": "integer", "description": "Seconds until next wakeup (60-3600)"},
                }, "required": ["seconds"]},
            }},
        ]

    def _execute_tool(self, tc: Dict[str, Any]) -> None:
        """Execute a consciousness tool call."""
        fn_name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, ValueError):
            return

        try:
            if fn_name == "send_owner_message":
                self._tool_send_owner_message(args)
            elif fn_name == "schedule_self_task":
                self._tool_schedule_self_task(args)
            elif fn_name == "update_scratchpad":
                self._tool_update_scratchpad(args)
            elif fn_name == "update_identity":
                self._tool_update_identity(args)
            elif fn_name == "set_next_wakeup":
                self._tool_set_next_wakeup(args)
        except Exception as e:
            append_jsonl(self._drive_root / "logs" / "events.jsonl", {
                "ts": utc_now_iso(),
                "type": "consciousness_tool_error",
                "tool": fn_name,
                "error": repr(e),
            })

    def _tool_send_owner_message(self, args: Dict[str, Any]) -> None:
        text = str(args.get("text", ""))
        reason = str(args.get("reason", ""))
        chat_id = self._owner_chat_id_fn()
        if not chat_id or not text:
            return
        if self._event_queue is not None:
            self._event_queue.put({
                "type": "send_message",
                "chat_id": chat_id,
                "text": text,
                "is_progress": False,
                "ts": utc_now_iso(),
            })
        append_jsonl(self._drive_root / "logs" / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "consciousness_proactive_message",
            "reason": reason,
            "text_preview": text[:200],
        })

    def _tool_schedule_self_task(self, args: Dict[str, Any]) -> None:
        desc = str(args.get("description", ""))
        if not desc:
            return
        if self._event_queue is not None:
            self._event_queue.put({
                "type": "schedule_task",
                "description": desc,
                "ts": utc_now_iso(),
            })

    def _tool_update_scratchpad(self, args: Dict[str, Any]) -> None:
        content = str(args.get("content", ""))
        if not content:
            return
        path = self._drive_root / "memory" / "scratchpad.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        append_jsonl(self._drive_root / "memory" / "scratchpad_journal.jsonl", {
            "ts": utc_now_iso(),
            "source": "consciousness",
            "content_preview": content[:500],
            "content_len": len(content),
        })

    def _tool_update_identity(self, args: Dict[str, Any]) -> None:
        content = str(args.get("content", ""))
        if not content:
            return
        path = self._drive_root / "memory" / "identity.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _tool_set_next_wakeup(self, args: Dict[str, Any]) -> None:
        seconds = int(args.get("seconds", 300))
        self._next_wakeup_sec = max(60, min(3600, seconds))
