"""
Уроборос — Пакет инструментов (плагинная архитектура).

Реэкспорт: ToolRegistry, ToolContext, ToolEntry.
Добавить инструмент: создать модуль в этом пакете, экспортировать get_tools().
"""

from ouroboros.tools.registry import ToolRegistry, ToolContext, ToolEntry

__all__ = ['ToolRegistry', 'ToolContext', 'ToolEntry']
