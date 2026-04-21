"""Test reasoning effort resolution via config.resolve_effort()."""

import os
from unittest.mock import patch
from ouroboros.config import resolve_effort
# Backward-compat shim in agent.py must still work
from ouroboros.agent import _resolve_initial_effort


# ---------------------------------------------------------------------------
# Task / Chat
# ---------------------------------------------------------------------------

def test_task_effort_default_is_medium():
    """Default task effort is 'medium' when no env var is set."""
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_effort("task") == "medium"
        assert resolve_effort("chat") == "medium"
        assert resolve_effort("") == "medium"


def test_task_effort_via_new_env():
    """OUROBOROS_EFFORT_TASK controls task/chat effort."""
    for effort in ("none", "low", "medium", "high"):
        with patch.dict(os.environ, {"OUROBOROS_EFFORT_TASK": effort}, clear=True):
            assert resolve_effort("task") == effort


def test_task_effort_legacy_fallback():
    """Legacy OUROBOROS_INITIAL_REASONING_EFFORT is honoured when OUROBOROS_EFFORT_TASK is absent."""
    with patch.dict(os.environ, {"OUROBOROS_INITIAL_REASONING_EFFORT": "medium"}, clear=True):
        assert resolve_effort("task") == "medium"


def test_task_effort_new_takes_precedence_over_legacy():
    """OUROBOROS_EFFORT_TASK wins over legacy alias."""
    env = {"OUROBOROS_EFFORT_TASK": "high", "OUROBOROS_INITIAL_REASONING_EFFORT": "low"}
    with patch.dict(os.environ, env, clear=True):
        assert resolve_effort("task") == "high"


def test_task_effort_invalid_falls_back_to_medium():
    """Invalid effort values fall back to 'medium'."""
    with patch.dict(os.environ, {"OUROBOROS_EFFORT_TASK": "extreme"}, clear=True):
        assert resolve_effort("task") == "medium"


# ---------------------------------------------------------------------------
# Evolution
# ---------------------------------------------------------------------------

def test_evolution_effort_default_is_high():
    """Default evolution effort is 'high'."""
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_effort("evolution") == "high"


def test_evolution_effort_configurable():
    """Evolution effort can be overridden via OUROBOROS_EFFORT_EVOLUTION."""
    with patch.dict(os.environ, {"OUROBOROS_EFFORT_EVOLUTION": "medium"}, clear=True):
        assert resolve_effort("evolution") == "medium"


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------

def test_review_effort_default_is_medium():
    """Default review effort is 'medium'."""
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_effort("review") == "medium"


def test_review_effort_configurable():
    """Review effort can be overridden via OUROBOROS_EFFORT_REVIEW."""
    with patch.dict(os.environ, {"OUROBOROS_EFFORT_REVIEW": "high"}, clear=True):
        assert resolve_effort("review") == "high"


# ---------------------------------------------------------------------------
# Consciousness
# ---------------------------------------------------------------------------

def test_consciousness_effort_default_is_low():
    """Default consciousness effort is 'low'."""
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_effort("consciousness") == "low"


def test_consciousness_effort_configurable():
    """Consciousness effort can be overridden via OUROBOROS_EFFORT_CONSCIOUSNESS."""
    with patch.dict(os.environ, {"OUROBOROS_EFFORT_CONSCIOUSNESS": "none"}, clear=True):
        assert resolve_effort("consciousness") == "none"


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------

def test_task_type_is_case_insensitive():
    """Task type matching is case-insensitive."""
    with patch.dict(os.environ, {}, clear=True):
        assert resolve_effort("EVOLUTION") == "high"
        assert resolve_effort("Review") == "medium"
        assert resolve_effort("CONSCIOUSNESS") == "low"


# ---------------------------------------------------------------------------
# Backward-compat shim
# ---------------------------------------------------------------------------

def test_shim_still_works():
    """_resolve_initial_effort in agent.py is still callable and correct."""
    with patch.dict(os.environ, {}, clear=True):
        assert _resolve_initial_effort("task") == "medium"
        assert _resolve_initial_effort("evolution") == "high"
        assert _resolve_initial_effort("review") == "medium"
