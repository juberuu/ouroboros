"""Static checks for Dashboard-hosted observability and update panels."""

from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_nav_moves_observability_pages_into_dashboard():
    html = _read("web/index.html")
    settings_ui = _read("web/modules/settings_ui.js")
    dashboard = _read("web/modules/dashboard.js")
    app = _read("web/app.js")

    assert 'data-page="logs"' not in html
    assert 'data-page="costs"' not in html
    assert 'data-page="evolution"' not in html
    assert 'data-page="dashboard"' in html
    for tab in ("logs", "evolution", "updates", "costs"):
        assert f'data-settings-tab="{tab}"' not in settings_ui
        assert f'data-dashboard-tab="{tab}"' in dashboard
        assert f'data-dashboard-panel="{tab}"' in dashboard
    assert "openDashboardTab" in app
    assert "dashboardActiveSubtab" in app


def test_settings_mobile_horizontal_pills_contract_exists():
    """v5.7.0: the v5.6.0 drill-down/back-button accordion was reverted in
    favour of horizontal-scroll pills on every viewport. The active pill
    auto-scrolls into view via scrollIntoView({inline:'center'}) so users
    on narrow phones can still reach every sub-tab in one tap. The legacy
    .settings-mobile-back element is kept in the DOM hidden for back-compat
    but is no longer functionally wired."""
    settings_ui = _read("web/modules/settings_ui.js")
    settings_css = _read("web/settings.css")

    # v5.7.0 behaviour: scrollable pills + scrollIntoView for active.
    assert "scrollIntoView" in settings_ui
    assert "inline: 'center'" in settings_ui
    # Mobile media query keeps pills horizontal (overflow-x: auto on mobile).
    assert "overflow-x: auto" in settings_css
    # The legacy back-button element is hidden on every viewport now.
    assert ".settings-mobile-back" in settings_css
    # The drill-down toggle class may still appear in comments documenting
    # the v5.6.0 rollback, but no runtime class mutation may remain.
    assert "root.classList.add('settings-subtab-open')" not in settings_ui
    assert "root.classList.remove('settings-subtab-open')" not in settings_ui


def test_update_panel_contract_exists():
    """Trimmed in v5.15.x — the long literal chain (30+ substrings spanning
    JS / server.py / git_ops.py / ARCHITECTURE.md) was high-maintenance and
    duplicated coverage with the behavioral test
    test_update_apply_consumes_intent_before_restart below. This test now
    only pins the entry points + the official-remote URL constant; broader
    update-intent semantics live in the behavioral test."""
    updates = _read("web/modules/updates.js")
    server = _read("server.py")
    git_ops = _read("supervisor/git_ops.py")

    assert "export function initUpdates" in updates
    for endpoint in ("/api/update/status", "/api/update/check", "/api/update/apply"):
        assert endpoint in updates
    for handler in ("api_update_status", "api_update_check", "api_update_apply"):
        assert handler in server
    assert 'OFFICIAL_UPDATE_REMOTE_URL = "https://github.com/joi-lab/ouroboros-desktop"' in git_ops


def test_update_panel_surfaces_unmanaged_checkouts_as_unavailable():
    updates = _read("web/modules/updates.js")

    assert "Managed updates are unavailable for this checkout." in updates
    assert "managed_updates_unavailable" in updates
    assert "applyBtn.textContent = 'Unavailable'" in updates


def test_update_panel_mobile_headline_does_not_squeeze_summary():
    updates = _read("web/modules/updates.js")
    css = _read("web/style.css")

    assert "updates-card-head-main" in updates
    assert ".updates-card-head-main" in css
    assert "min-width: 0;" in css
    assert ".updates-card-head" in css
    assert "flex-direction: column;" in css


def test_update_apply_consumes_intent_before_restart():
    server = _read("server.py")
    git_ops = _read("supervisor/git_ops.py")
    apply_block = server[server.index("async def api_update_apply"):server.index("\n\n_evo_cache", server.index("async def api_update_apply"))]

    assert "checkout_and_reset(" in apply_block
    assert 'reason="ui_update_apply"' in apply_block
    assert "_clear_update_intent()" in apply_block
    assert "except Exception as checkout_exc:" in apply_block
    assert apply_block.index("except Exception as checkout_exc:") < apply_block.index("_request_restart_exit()")
    assert 'str(reason or "") != "ui_update_apply"' in git_ops
    assert "_request_restart_exit()" in apply_block


def test_button_design_system_contract_exists():
    css = _read("web/style.css")
    dev = _read("docs/DEVELOPMENT.md")
    marketplace = _read("web/modules/marketplace.js")

    btn_block = css[css.index(".btn {"):css.index(".btn-primary {")]
    assert "justify-content: center" in btn_block
    assert ".btn-secondary" in css
    assert ".btn-ghost" in css
    assert ".btn-lg" in css
    assert ".btn-default.btn-primary" not in marketplace
    assert "### Button conventions" in dev
