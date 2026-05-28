"""Shared chat id policy for human-visible and synthetic conversations.

The negative chat id range is reserved for synthetic agent-to-agent traffic
that must not appear in human dialogue history or browser chat streams. Source
attribution belongs in message payloads (``source``), not in the numeric id.
"""

from __future__ import annotations

WEB_UI_CHAT_ID = 1

# Reserved for A2A-like synthetic conversations. Legacy A2A generated
# unbounded negative ids (-1001, -1002, ...), so every negative id remains
# internal for history/memory isolation.
A2A_CHAT_ID_MIN = -3999
A2A_CHAT_ID_MAX = -1


def is_a2a_chat_id(chat_id: object) -> bool:
    """Return True when ``chat_id`` belongs to synthetic A2A traffic."""
    try:
        value = int(chat_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return value < 0


def is_internal_chat_id(chat_id: object) -> bool:
    """Return True for synthetic chat ids hidden from human-facing history."""
    return is_a2a_chat_id(chat_id)


__all__ = [
    "A2A_CHAT_ID_MAX",
    "A2A_CHAT_ID_MIN",
    "WEB_UI_CHAT_ID",
    "is_a2a_chat_id",
    "is_internal_chat_id",
]
