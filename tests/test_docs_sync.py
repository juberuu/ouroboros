"""Guardrails for architecture docs after UI/routing overhaul.

README prose pins were retired in v5.8.3-rc.5 — the README is intentionally
allowed to evolve its marketing copy without dragging tests along; the
ARCHITECTURE.md pins below are the load-bearing rationale-layer guards
(P6) that must survive every doc-touch commit.
"""

import os
import pathlib

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_architecture_mentions_shared_log_grouping_and_direct_provider_review_fallback():
    arch = _read("docs/ARCHITECTURE.md")

    assert "log_events.js" in arch
    assert "live task card" in arch
    assert "grouped task cards" in arch
    # Direct-provider fallback covers official OpenAI, Anthropic, and Cloud.ru,
    # while still excluding OpenRouter/OpenAI-compatible/mixed-provider configs.
    # Keep the generalized name ("Direct-provider review fallback") and a
    # reference to the legacy "OpenAI-only review fallback" phrase for
    # discoverability, and pin the honest scope language so the doc cannot
    # silently re-expand to claim symmetric coverage it does not have yet.
    assert "Direct-provider review fallback" in arch
    assert "OpenAI-only review fallback" in arch  # legacy name still referenced for discoverability
    assert "official OpenAI, Anthropic, and Cloud.ru" in arch
    assert "_exclusive_direct_remote_provider_env" in arch
    # v4.34.0: direct-provider fallback now documents the
    # `main_model.startswith(provider_prefix)` guard in get_review_models —
    # previously absent, allowing OpenAI/Anthropic-only setups with a
    # cross-provider free-text main model to silently miss the fallback.
    assert "migrate_model_value" in arch
    assert "already start with the exclusive provider prefix" in arch
    # v4.34.0: Claude Runtime Status doc widened to cover both backend and
    # browser-side `catch` block paths that set `claudeRuntimeHasError`.
    assert "refreshClaudeCodeStatus" in arch
    assert "transport failure" in arch


def test_consciousness_prompt_matches_scope_limited_contracts():
    consciousness = _read("prompts/CONSCIOUSNESS.md")

    assert "schedule subagents" in consciousness
    assert "wait on subagents" in consciousness
    assert "Update your scratchpad or identity" in consciousness
    assert "Message the user proactively" in consciousness
    assert "recent_tasks" in consciousness
