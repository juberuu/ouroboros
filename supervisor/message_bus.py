"""
Supervisor — Message Bus & Formatting.

Queue-based message bus that connects local UI/skill transports and the
Agent Supervisor.
"""

from __future__ import annotations

import base64
import logging
import queue
import re
import threading
from typing import Any, Dict, List, Optional

from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.event_bus import CHAT_OUTBOUND, CHAT_PHOTO, CHAT_TYPING, publish_event
from supervisor.state import append_jsonl, load_state, save_state
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level config (set via init())
# ---------------------------------------------------------------------------
DATA_DIR = None  # pathlib.Path
TOTAL_BUDGET_LIMIT: float = 0.0
BUDGET_REPORT_EVERY_MESSAGES: int = 10
_BRIDGE: Optional["LocalChatBridge"] = None


def init(
    drive_root,
    total_budget_limit: float,
    budget_report_every: int,
    chat_bridge: "LocalChatBridge",
) -> None:
    global DATA_DIR, TOTAL_BUDGET_LIMIT, BUDGET_REPORT_EVERY_MESSAGES, _BRIDGE
    DATA_DIR = drive_root
    TOTAL_BUDGET_LIMIT = total_budget_limit
    BUDGET_REPORT_EVERY_MESSAGES = budget_report_every
    _BRIDGE = chat_bridge


def get_bridge() -> "LocalChatBridge":
    assert _BRIDGE is not None, "message_bus.init() not called"
    return _BRIDGE


def try_get_bridge() -> "Optional[LocalChatBridge]":
    """Return the bridge or None if not yet initialized (safe for early callers)."""
    return _BRIDGE


def refresh_budget_limit(new_limit: Optional[float]) -> None:
    """Hot-reload the total budget limit used for status messages.

    Accepts None gracefully (treated as 0.0 / no limit).
    """
    global TOTAL_BUDGET_LIMIT
    try:
        TOTAL_BUDGET_LIMIT = float(new_limit) if new_limit is not None else 0.0
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# LocalChatBridge
# ---------------------------------------------------------------------------

