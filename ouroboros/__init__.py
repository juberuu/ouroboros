"""
Уроборос — самомодифицирующийся агент.

Философия: BIBLE.md
Архитектура: agent.py (оркестратор), tools/ (плагинные инструменты),
             llm.py (LLM), memory.py (память), review.py (deep review),
             utils.py (общие утилиты).
"""

# IMPORTANT: Do NOT import agent/loop/llm/etc here!
# colab_launcher.py imports ouroboros.apply_patch, which triggers __init__.py.
# Any eager imports here get loaded into supervisor's memory and persist
# in forked worker processes as stale code, preventing hot-reload.
# Workers import make_agent directly from ouroboros.agent.

__all__ = ['agent', 'tools', 'llm', 'memory', 'review', 'utils']
__version__ = '4.0.1'
