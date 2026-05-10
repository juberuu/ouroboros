import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_settings_secrets_are_generic_and_integrations_tab_removed():
    ui = (ROOT / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    settings = (ROOT / "web" / "modules" / "settings.js").read_text(encoding="utf-8")
    assert "Integrations" not in ui
    assert "TELEGRAM_" not in ui
    assert "TELEGRAM_" not in settings
    assert "skill-requested-secrets" in ui
    assert "custom-secrets-list" in ui
    assert "Source Control" in ui


def test_skills_and_widgets_use_inner_scroll_regions():
    skills = (ROOT / "web" / "modules" / "skills.js").read_text(encoding="utf-8")
    widgets = (ROOT / "web" / "modules" / "widgets.js").read_text(encoding="utf-8")
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert 'class="skills-scroll scroll-fade-y"' in skills
    assert 'class="widgets-scroll scroll-fade-y"' in widgets
    assert ".skills-scroll" in css and "overflow-y: auto" in css
    assert ".widgets-scroll" in css and "overflow-y: auto" in css
