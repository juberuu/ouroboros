"""
Уроборос — самомодифицирующийся агент.

Философия: BIBLE.md
Архитектура: agent.py (оркестратор), tools.py (SSOT инструменты),
             llm.py (LLM), memory.py (память), review.py (deep review),
             utils.py (общие утилиты).
"""

from ouroboros.agent import make_agent

__all__ = ['make_agent', 'agent', 'tools', 'llm', 'memory', 'review', 'utils']
__version__ = '1.1.0'
