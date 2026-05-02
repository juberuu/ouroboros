"""Static checks for the Skills smart-toggle activation flow."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_skill_toggle_runs_review_and_grant_before_enable():
    source = _read("web/modules/skills.js")

    assert "async function requestMissingKeyGrants" in source
    assert "async function reviewSkillInBackground" in source
    assert "async function toggleSkillEnabled" in source
    assert "needs a fresh security review before it can be enabled" in source
    assert "await reviewSkillInBackground(name);" in source
    assert "await requestMissingKeyGrants(name, missing);" in source
    assert "await toggleSkillEnabled(name, wantsEnabled);" in source


def test_skill_cards_keep_toggle_but_move_secondary_actions_to_menu():
    source = _read("web/modules/skills.js")
    css = _read("web/style.css")

    assert "class=\"skills-switch" in source
    assert "skills-card-menu" in source
    assert "data-skill-menu-trigger" in source
    assert "skills-menu-item skills-update" in source
    assert "skills-menu-item skills-uninstall" in source
    assert "skills-card-menu-dialog" in source
    assert "showModal()" in source
    assert "event.target.classList?.contains('skills-card-menu-dialog')" in source
    assert ".skills-card-menu-dialog" in css
    assert "::backdrop" in css
    assert ".skills-menu-item" in css
    assert ">Heal<" not in source
