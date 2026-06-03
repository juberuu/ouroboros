"""Drift-guard: AGENT-context size budgets are a single source of truth.

Mirrors ``test_max_tokens_constants.py``: pin the SSOT values, assert the call
sites consume them, and assert the old bare literals are gone so the low/max
context work cannot silently re-pin a budget in one module while the others
drift. The AGENT-context budgets live in ``ouroboros.context_budget`` and are
deliberately separate from the REVIEW-prompt budget family
(``review_helpers.REVIEW_PROMPT_TOKEN_BUDGET`` / ``scope_review`` window).
"""

import inspect
import pathlib

from ouroboros import context_budget as cb


def _src(rel: str) -> str:
    return pathlib.Path(rel).read_text(encoding="utf-8")


def test_agent_context_budget_values_pinned():
    """Values are the SSOT; changing them is a deliberate, visible edit."""
    assert cb.EMERGENCY_COMPACTION_CHARS == 1_200_000
    assert cb.BG_CONTEXT_WARN_CHARS == 600_000
    assert cb.BG_CONTEXT_MAX_CHARS == 1_200_000
    assert cb.BG_STATE_JSON_WARN_CHARS == 200_000
    assert cb.LARGE_CONTEXT_SECTION_CHARS == 200_000
    assert cb.CONTEXT_SOFT_CAP_TOKENS == 200_000
    # Low-profile overrides (≈200K window).
    assert cb.MAX_RECENT_CHAT_TAIL == 1000
    assert cb.LOW_EMERGENCY_COMPACTION_CHARS == 400_000
    # Low must compact strictly sooner than max without shortening recent-dialogue
    # raw history unless consolidation already represents the older span.
    assert cb.LOW_EMERGENCY_COMPACTION_CHARS < cb.EMERGENCY_COMPACTION_CHARS


def test_call_sites_consume_the_ssot_at_runtime():
    """Consuming modules must reference the SSOT, not their own copies."""
    from ouroboros import context as ctxmod
    from ouroboros.context import build_llm_messages

    assert ctxmod._LARGE_CONTEXT_SECTION_CHARS == cb.LARGE_CONTEXT_SECTION_CHARS
    assert (
        inspect.signature(build_llm_messages).parameters["soft_cap_tokens"].default
        == cb.CONTEXT_SOFT_CAP_TOKENS
    )


def test_call_sites_import_the_ssot_names():
    loop_src = _src("ouroboros/loop.py")
    assert "EMERGENCY_COMPACTION_CHARS" in loop_src
    assert "LOW_EMERGENCY_COMPACTION_CHARS" in loop_src  # profile-keyed in low

    ctx_recent_src = _src("ouroboros/context.py")
    assert "MAX_RECENT_CHAT_TAIL" in ctx_recent_src
    assert "consolidated_offset > 0" in ctx_recent_src

    consc_src = _src("ouroboros/consciousness.py")
    for name in ("BG_CONTEXT_MAX_CHARS", "BG_CONTEXT_WARN_CHARS", "BG_STATE_JSON_WARN_CHARS"):
        assert name in consc_src, f"consciousness.py must consume {name}"

    ctx_src = _src("ouroboros/context.py")
    assert "LARGE_CONTEXT_SECTION_CHARS" in ctx_src
    assert "CONTEXT_SOFT_CAP_TOKENS" in ctx_src

    assert "CONTEXT_SOFT_CAP_TOKENS" in _src("ouroboros/agent.py")


def test_old_bare_literals_are_gone_from_call_sites():
    """The decisive anti-drift check: no bare literal can outlive the SSOT."""
    assert "> 1_200_000" not in _src("ouroboros/loop.py")

    consc = _src("ouroboros/consciousness.py")
    assert "= 1_200_000" not in consc
    assert "= 600_000" not in consc
    assert "> 200_000" not in consc

    ctx = _src("ouroboros/context.py")
    assert "= 200_000" not in ctx

    assert "_soft_cap = 200_000" not in _src("ouroboros/agent.py")
