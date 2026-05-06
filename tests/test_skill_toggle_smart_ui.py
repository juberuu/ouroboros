"""Static checks for the Skills smart-toggle activation flow."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_skill_toggle_requires_fresh_pass_before_enable():
    source = _read("web/modules/skills.js")

    assert "async function requestMissingKeyGrants" in source
    assert "async function toggleSkillEnabled" in source
    assert "review is stale — re-review the skill first" in source
    assert "review is still pending" in source
    assert "review has not passed yet" in source
    assert "Run review and wait for a fresh PASS before enabling this skill." in source
    assert "needs a fresh security review before it can be enabled" not in source
    assert "Review did not pass. Use Repair if the skill needs repair." not in source
    assert "await requestMissingKeyGrants(name, missing);" in source
    assert "await toggleSkillEnabled(name, wantsEnabled);" in source


def test_skill_card_primary_actions_and_lock_surfaces_use_shared_modal():
    source = _read("web/modules/skills.js")
    css = _read("web/style.css")
    dialog = _read("web/modules/confirm_dialog.js")

    assert "import { openConfirmDialog } from './confirm_dialog.js';" in source
    assert "function getSkillPrimaryAction" in source
    assert "skills-primary-action" in source
    assert "data-skill-action" in source
    assert "skills-status-chip" in source and "role=\"button\"" in source
    assert "skills-lock-hint" in source and "role=\"button\"" in source
    assert "event.target.closest('[data-skill-action]')" in source
    assert "triggerSkillAction(name, action" in source
    assert "openConfirmDialog({" in source
    assert ".skills-primary-action" in css
    assert "export function openConfirmDialog" in dialog

    primary_action = source.split("function getSkillPrimaryAction", 1)[1].split("function renderSkillCard", 1)[0]
    assert primary_action.index("skill.review_status === 'fail'") < primary_action.index("!reviewReady(skill)")
    assert primary_action.index("!reviewReady(skill)") < primary_action.index("!grantReady(skill)")


def test_skill_cards_keep_toggle_but_move_secondary_actions_to_menu():
    source = _read("web/modules/skills.js")
    css = _read("web/style.css")

    assert "class=\"skills-switch" in source
    assert "skills-card-menu" in source
    assert "data-skill-menu-trigger" in source
    assert "skills-menu-item skills-update" in source
    assert "skills-menu-item skills-uninstall" in source
    assert "skills-menu-item skills-review" in source
    assert "skill.review_status === 'pending' ? 'Review'" in source
    assert "skills-card-menu-dialog" in source
    # v5.7.0: secondary actions moved to the card header kebab and open as
    # anchored non-modal popovers. Modal dialogs/backdrops caused the menu to
    # appear detached from the skill card and dim the whole page.
    assert "if (opening) popover.show();" in source
    assert "if (opening) popover.showModal();" not in source
    assert "event.target.closest('[data-skill-menu-close]')" in source
    assert ".skills-card-menu-dialog" in css
    assert ".skills-card-menu-dialog::backdrop" not in css
    assert "top: calc(100% + 6px)" in css
    assert "right: 0" in css
    assert ".skills-menu-item" in css
    assert ">Heal<" not in source
