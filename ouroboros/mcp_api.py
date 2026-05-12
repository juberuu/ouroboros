"""HTTP surface for the MCP client manager.

Adds three endpoints used by the Settings → Advanced → MCP Servers
widget:

- ``GET  /api/mcp/status``    — current servers + discovered tools (auth tokens redacted).
- ``POST /api/mcp/refresh``   — re-discover tools for one server (body ``{"server_id": "..."}``)
  or every enabled server (empty body).
- ``POST /api/mcp/test``      — probe a candidate config without saving it (body ``{"server": {...}}``).

The endpoints intentionally route through the module-level
:class:`ouroboros.mcp_client.MCPManager` so the same state the agent
sees in :class:`ouroboros.tools.registry.ToolRegistry` is what the UI
visualizes — no parallel cache to drift out of sync.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.config import load_settings
from ouroboros.mcp_client import (
    canonical_server_id,
    get_manager,
    looks_masked_secret,
    reconfigure_from_settings,
)


log = logging.getLogger(__name__)


def _error_response(message: str, *, status_code: int = 500) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _ensure_configured() -> None:
    """Reconcile the manager state with the on-disk settings.

    Calling this on every endpoint avoids a stale-state regression when
    settings are edited out-of-band (e.g. test setup or CLI script).
    Production traffic also goes through the standard ``api_settings_post``
    reconfigure call but this is cheap and idempotent.
    """
    try:
        reconfigure_from_settings(load_settings())
    except Exception:
        log.warning("MCP reconfigure_from_settings failed", exc_info=True)


async def api_mcp_status(request: Request) -> JSONResponse:
    """GET /api/mcp/status — masked status snapshot for the UI."""
    try:
        await asyncio.to_thread(_ensure_configured)
        payload = await asyncio.to_thread(get_manager().status_payload)
        return JSONResponse(payload)
    except Exception as exc:
        log.exception("api_mcp_status failed")
        return _error_response(f"{type(exc).__name__}: MCP status failed")


async def api_mcp_refresh(request: Request) -> JSONResponse:
    """POST /api/mcp/refresh — refresh one or all servers."""
    try:
        try:
            body: Dict[str, Any] = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        server_id = canonical_server_id(body.get("server_id") or "")
        await asyncio.to_thread(_ensure_configured)
        manager = get_manager()
        if server_id:
            outcome = await asyncio.to_thread(manager.refresh_server, server_id)
            return JSONResponse({"server_id": server_id, **outcome})
        outcome = await asyncio.to_thread(manager.refresh_all)
        return JSONResponse(outcome)
    except Exception as exc:
        log.exception("api_mcp_refresh failed")
        return _error_response(f"{type(exc).__name__}: MCP refresh failed")


async def api_mcp_test(request: Request) -> JSONResponse:
    """POST /api/mcp/test — probe a candidate server without saving it.

    The body contains either:

    - ``{"server": {...}}`` for a brand-new/unsaved card;
    - ``{"server_id": "..."}`` to test the persisted server exactly;
    - ``{"server_id": "...", "server": {...}}`` to test an edited saved
      card while rehydrating a still-masked ``auth_token`` from disk.

    The third form is important for the UI: users often edit URL,
    transport, auth header, or allowlist on an already-saved server
    without re-typing the secret. We must probe the edited candidate,
    not the stale persisted config, while still sending the real token.
    """
    try:
        try:
            body: Dict[str, Any] = await request.json()
        except (json.JSONDecodeError, ValueError):
            body = {}
        await asyncio.to_thread(_ensure_configured)
        manager = get_manager()
        server_id = canonical_server_id(body.get("server_id") or "")
        if server_id:
            settings = await asyncio.to_thread(load_settings)
            servers = settings.get("MCP_SERVERS") or []
            target: Dict[str, Any] | None = None
            for entry in servers:
                if isinstance(entry, dict) and canonical_server_id(entry.get("id") or "") == server_id:
                    target = dict(entry)
                    break
            if target is None:
                return JSONResponse(
                    {"ok": False, "error": f"server id {server_id!r} not found"},
                    status_code=404,
                )
            candidate = body.get("server")
            if isinstance(candidate, dict):
                # Use the edited candidate, but rehydrate masked token
                # values from the saved config. The caller can also omit
                # auth_token entirely to intentionally test without auth.
                probe = dict(candidate)
                if looks_masked_secret(probe.get("auth_token")):
                    probe["auth_token"] = str(target.get("auth_token") or "")
                target = probe
            outcome = await asyncio.to_thread(manager.test_server, target)
            return JSONResponse(outcome)
        candidate = body.get("server")
        if not isinstance(candidate, dict):
            return JSONResponse(
                {"ok": False, "error": "request body must include `server` (object) or `server_id` (string)"},
                status_code=400,
            )
        outcome = await asyncio.to_thread(manager.test_server, candidate)
        return JSONResponse(outcome)
    except Exception as exc:
        log.exception("api_mcp_test failed")
        return _error_response(f"{type(exc).__name__}: MCP test failed")
