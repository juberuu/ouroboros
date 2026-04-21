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

    return name in CORE_TOOL_NAMES or name in META_TOOL_NAMES


def initial_tool_schemas(registry: ToolSchemaProvider) -> List[Dict[str, Any]]:
    """Return the schemas that should be present from round 1."""

    result = []
    for schema in registry.schemas():
        name = schema.get("function", {}).get("name", "")
        if is_initial_task_tool(name):
            result.append(schema)
    return result


def list_non_core_tools(registry: ToolSchemaProvider) -> List[Dict[str, str]]:
    """Return name+description for tools that require explicit enable_tools."""

    result = []
    for schema in registry.schemas():
        function = schema.get("function", {})
        name = function.get("name", "")
        if not name or is_initial_task_tool(name):
            continue
        result.append({
            "name": name,
            "description": function.get("description", "No description"),
        })
    return result
