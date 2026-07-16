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
    # Direct-provider fallback covers official OpenAI, Anthropic, Cloud.ru, and GigaChat,
    # while still excluding OpenRouter/OpenAI-compatible/mixed-provider configs.
    # Keep the generalized name ("Direct-provider review fallback") and a
    # reference to the legacy "OpenAI-only review fallback" phrase for
    # discoverability, and pin the honest scope language so the doc cannot
    # silently re-expand to claim symmetric coverage it does not have yet.
    assert "Direct-provider review fallback" in arch
    assert "OpenAI-only review fallback" in arch  # legacy name still referenced for discoverability
    assert "official OpenAI, Anthropic, Cloud.ru, and GigaChat" in arch
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


def test_architecture_documents_skill_schedule_lifecycle_and_evolution_light_block():
    arch = _read("docs/ARCHITECTURE.md")

    # v6.9 RC2: skill schedule readiness SSOT, lifecycle resync, tombstone
    # retention, DST contract, and the evolution light-mode hard block.
    assert "resync_skill_schedules()" in arch
    assert "skill_readiness_for_execution()" in arch
    assert "DST-aware system" in arch
    assert "hard-blocked in `light` runtime mode" in arch
    # Experience Review memory write-back data flow is documented.
    assert "MEMORY_ACTIONS_JSON" in arch
    assert "apply_memory_actions" in arch
    assert "never auto-written to `identity.md`" in arch


def test_consciousness_prompt_matches_scope_limited_contracts():
    consciousness = _read("prompts/CONSCIOUSNESS.md")

    assert "schedule subagents" in consciousness
    assert "wait on subagents" in consciousness
    assert "Update your scratchpad or identity" in consciousness
    assert "Message the user proactively" in consciousness
    assert "recent_tasks" in consciousness


def test_phase3_governance_language_is_pinned_without_new_qa_surface():
    bible = _read("BIBLE.md")
    development = _read("docs/DEVELOPMENT.md")
    system = _read("prompts/SYSTEM.md")
    authoring = _read("docs/CREATING_SKILLS.md")
    architecture = _read("docs/ARCHITECTURE.md")
    checklists = _read("docs/CHECKLISTS.md")
    development_flat = " ".join(development.split())

    assert (
        "Uncertainty calls for judgment, not permission: within its legitimate "
        "authority, Ouroboros decides autonomously."
    ) in bible
    assert (
        "Structural depth is not scope breadth: choose the smallest change that "
        "eliminates the proven failure class."
    ) in bible

    for principle in (
        "Single Responsibility Principle",
        "Open/Closed Principle",
        "Liskov Substitution Principle",
        "Interface Segregation Principle",
        "Dependency Inversion Principle",
    ):
        assert principle in development
    assert "DI container" in development
    assert "AST analyzer" in development
    assert "Diff size, line count, and file count alone are not findings" in development

    assert "Mutable external-fact inventory" in development
    for column in (
        "Location",
        "Fact",
        "Mutability",
        "Current authority",
        "Live/probe option",
        "Risk",
        "Recommendation",
    ):
        assert f"| {column} " in development
    assert "does not migrate their runtime representations" in development_flat

    for text in (development, system, authoring, architecture, checklists):
        flat = " ".join(text.split())
        assert "real consumer flow" in flat
        assert "screenshot" in flat.lower()
        assert "vision" in flat.lower()
        assert "not a universal" in flat or "not universal" in flat or "no universal" in flat
    assert "No visual-QA runner, endpoint, ledger" in " ".join(architecture.split())


def test_phase3_widget_authoring_docs_match_recursive_schema_v1():
    development = _read("docs/DEVELOPMENT.md")
    authoring = _read("docs/CREATING_SKILLS.md")
    architecture = _read("docs/ARCHITECTURE.md")
    checklists = _read("docs/CHECKLISTS.md")
    authoring_flat = " ".join(authoring.split())

    for text in (development, authoring, architecture, checklists):
        for component in ("group", "metric", "callout"):
            assert component in text
    assert "maximum depth of 8" in authoring
    assert "256 nodes" in authoring
    assert "stable tree path" in authoring
    assert "transitively passive" in authoring_flat
    assert "dynamic_ui_schema" in authoring
