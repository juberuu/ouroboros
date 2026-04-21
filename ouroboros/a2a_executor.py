"""
A2A — Agent Executor.

Bridges incoming A2A messages to the Ouroboros supervisor via
handle_chat_direct(). Collects responses through LocalChatBridge
subscription mechanism.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from typing import Any, Optional

try:
    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.events import EventQueue
    from a2a.types import (
        Artifact,
        Message,
        Part,
        TaskState,
        TaskStatus,
        TaskStatusUpdateEvent,
        TaskArtifactUpdateEvent,
        TextPart,
        Role,
    )
    _A2A_AVAILABLE = True
except ImportError:
    _A2A_AVAILABLE = False
    AgentExecutor = object  # type: ignore[assignment,misc]
    RequestContext = EventQueue = None  # type: ignore[assignment]
    Artifact = Message = Part = TaskState = TaskStatus = None  # type: ignore[assignment]
    TaskStatusUpdateEvent = TaskArtifactUpdateEvent = None  # type: ignore[assignment]
    TextPart = Role = None  # type: ignore[assignment]

log = logging.getLogger("a2a-server")


def _now() -> str:
    """ISO 8601 UTC timestamp string for TaskStatus."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

# Virtual chat_id range for A2A (negative, avoids collision with web=1, telegram=positive)
_A2A_CHAT_ID_BASE = -1000
_a2a_seq = 0
_a2a_seq_lock = threading.Lock()


def _next_a2a_chat_id() -> int:
    global _a2a_seq
    with _a2a_seq_lock:
        _a2a_seq += 1
        return _A2A_CHAT_ID_BASE - _a2a_seq


