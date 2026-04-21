"""Guardrails for README and architecture docs after UI/routing overhaul."""

import os
import pathlib

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_readme_mentions_multistep_wizard_and_live_task_ui():
    readme = _read("README.md")

    assert "shared desktop/web wizard is now multi-step" in readme
    assert "add access first, choose visible models second, set review mode third, set budget fourth" in readme
    assert "Focused Task UX" in readme
    assert "live task card" in readme


def test_architecture_mentions_shared_log_grouping_and_direct_provider_review_fallback():
    arch = _read("docs/ARCHITECTURE.md")

    assert "log_events.js" in arch
    assert "live task card" in arch
    assert "grouped task cards" in arch
    # Post-v4.33.1: the review fallback currently applies only to OpenAI-only
    # and Anthropic-only setups — `_exclusive_direct_remote_provider_env`
    # early-returns "" when OpenAI-compatible or Cloud.ru keys are present.
    # Keep the generalized name ("Direct-provider review fallback") and a
    # reference to the legacy "OpenAI-only review fallback" phrase for
    # discoverability, and pin the honest scope language so the doc cannot
    # silently re-expand to claim symmetric coverage it does not have yet.
    assert "Direct-provider review fallback" in arch
    assert "OpenAI-only review fallback" in arch  # legacy name still referenced for discoverability
    assert "Current scope is OpenAI-only and Anthropic-only" in arch
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
