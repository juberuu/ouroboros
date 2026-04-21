"""HTTP + WebSocket envelope shapes (v1, descriptive but tight).

These ``TypedDict`` definitions document the exact payloads the current
runtime sends and accepts. They are descriptive contracts, not validators —
nothing in the runtime rejects extra or missing fields yet. Their job is to:

- make WS/HTTP envelopes *visible* as a stable surface so external
  skills/extensions can consume them without grepping ``server.py``;
- anchor regression tests in ``tests/test_contracts.py`` that read the
  broadcast/response shapes and assert the required keys are still present;
- serve as the single place where new envelope keys are added when the
  runtime evolves.

Conventions
-----------
- Default to ``total=True`` (keys listed at the top level are required).
- Mark genuinely optional keys with ``NotRequired[...]``.
- Keep ``type`` (the discriminator) always required on every envelope so
  clients can dispatch by it.

Inbound WebSocket (client -> server; see ``server.ws_endpoint``)
----------------------------------------------------------------
- ``ChatInbound``    — user message from the web UI.
- ``CommandInbound`` — header-button command (``evolve``, ``panic`` …).

Outbound WebSocket (server -> client; see ``supervisor.message_bus``
+ ``server.broadcast_ws``)
--------------------------------------------------------------------
- ``ChatOutbound``   — assistant/user/system chat frame.
- ``PhotoOutbound``  — base64-encoded image with optional caption.
- ``TypingOutbound`` — typing indicator.
- ``LogOutbound``    — streamed log/event envelope.

HTTP responses
--------------
- ``HealthResponse``       — ``GET /api/health``.
- ``StateResponse``        — ``GET /api/state`` (happy path; the error path
  returns ``{"error": str}`` with a different shape not declared here).
- ``SettingsNetworkMeta``  — the ``_meta`` block injected into
  ``GET /api/settings``.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

try:  # Python 3.11+ — ``NotRequired`` and ``Literal`` in ``typing``.
    from typing import TypedDict, Literal, NotRequired  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover — CI pins Python 3.10.
    from typing_extensions import TypedDict, Literal, NotRequired  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# WebSocket — inbound (``web/modules/ws.js`` -> ``server.ws_endpoint``)
# ---------------------------------------------------------------------------

class ChatInbound(TypedDict):
    """Inbound WS chat message. ``type`` and ``content`` are always required.

    ``sender_session_id`` and ``client_message_id`` are supplied by
    ``web/modules/ws.js`` but are optional — server-generated tests or
    external skills may omit them.
    """

    type: Literal["chat"]
    content: str
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]


class CommandInbound(TypedDict):
    """Inbound WS command message (header buttons).

    ``server.ws_endpoint`` reads ``msg.get("cmd", "")`` and drops the message
    silently when empty, so treat ``cmd`` as required in the contract — an
    empty ``cmd`` is not a valid command.
    """

    type: Literal["command"]
    cmd: str


# ---------------------------------------------------------------------------
# WebSocket — outbound (``supervisor.message_bus`` / ``server.broadcast_ws``)
# ---------------------------------------------------------------------------

class ChatOutbound(TypedDict):
    """Outbound WS chat frame.

    Required keys are the ones every ``_broadcast_fn({...})`` call in
    ``supervisor/message_bus.py`` sets unconditionally. Provenance fields
    (``source``, ``sender_label``, …) are populated only by some code paths
    (Telegram ingestion, web handler) and are ``NotRequired``.
    """

    type: Literal["chat"]
    role: Literal["user", "assistant", "system"]
    content: str
    ts: str
    markdown: NotRequired[bool]
    is_progress: NotRequired[bool]
    task_id: NotRequired[str]
    source: NotRequired[str]
    sender_label: NotRequired[str]
    sender_session_id: NotRequired[str]
    client_message_id: NotRequired[str]
    telegram_chat_id: NotRequired[int]


class PhotoOutbound(TypedDict):
    """Outbound WS photo frame."""

    type: Literal["photo"]
    role: Literal["user", "assistant"]
    image_base64: str
    mime: str
    ts: str
    caption: NotRequired[str]
    source: NotRequired[str]
    sender_label: NotRequired[str]
    telegram_chat_id: NotRequired[int]


class TypingOutbound(TypedDict):
    """Outbound WS typing indicator."""

    type: Literal["typing"]
    action: str


class LogOutbound(TypedDict):
    """Outbound WS log event — ``data`` matches one JSONL line."""

    type: Literal["log"]
    data: Dict[str, Any]


# ---------------------------------------------------------------------------
# HTTP responses
# ---------------------------------------------------------------------------

class HealthResponse(TypedDict):
    """Shape of ``GET /api/health`` (always 200, always 4 keys)."""

    status: Literal["ok"]
    version: str
    runtime_version: str
    app_version: str


class EvolutionStateSnapshot(TypedDict):
    """Nested ``evolution_state`` block inside ``StateResponse``.

    Every field is set unconditionally by
    ``supervisor.queue.get_evolution_status_snapshot()``.
    """

    enabled: bool
    status: str
    detail: str
    cycle: int
    owner_chat_bound: bool
    last_task_at: str
    consecutive_failures: int
    budget_remaining_usd: float
    budget_reserve_usd: float
    pending_count: int
    running_count: int
    queued_task_id: str
    running_task_id: str


class StateResponse(TypedDict):
    """Shape of ``GET /api/state`` (happy path).

    The happy path in ``server.api_state`` emits every key listed here
    unconditionally. The error path returns a separate ``{"error": str}``
    payload with HTTP 500 and is intentionally not pinned here.
    ``supervisor_error`` is ``Optional[str]`` because it may legitimately
    be ``None`` while the supervisor is running cleanly.
    """

    uptime: int
    workers_alive: int
    workers_total: int
    pending_count: int
    running_count: int
    spent_usd: float
    budget_limit: float
    budget_pct: float
    branch: str
    sha: str
    evolution_enabled: bool
    bg_consciousness_enabled: bool
    evolution_cycle: int
    evolution_state: EvolutionStateSnapshot
    bg_consciousness_state: Dict[str, Any]
    spent_calls: int
    supervisor_ready: bool
    supervisor_error: Optional[str]


class SettingsNetworkMeta(TypedDict):
    """``_meta`` block injected into ``GET /api/settings``.

    Produced by ``server._build_network_meta``; every key is set in every
    branch (loopback / lan_reachable / host_ip_unknown), so all fields are
    required by the contract.
    """

    bind_host: str
    bind_port: int
    lan_ip: str
    reachability: Literal["loopback_only", "lan_reachable", "host_ip_unknown"]
    recommended_url: str
    warning: str


# The ``SettingsResponse`` surface as a whole is intentionally not pinned —
# the runtime returns a shallow copy of ``SETTINGS_DEFAULTS`` (itself the
# shape SSOT) overlaid with user values, masked secrets, and ``_meta``.
# Listing every key here would duplicate ``ouroboros.config.SETTINGS_DEFAULTS``.


__all__ = [
    "ChatInbound",
    "CommandInbound",
    "ChatOutbound",
    "PhotoOutbound",
    "TypingOutbound",
    "LogOutbound",
    "HealthResponse",
    "StateResponse",
    "EvolutionStateSnapshot",
    "SettingsNetworkMeta",
]
