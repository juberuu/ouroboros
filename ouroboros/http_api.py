"""Shared Starlette HTTP-API plumbing used across ``ouroboros`` route modules.

Pre-v5.15 every API module (``extensions_api``, ``marketplace_api``,
``file_browser_api``, ``server.py``) carried its own private copies of these
helpers. Centralising them keeps the API modules thin and lets future helpers
land in one place instead of being copied into N modules.
"""
from __future__ import annotations

import pathlib
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse


_TRUE_LITERALS = frozenset({"1", "true", "yes", "on"})
_FALSE_LITERALS = frozenset({"0", "false", "no", "off"})


def request_drive_root(request: Request) -> pathlib.Path:
    """Drive root pinned on ``request.app.state`` or the configured default."""
    from ouroboros.config import DATA_DIR
    state = getattr(request.app, "state", None)
    drive_root = getattr(state, "drive_root", None) if state is not None else None
    return pathlib.Path(drive_root) if drive_root is not None else pathlib.Path(DATA_DIR)


def request_repo_dir(request: Request) -> pathlib.Path:
    """Repo dir pinned on ``request.app.state`` or the configured default."""
    from ouroboros.config import REPO_DIR
    state = getattr(request.app, "state", None)
    repo_dir = getattr(state, "repo_dir", None) if state is not None else None
    return pathlib.Path(repo_dir) if repo_dir is not None else pathlib.Path(REPO_DIR)


def coerce_bool(value: Any, default: bool = False) -> bool:
    """Best-effort bool coercion accepting common HTTP truthy/falsy literals."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_LITERALS:
            return True
        if lowered in _FALSE_LITERALS:
            return False
    return default


def coerce_int(value: Any, default: int = 0) -> int:
    """Best-effort int coercion. Returns ``default`` on parse failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def json_error(message: str, status: int = 500, **extra: Any) -> JSONResponse:
    """``JSONResponse({"error": message, **extra}, status_code=status)``."""
    payload: dict[str, Any] = {"error": message}
    payload.update(extra)
    return JSONResponse(payload, status_code=status)


__all__ = (
    "coerce_bool",
    "coerce_int",
    "json_error",
    "request_drive_root",
    "request_repo_dir",
)
