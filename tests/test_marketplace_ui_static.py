"""Static contract checks for the ClawHub Marketplace UI module."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _marketplace_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "marketplace.js").read_text(
        encoding="utf-8"
    )


def _skills_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "skills.js").read_text(
        encoding="utf-8"
    )


def test_marketplace_search_mode_hides_pagination_and_keeps_official_clickable():
    source = _marketplace_js()
    assert "const searchMode = Boolean(String(query || '').trim());" in source
    assert "if (searchMode || (!nextCursor && !hasPrevious))" in source
    assert "onlyOfficial.disabled = searchMode;" not in source
    assert "Filters enriched search results to skills marked official." in source


def test_marketplace_search_request_drops_browse_only_params():
    source = _marketplace_js()
    assert "const MARKETPLACE_SEARCH_LIMIT = 16;" in source
    assert "String(query ? MARKETPLACE_SEARCH_LIMIT : state.limit)" in source
    assert "if (!query && state.cursor) params.set('cursor', state.cursor);" in source
    assert "if (state.onlyOfficial) params.set('official', '1');" in source
    assert "params.set('offset'" not in source


def test_marketplace_browse_tracks_cursor_history_for_prev():
    source = _marketplace_js()
    assert "cursorHistory: []" in source
    assert "state.cursorHistory.push(state.cursor || '');" in source
    assert "state.cursor = state.cursorHistory.pop() || '';" in source
    assert "hasPrevious: state.cursorHistory.length > 0" in source


def test_marketplace_empty_and_timeout_copy_is_human_readable():
    source = _marketplace_js()
    assert "No installable${officialText} skills found ${mode}." in source
    assert "ClawHub did not respond in time. Try again" in source
    assert "packages/search?family=skill" not in source


def test_marketplace_review_failure_points_to_heal_flow():
    source = _marketplace_js()
    assert "Fix" in source
    assert "Ask Ouroboros in chat to repair" in source
    assert "AUTO-REVIEW FAILED" not in source
    assert "rerun review from the Skills tab" not in source


def test_marketplace_cards_have_lifecycle_next_action():
    source = _marketplace_js()
    assert "function lifecycleFor" in source
    assert "marketplace-next-action" in source
    assert "data-mp-action" in source
    assert "state.pendingBySlug" in source


def test_marketplace_fix_prompt_has_heal_payload_root_marker():
    source = _marketplace_js()
    assert "HEAL_SKILL_PAYLOAD_ROOT_JSON" in source
    assert "diagnostics.payload_root" in source
    assert "Final non-negotiable rules:" in source
    assert ".replace(/`/g, \"'\")" in source


def test_marketplace_install_does_not_silently_enable():
    source = _marketplace_js()
    assert "review passed. Enable it from the card when ready." in source
    assert "Installed and enabled" not in source
    assert "toggleInstalledSkill(installedNow, true)" not in source


def test_installed_skills_keep_review_before_fix_for_pending_or_stale():
    source = _skills_js()
    next_action = source.split("function skillNextAction", 1)[1].split("function renderSkillCard", 1)[0]
    assert "if (!reviewReady(skill))" in next_action
    assert "skill.review_status === 'fail'" in next_action


def test_marketplace_pending_or_stale_lifecycle_uses_review_not_fix():
    source = _marketplace_js()
    lifecycle = source.split("function lifecycleFor", 1)[1].split("function buildHealPrompt", 1)[0]
    assert "installed.review_status === 'fail'" in lifecycle
    assert "action: 'review'" in lifecycle
    assert "button: installed.review_stale ? 'Re-review' : 'Review'" in lifecycle
