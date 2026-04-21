"""Frozen tool-module ABI: ``ToolEntry`` shape + ``get_tools()`` signature.

Every module in ``ouroboros/tools/`` is expected to expose::

    def get_tools() -> list[ToolEntry]: ...

where each ``ToolEntry`` carries ``name``, JSON Schema, a handler callable,
and optional flags (``is_code_tool``, ``timeout_sec``). ``ToolRegistry`` auto-
discovers those via ``importlib`` + ``pkgutil.iter_modules`` in dev mode and
via the hardcoded ``_FROZEN_TOOL_MODULES`` allowlist in packaged builds.

This module declares that contract as Protocols so:

- external *extensions* (Phase 4) can type-annotate against a stable ABI
  without importing the private dataclasses;
- contract tests (``test_contracts.py``) can assert that the concrete
  ``ouroboros.tools.registry.ToolEntry`` still structurally satisfies
  ``ToolEntryProtocol`` (regression guard for ABI drift).

Intentionally *not* redefined here:

- the actual handler calling convention is ``fn(ctx, **kwargs) -> str``; the
  protocol keeps ``handler`` typed as ``Callable`` because real tools accept
  arbitrary keyword args driven by their JSON Schema.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class ToolEntryProtocol(Protocol):
    """Structural contract for a ``ToolEntry``-like descriptor.

    The concrete dataclass lives in ``ouroboros.tools.registry`` and adds
    some defaulted fields (``is_code_tool=False``, ``timeout_sec=360``).
    Only the fields listed here are part of the frozen ABI.
    """

    name: str
    schema: Dict[str, Any]
    handler: Callable[..., str]
    is_code_tool: bool
    timeout_sec: int


class GetToolsProtocol(Protocol):
    """Callable contract for ``get_tools()`` exported by every tools module."""

    def __call__(self) -> List[ToolEntryProtocol]: ...


__all__ = ["ToolEntryProtocol", "GetToolsProtocol"]
