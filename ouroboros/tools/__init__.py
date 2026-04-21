"""
Ouroboros — Tool package (plugin architecture).

Re-exports: ToolRegistry, ToolContext, ToolEntry.
To add a tool: create a module in this package, export get_tools().
"""

from ouroboros.tools.registry import ToolRegistry, ToolContext, ToolEntry

__all__ = ['ToolRegistry', 'ToolContext', 'ToolEntry']