class LocalChatBridge:
    """Local message bus using queue.Queue."""

    def __init__(self, settings: Optional[Dict[str, Any]] = None):
        self._inbox = queue.Queue()   # user -> agent
        self._outbox = queue.Queue()  # agent -> UI
        self._log_queue: queue.Queue = queue.Queue(maxsize=1000)
        self._update_counter = 0
        self._broadcast_fn = None  # set by server.py for WebSocket streaming
        # A2A response subscriptions: {subscription_id: (chat_id, callback)}
        self._response_subs: Dict[str, tuple] = {}
        self._response_subs_lock = threading.Lock()
        self._chat_transports: Dict[int, Dict[str, Any]] = {}
        if settings:
            self.configure_from_settings(settings)

    def broadcast(self, payload: dict) -> None:
        """Broadcast a payload to WebSocket clients if the broadcast hook is wired.

        A2A virtual chat_ids are intentionally skipped so that
        A2A task traffic does not appear in the human-visible chat UI live stream.
        The history API (server_history_api.py) separately filters A2A chat_ids
        from page-reload history, providing consistent isolation.
        """
        chat_id = payload.get("chat_id")
        if is_a2a_chat_id(chat_id):
            return
        if self._broadcast_fn:
            self._broadcast_fn(payload)

    def get_updates(self, offset: int, timeout: int = 10) -> List[Dict[str, Any]]:
        """Block on the inbox queue and return updates."""
        try:
            raw_msg = self._inbox.get(timeout=timeout)
            if isinstance(raw_msg, str):
                msg = {
                    "chat_id": 1,
                    "user_id": 1,
                    "text": raw_msg,
                    "source": "web",
                    "sender_label": "",
                }
            else:
                msg = dict(raw_msg or {})

            message = {
                "chat": {"id": int(msg.get("chat_id") or 1)},
                "from": {"id": int(msg.get("user_id") or 1)},
                "text": str(msg.get("text") or ""),
                "source": str(msg.get("source") or "web"),
            }
            chat_id_value = int(msg.get("chat_id") or 1)
            if isinstance(msg.get("transport"), dict) and msg.get("transport") and chat_id_value != 1:
                self._chat_transports[chat_id_value] = dict(msg.get("transport") or {})
            else:
                self._chat_transports.pop(chat_id_value, None)
            for key in (
                "sender_label",
                "sender_session_id",
                "client_message_id",
                "transport",
                "image_base64",
                "image_mime",
                "image_caption",
                "suppress_chat_log",
                "task_constraint",
            ):
                value = msg.get(key)
                if value not in (None, "", 0):
                    message[key] = value

            self._update_counter = max(offset, self._update_counter + 1)
            return [{
                "update_id": self._update_counter,
                "message": message,
            }]
        except queue.Empty:
            return []

    def configure_from_settings(self, settings: Dict[str, Any]) -> None:
        """Compatibility no-op; chat bridges are now skills."""
        return None

    def subscribe_response(self, chat_id: int, callback) -> str:
        """Subscribe to agent responses for a given chat_id. Returns subscription_id."""
        import uuid as _uuid
        sub_id = _uuid.uuid4().hex
        with self._response_subs_lock:
            self._response_subs[sub_id] = (chat_id, callback)
        return sub_id

    def unsubscribe_response(self, subscription_id: str) -> None:
        """Remove a response subscription."""
        with self._response_subs_lock:
            self._response_subs.pop(subscription_id, None)

    def shutdown(self) -> None:
        return None

    def handle_web_message(
        self,
        text: str,
        *,
        sender_session_id: str = "",
        client_message_id: str = "",
    ) -> None:
        clean_text = str(text or "").strip()
        if not clean_text:
            return
        ts = utc_now_iso()
        if self._broadcast_fn:
            self._broadcast_fn({
                "type": "chat",
                "role": "user",
                "content": clean_text,
                "ts": ts,
                "source": "web",
                "sender_session_id": sender_session_id,
                "client_message_id": client_message_id,
            })
        self.enqueue_local_message(
            clean_text,
            chat_id=1,
            user_id=1,
            source="web",
            sender_label="",
            sender_session_id=sender_session_id,
            client_message_id=client_message_id,
                    )

    def enqueue_local_message(
        self,
        text: str,
        *,
        chat_id: int = 1,
        user_id: int = 1,
        source: str = "web",
        sender_label: str = "",
        sender_session_id: str = "",
        client_message_id: str = "",
        transport: Optional[Dict[str, Any]] = None,
        image_base64: str = "",
        image_mime: str = "",
        image_caption: str = "",
        suppress_chat_log: bool = False,
        task_constraint: Optional[Dict[str, Any]] = None,
    ) -> None:
        clean_text = str(text or "").strip()
        caption_text = str(image_caption or "").strip()
        image_b64 = str(image_base64 or "").strip()
        if not clean_text and caption_text:
            clean_text = caption_text
        if not clean_text and not image_b64:
            return
        self._inbox.put({
            "chat_id": int(chat_id or 1),
            "user_id": int(user_id or 1),
            "text": clean_text,
            "source": str(source or "web"),
            "sender_label": str(sender_label or ""),
            "sender_session_id": str(sender_session_id or ""),
            "client_message_id": str(client_message_id or ""),
            "transport": dict(transport or {}),
            "image_base64": image_b64,
            "image_mime": str(image_mime or ""),
            "image_caption": caption_text,
            "suppress_chat_log": bool(suppress_chat_log),
            "task_constraint": dict(task_constraint or {}),
        })

    def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "",
        ts: Optional[str] = None,
        is_progress: bool = False,
        task_id: str = "",
    ) -> Tuple[bool, str]:
        """Put a message in the outbox for the UI to consume."""
        clean_text = _strip_markdown(text) if not parse_mode else text
        message_ts = ts or utc_now_iso()
        transport = dict(self._chat_transports.get(int(chat_id or 0), {}) or {})
        msg = {
            "type": "text",
            "content": clean_text,
            "markdown": bool(parse_mode),
            "is_progress": bool(is_progress),
            "ts": message_ts,
            "task_id": str(task_id or ""),
        }
        self._outbox.put(msg)
        # Notify A2A response subscribers
        with self._response_subs_lock:
            subs = [(sid, cb) for sid, (cid, cb) in self._response_subs.items()
                    if cid == chat_id and not is_progress]
        for sid, cb in subs:
            try:
                cb(clean_text)
            except Exception:
                log.debug("A2A response callback error for sub %s", sid, exc_info=True)
        # Skip WebSocket broadcast for A2A virtual chat_ids.
        if self._broadcast_fn and not is_a2a_chat_id(chat_id):
            self._broadcast_fn({
                "type": "chat",
                "role": "assistant",
                "content": clean_text,
                "markdown": bool(parse_mode),
                "is_progress": bool(is_progress),
                "ts": message_ts,
                "task_id": str(task_id or ""),
                "transport": transport,
            })
        if not is_a2a_chat_id(chat_id):
            publish_event(CHAT_OUTBOUND, {
                "chat_id": int(chat_id or 0),
                "text": clean_text,
                "markdown": bool(parse_mode),
                "is_progress": bool(is_progress),
                "ts": message_ts,
                "task_id": str(task_id or ""),
                "transport": transport,
            })
        return True, "ok"

    def send_chat_action(self, chat_id: int, action: str = "typing") -> bool:
        """Send typing indicator to UI via WebSocket broadcast."""
        if is_a2a_chat_id(chat_id):
            return True
        self._outbox.put({
            "type": "action",
            "content": action,
        })
        if self._broadcast_fn:
            self._broadcast_fn({"type": "typing", "action": action})
        typing_transport = dict(self._chat_transports.get(int(chat_id or 0), {}) or {})
        publish_event(CHAT_TYPING, {"chat_id": int(chat_id or 0), "action": str(action or ""), "transport": typing_transport})
        return True

    def send_photo(
        self,
        chat_id: int,
        photo_bytes: bytes,
        caption: str = "",
        mime: str = "image/png",
    ) -> Tuple[bool, str]:
        """Send photo to UI and host event subscribers."""
        if is_a2a_chat_id(chat_id):
            return True, "ok"
        b64_str = base64.b64encode(photo_bytes).decode("ascii")
        msg = {
            "type": "photo",
            "role": "assistant",
            "image_base64": b64_str,
            "mime": mime,
            "caption": caption,
            "ts": utc_now_iso(),
        }
        self._outbox.put(msg)
        if self._broadcast_fn:
            self._broadcast_fn(msg)
        photo_transport = dict(self._chat_transports.get(int(chat_id or 0), {}) or {})
        publish_event(CHAT_PHOTO, {
            "chat_id": int(chat_id or 0),
            "transport": photo_transport,
            "caption": str(caption or ""),
            "image_base64": b64_str,
            "mime": str(mime or ""),
            "ts": msg["ts"],
        })
        return True, "ok"

    # Log streaming
    def push_log(self, event: dict):
        """Called by append_jsonl hook to stream log events to the UI."""
        try:
            self._log_queue.put_nowait(event)
        except queue.Full:
            try:
                self._log_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._log_queue.put_nowait(event)
            except queue.Full:
                pass
        if self._broadcast_fn:
            self._broadcast_fn({"type": "log", "data": event})

    def ui_poll_logs(self) -> list:
        """Called by the web UI to drain pending log events."""
        batch = []
        for _ in range(50):
            try:
                batch.append(self._log_queue.get_nowait())
            except queue.Empty:
                break
        return batch

    # UI hooks
    def ui_send(
        self,
        text: str,
        *,
        broadcast: bool = True,
        sender_session_id: str = "",
        client_message_id: str = "",
        suppress_chat_log: bool = False,
        task_constraint: Optional[Dict[str, Any]] = None,
    ):
        """Called by the web UI to send a message to the agent."""
        if broadcast:
            self.handle_web_message(
                text,
                sender_session_id=sender_session_id,
                client_message_id=client_message_id,
            )
            return
        self.enqueue_local_message(text, suppress_chat_log=suppress_chat_log, task_constraint=task_constraint)

    def ui_receive(self, timeout: float = 0.1) -> Optional[Dict[str, Any]]:
        """Called by the web UI to check for new messages from the agent."""
        try:
            return self._outbox.get(timeout=timeout)
        except queue.Empty:
            return None


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _strip_markdown(text: str) -> str:
    """Strip all markdown formatting markers, leaving only plain text."""
    text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"\1", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\*\-]\s+", "• ", text, flags=re.MULTILINE)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    text = text.replace("`", "")
    return text


def _send_markdown(
    chat_id: int,
    text: str,
    ts: Optional[str] = None,
    is_progress: bool = False,
    task_id: str = "",
) -> Tuple[bool, str]:
    """Send markdown text to the UI."""
    bridge = get_bridge()
    if not text:
        return False, "empty"
    return bridge.send_message(
        chat_id,
        text,
        parse_mode="markdown",
        ts=ts,
        is_progress=is_progress,
        task_id=task_id,
    )


# ---------------------------------------------------------------------------
# Budget + logging
# ---------------------------------------------------------------------------

def _format_budget_line(st: Dict[str, Any]) -> str:
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    pct = (spent / total * 100.0) if total > 0 else 0.0
    sha = (st.get("current_sha") or "")[:8]
    branch = st.get("current_branch") or "?"
    return f"—\nBudget: ${spent:.4f} / ${total:.2f} ({pct:.2f}%) | {branch}@{sha}"


def budget_line(force: bool = False) -> str:
    try:
        st = load_state()
        every = max(1, int(BUDGET_REPORT_EVERY_MESSAGES))
        if force:
            st["budget_messages_since_report"] = 0
            save_state(st)
            return _format_budget_line(st)

        counter = int(st.get("budget_messages_since_report") or 0) + 1
        if counter < every:
            st["budget_messages_since_report"] = counter
            save_state(st)
            return ""

        st["budget_messages_since_report"] = 0
        save_state(st)
        return _format_budget_line(st)
    except Exception:
        log.debug("Suppressed exception in budget_line", exc_info=True)
        return ""


def log_chat(
    direction: str,
    chat_id: int,
    user_id: int,
    text: str,
    ts: Optional[str] = None,
    fmt: str = "",
    source: str = "",
    sender_label: str = "",
    sender_session_id: str = "",
    client_message_id: str = "",
    transport: Optional[Dict[str, Any]] = None,
    task_id: str = "",
) -> None:
    if DATA_DIR:
        append_jsonl(DATA_DIR / "logs" / "chat.jsonl", {
            "ts": ts or utc_now_iso(),
            "session_id": load_state().get("session_id"),
            "direction": direction,
            "chat_id": chat_id,
            "user_id": user_id,
            "text": text,
            "format": fmt,
            "source": source,
            "sender_label": sender_label,
            "sender_session_id": sender_session_id,
            "client_message_id": client_message_id,
            "transport": dict(transport or {}),
            "task_id": str(task_id or ""),
        })


def send_with_budget(chat_id: int, text: str, log_text: Optional[str] = None,
                     fmt: str = "",
                     is_progress: bool = False, task_id: str = "",
                     ts: Optional[str] = None) -> None:
    st = load_state()
    owner_id = int(st.get("owner_id") or 0)
    _text = str(text or "")
    msg_ts = ts or utc_now_iso()

    if is_progress and DATA_DIR:
        append_jsonl(DATA_DIR / "logs" / "progress.jsonl", {
            "ts": msg_ts,
            "type": "send_message",
            "task_id": task_id,
            "is_progress": True,
            "direction": "out", "chat_id": chat_id, "user_id": owner_id,
            "text": text if log_text is None else log_text,
            "content": _text,
            "format": fmt,
        })
    else:
        log_chat(
            "out",
            chat_id,
            owner_id,
            text if log_text is None else log_text,
            ts=msg_ts,
            fmt=fmt,
            task_id=task_id,
        )

    if _text.strip() in ("", "\u200b"):
        return
    # Budget footers are now shown in dashboard/status flows, not auto-appended
    # to every outgoing chat message.
    full = _text

    if fmt == "markdown":
        ok, err = _send_markdown(
            chat_id,
            full,
            ts=msg_ts,
            is_progress=is_progress,
            task_id=task_id,
        )
        return

    bridge = get_bridge()
    bridge.send_message(chat_id, full, ts=msg_ts, is_progress=is_progress, task_id=task_id)
