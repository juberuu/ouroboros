"""Static contract checks for the ClawHub Marketplace UI module."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _marketplace_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "marketplace.js").read_text(
        encoding="utf-8"
    )


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _skills_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "skills.js").read_text(
        encoding="utf-8"
    )


def _ouroboroshub_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "ouroboroshub.js").read_text(
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
    assert "registry_warnings" in source
    assert "packages/search?family=skill" not in source


def test_marketplace_review_failure_points_to_heal_flow():
    source = _marketplace_js()
    assert "Repair" in source
    assert "Start a repair task" in source
    assert "visible_text:" in source
    assert "AUTO-REVIEW FAILED" not in source
    assert "rerun review from the Skills tab" not in source


def test_marketplace_cards_have_lifecycle_next_action():
    source = _marketplace_js()
    lifecycle_card = _read("web/modules/lifecycle_card.js")
    css = _read("web/style.css")
    assert "function lifecycleFor" in source
    assert "marketplace-next-action" in source
    assert "marketplace-working-spinner" in lifecycle_card
    assert ".marketplace-working-spinner" in css
    assert "data-mp-action" in source
    assert "getPendingBySlug" in source
    assert "./lifecycle_card.js" in source
    assert "import { openConfirmDialog } from './confirm_dialog.js';" in source


def test_ouroboroshub_cards_share_lifecycle_pending_ui():
    source = _ouroboroshub_js()
    lifecycle_card = _read("web/modules/lifecycle_card.js")

    assert "./lifecycle_card.js" in source
    assert "setPending" in source
    assert "startLifecyclePoller" in source
    assert "marketplace-working-spinner" in lifecycle_card
    assert "marketplace-card-state-hint" in source
    assert "marketplace-card is-working" not in source
    assert "lifecycleCardClassFor(pending)" in source
    assert "if (!data.ok) throw new Error(data.error || 'install failed')" in source


def test_shared_confirm_dialog_module_replaces_native_marketplace_confirms():
    source = _marketplace_js()
    dialog = _read("web/modules/confirm_dialog.js")
    css = _read("web/style.css")

    assert "openConfirmDialog({" in source
    assert "confirm(" not in source
    assert "export function openConfirmDialog" in dialog
    assert "let activeClose" in dialog
    assert "document.removeEventListener('keydown', onKey);" in dialog
    assert "if (activeClose) activeClose(false);" in dialog
    assert ".confirm-dialog" in css


def test_marketplace_fix_prompt_has_heal_payload_root_marker():
    source = _marketplace_js()
    assert "HEAL_SKILL_PAYLOAD_ROOT_JSON" in source
    assert "diagnostics.payload_root" in source
    assert "Final non-negotiable rules:" in source
    assert ".replace(/`/g, \"'\")" in source
    assert "Start a repair task" in source
    assert "visible_text:" in source
    assert "data-page=\"chat\"" in source


def test_marketplace_install_does_not_silently_enable():
    source = _marketplace_js()
    assert "review passed. Enable it from the card when ready." in source
    assert "Installed and enabled" not in source
    assert "toggleInstalledSkill(installedNow, true)" not in source


def test_installed_skills_keep_review_before_fix_for_pending_or_stale():
    source = _skills_js()
    next_action = source.split("function skillNextAction", 1)[1].split("function renderSkillCard", 1)[0]
    toggle_lock = source.split("function toggleLockReason", 1)[1].split("function skillNextAction", 1)[0]
    assert "skill.review_status === 'fail'" in next_action
    assert "review is stale — re-review the skill first" in toggle_lock
    assert "review is still pending" in toggle_lock
    assert "review has not passed yet" in toggle_lock
    assert "Run review and wait for a fresh PASS before enabling this skill." in source
    assert "needs a fresh security review before it can be enabled" not in source


def test_marketplace_pending_or_stale_lifecycle_uses_review_not_fix():
    source = _marketplace_js()
    lifecycle = source.split("function lifecycleFor", 1)[1].split("function buildHealPrompt", 1)[0]
    assert "installed.review_status === 'fail'" in lifecycle
    assert "action: 'review'" in lifecycle
    assert "button: installed.review_stale ? 'Re-review' : 'Review'" in lifecycle


def test_marketplace_update_uses_shared_pending_lifecycle_card():
    source = _marketplace_js()
    update_block = source.split("if (updateBtn)", 1)[1].split("if (uninstallBtn)", 1)[0]
    poller = _read("web/modules/lifecycle_card.js")

    assert "setPending(slug, {" in update_block
    assert "label: 'Updating'" in update_block
    assert "target: sanitized" in update_block
    assert "throw new Error(result.error || 'update failed')" in update_block
    assert "retry_action: 'update'" in update_block
    assert "targets.has(e?.target)" in poller
