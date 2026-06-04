"""Task-start tool visibility policy.

This module determines which tools are available at the start of a task
without an explicit ``enable_tools`` call.

Tool sets are imported from ``ouroboros.tool_capabilities`` (the single
source of truth).  This module adds the visibility-decision logic on top.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol

from ouroboros.tool_capabilities import CORE_TOOL_NAMES, META_TOOL_NAMES


class ToolSchemaProvider(Protocol):
    """Minimal registry contract needed by the loop/discovery helpers."""

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        ...


def is_initial_task_tool(name: str) -> bool:
    """Return True if the tool should be loaded before any enable_tools call."""

    return str(name or "").strip() in CORE_TOOL_NAMES or str(name or "").strip() in META_TOOL_NAMES


def initial_tool_schemas(registry: ToolSchemaProvider) -> List[Dict[str, Any]]:
    """Return the full capability envelope that should be present from round 1.

    Visibility is selected by the registry context: normal main/direct/evolution
    tasks expose all available first-party built-ins plus live extension/MCP
    schemas; workspace and local-readonly tasks expose their guarded envelope.
    No enabled schema is silently skipped here.
    """

    return registry.schemas()


def list_non_core_tools(registry: ToolSchemaProvider) -> List[Dict[str, str]]:
    """Return name+description for tools that require explicit enable_tools."""

    return []