class OuroborosA2AExecutor(AgentExecutor):
    """Bridges A2A protocol to Ouroboros supervisor."""

    def __init__(self, max_concurrent: int = 3):
        self._semaphore = threading.Semaphore(max_concurrent)
        # Track active task_ids for cancel() observability.
        # Note: cancellation is advisory — running handle_chat_direct() calls
        # complete naturally; this set is used for status queries only.
        self._active_tasks: set[str] = set()
        self._active_lock = threading.Lock()

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id
        context_id = context.context_id

        # Extract text from incoming message
        text = self._extract_text(context.message)
        if not text:
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.failed,
                        timestamp=_now(),
                        message=Message(
                            messageId=uuid.uuid4().hex,
                            role=Role.agent,
                            parts=[Part(root=TextPart(text="Empty message received"))],
                        ),
                    ),
                )
            )
            return

        # Check concurrency
        if not self._semaphore.acquire(blocking=False):
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.rejected,
                        timestamp=_now(),
                        message=Message(
                            messageId=uuid.uuid4().hex,
                            role=Role.agent,
                            parts=[Part(root=TextPart(text="Too many concurrent tasks"))],
                        ),
                    ),
                )
            )
            return

        with self._active_lock:
            self._active_tasks.add(task_id)
        try:
            # Signal working
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=False,
                    status=TaskStatus(
                        state=TaskState.working,
                        timestamp=_now(),
                    ),
                )
            )

            # Dispatch to supervisor and wait for response
            response_text = await self._dispatch_to_supervisor(text, event_queue, task_id, context_id)

            # Check whether cancel() was called while dispatch was running.
            # If so, the canceled terminal event was already emitted by cancel();
            # do NOT emit artifact or completed so there is only one terminal event.
            with self._active_lock:
                still_active = task_id in self._active_tasks

            if not still_active:
                log.debug("A2A task %s was canceled during dispatch — skipping completion event", task_id)
                return

            # Publish result
            artifact = Artifact(
                artifactId=uuid.uuid4().hex[:12],
                parts=[Part(root=TextPart(text=response_text))],
            )
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    artifact=artifact,
                )
            )
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.completed,
                        timestamp=_now(),
                    ),
                )
            )
        except Exception as exc:
            log.error("A2A task %s failed: %s", task_id, exc, exc_info=True)
            # Same canceled-wins check for the failure path
            with self._active_lock:
                still_active = task_id in self._active_tasks
            if not still_active:
                return
            await event_queue.enqueue_event(
                TaskStatusUpdateEvent(
                    taskId=task_id,
                    contextId=context_id,
                    final=True,
                    status=TaskStatus(
                        state=TaskState.failed,
                        timestamp=_now(),
                        message=Message(
                            messageId=uuid.uuid4().hex,
                            role=Role.agent,
                            parts=[Part(root=TextPart(text=f"Task failed: {exc}"))],
                        ),
                    ),
                )
            )
        finally:
            self._semaphore.release()
            with self._active_lock:
                self._active_tasks.discard(task_id)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Signal task cancellation.

        Note: A2A cancel() emits a canceled status and removes the task from the
        active set so no further status updates are emitted. It does NOT interrupt
        an already-running handle_chat_direct() call — Ouroboros tasks run to
        completion naturally. The canceled status informs the client that the agent
        considers the task canceled; resource consumption continues until the
        underlying agent task finishes.
        """
        task_id = context.task_id
        with self._active_lock:
            self._active_tasks.discard(task_id)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                taskId=task_id,
                contextId=context.context_id,
                final=True,
                status=TaskStatus(
                    state=TaskState.canceled,
                    timestamp=_now(),
                ),
            )
        )

    async def _dispatch_to_supervisor(
        self,
        text: str,
        event_queue: EventQueue,
        task_id: str,
        context_id: str,
    ) -> str:
        """Send message to Ouroboros agent and wait for response."""
        from supervisor.message_bus import try_get_bridge
        from supervisor.workers import handle_chat_direct

        bridge = try_get_bridge()
        if bridge is None:
            raise RuntimeError("Supervisor not ready — bridge not initialized yet")
        chat_id = _next_a2a_chat_id()
        response_event = asyncio.Event()
        response_holder: dict = {}
        loop = asyncio.get_running_loop()

        def on_response(resp_text: str) -> None:
            response_holder["text"] = resp_text
            loop.call_soon_threadsafe(response_event.set)

        sub_id = bridge.subscribe_response(chat_id, on_response)
        try:
            # A2A traffic is isolated from the human-visible chat UI.
            # Negative chat_ids (A2A virtual ids from _next_a2a_chat_id) must NOT
            # appear as live WebSocket messages or history-replay entries in the UI.
            # Guards: supervisor/message_bus.broadcast() and send_message() both check
            # chat_id >= 0; server_history_api.py skips negative chat_id on replay.
            # A2A traffic is logged to a dedicated a2a.log (NOT to chat.jsonl) to avoid
            # polluting the agent's dialogue memory / consolidation pipeline.
            # Guards against UI leakage: broadcast() and send_message() check chat_id >= 0;
            # server_history_api.py skips negative chat_id entries on history replay.

            # Run handle_chat_direct in a thread (it's blocking).
            # handle_chat_direct acquires the module-level _chat_agent_lock in
            # supervisor/workers.py, serializing all callers (A2A + Web UI)
            # against the shared _chat_agent singleton.
            await asyncio.to_thread(handle_chat_direct, chat_id, text, None)

            # Wait for response with timeout (use hard timeout from config)
            try:
                timeout_sec = int(os.environ.get("OUROBOROS_HARD_TIMEOUT_SEC", "1800"))
            except (TypeError, ValueError):
                timeout_sec = 1800
            try:
                await asyncio.wait_for(response_event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                raise TimeoutError(f"Agent did not respond within {timeout_sec}s")

            return response_holder.get("text", "(no response)")
        finally:
            bridge.unsubscribe_response(sub_id)

    @staticmethod
    def _extract_text(message: Optional[Message]) -> str:
        if not message or not message.parts:
            return ""
        texts = []
        for part in message.parts:
            inner = part.root if hasattr(part, "root") else part
            if isinstance(inner, TextPart):
                texts.append(inner.text)
            elif hasattr(inner, "text"):
                texts.append(str(inner.text))
        return "\n".join(texts).strip()
