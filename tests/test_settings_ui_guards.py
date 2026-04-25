import os
import pathlib
import sys
import unittest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

REPO = pathlib.Path(__file__).resolve().parents[1]


class TestSettingsUiGuards(unittest.TestCase):
    def _read_settings_sources(self):
        return {
            "settings": (REPO / "web/modules/settings.js").read_text(encoding="utf-8"),
            "settings_ui": (REPO / "web/modules/settings_ui.js").read_text(encoding="utf-8"),
            "settings_controls": (REPO / "web/modules/settings_controls.js").read_text(encoding="utf-8"),
            "settings_catalog": (REPO / "web/modules/settings_catalog.js").read_text(encoding="utf-8"),
        }

    def test_save_checks_http_status(self):
        source = self._read_settings_sources()["settings"]
        self.assertIn("if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);", source)

    def test_save_does_not_overwrite_masked_secrets(self):
        source = self._read_settings_sources()["settings"]
        self.assertIn("function collectSecretValue(id, body) {", source)
        self.assertIn("if (input.dataset.forceClear === '1') {", source)
        self.assertIn("if (value && !value.includes('...')) body[settingKey] = value;", source)

    def test_masked_secret_inputs_clear_on_focus(self):
        source = self._read_settings_sources()["settings_ui"]
        self.assertIn("if (input.value.includes('...')) input.value = '';", source)
        self.assertIn("target.dataset.forceClear = '1';", source)

    def test_models_section_explains_local_switching(self):
        source = self._read_settings_sources()["settings_ui"]
        self.assertIn("These fields are cloud model IDs.", source)
        self.assertIn("through the GGUF server configured in Advanced.", source)

    def test_strange_settings_have_inline_explainer_copy(self):
        source = self._read_settings_sources()["settings_ui"]
        self.assertIn("Adds a password wall only for non-localhost app and API access.", source)
        self.assertIn("keeps review visible but non-blocking", source)
        self.assertIn("Backward-compatibility escape hatch for older installs.", source)

    def test_settings_expose_websearch_model(self):
        source = self._read_settings_sources()["settings_ui"]
        self.assertIn("Web Search Model", source)

    def test_budget_fields_live_in_costs_not_settings(self):
        settings_ui = self._read_settings_sources()["settings_ui"]
        costs_js = (REPO / "web/modules/costs.js").read_text(encoding="utf-8")
        # Budget inputs must be in costs.js
        self.assertIn('id="s-budget"', costs_js)
        self.assertIn('id="s-per-task-cost"', costs_js)
        # And not duplicated in settings_ui.js
        self.assertNotIn('id="s-budget"', settings_ui)
        self.assertNotIn('id="s-per-task-cost"', settings_ui)

    def test_settings_tabs_are_single_row_scrollable(self):
        css = (REPO / "web/settings.css").read_text(encoding="utf-8")
        self.assertIn("flex-wrap: nowrap;", css)
        self.assertIn("overflow-x: auto;", css)

    def test_runtime_tab_is_merged_into_advanced(self):
        source = self._read_settings_sources()["settings_ui"]
        self.assertNotIn('data-settings-tab="runtime"', source)
        self.assertIn('data-settings-tab="advanced"', source)

    def test_behavior_tab_exists_and_contains_effort_and_enforcement(self):
        source = self._read_settings_sources()["settings_ui"]
        self.assertIn('data-settings-tab="behavior"', source)
        self.assertIn('data-settings-panel="behavior"', source)
        # Reasoning Effort and Review Enforcement live in Behavior.
        behavior_section = source.split('data-settings-panel="behavior"')[1].split('data-settings-panel=')[0]
        self.assertIn("id: 's-effort-task'", behavior_section)
        self.assertIn('id="s-review-enforcement"', behavior_section)
        # enforcement uses a hidden input + segmented buttons, not a <select>
        self.assertNotIn('<select id="s-review-enforcement"', behavior_section)
        self.assertIn('data-enforcement-group', behavior_section)
        self.assertIn('data-effort-value="advisory"', behavior_section)
        self.assertIn('data-effort-value="blocking"', behavior_section)

    def test_review_models_are_in_models_tab(self):
        source = self._read_settings_sources()["settings_ui"]
        models_section = source.split('data-settings-panel="models"')[1].split('data-settings-panel=')[0]
        self.assertIn('id="s-review-models"', models_section)
        self.assertIn('id="s-scope-review-model"', models_section)
        self.assertIn('id="s-websearch-model"', models_section)

    def test_legacy_base_url_is_in_providers_not_advanced(self):
        source = self._read_settings_sources()["settings_ui"]
        providers_section = source.split('data-settings-panel="providers"')[1].split('data-settings-panel=')[0]
        advanced_section = source.split('data-settings-panel="advanced"')[1].split('data-settings-panel=')[0]
        self.assertIn('id="s-openai-base-url"', providers_section)
        self.assertNotIn('id="s-openai-base-url"', advanced_section)

    def test_save_reloads_settings_after_success(self):
        source = self._read_settings_sources()["settings"]
        self.assertIn("await loadSettings();", source)

    def test_model_picker_uses_single_custom_dropdown(self):
        sources = self._read_settings_sources()
        self.assertNotIn('list="settings-model-catalog"', sources["settings_ui"])
        self.assertNotIn('<datalist id="settings-model-catalog">', sources["settings_ui"])
        self.assertIn('autocomplete="off"', sources["settings_ui"])
        self.assertIn('spellcheck="false"', sources["settings_ui"])
        self.assertIn("closeAll();", sources["settings_controls"])
        self.assertIn("closeAll(picker);", sources["settings_controls"])
        self.assertIn("broadcastCatalog(items);", sources["settings_catalog"])

    def test_model_picker_selection_closes_without_reopening_from_synthetic_input(self):
        source = self._read_settings_sources()["settings_controls"]
        selection_handler = source.split("panel.addEventListener('mousedown'")[1].split("});", 1)[0]
        self.assertIn("closePicker(picker);", selection_handler)
        self.assertIn("new Event('change'", selection_handler)
        self.assertNotIn("new Event('input'", selection_handler)

    def test_settings_tracks_unsaved_changes_without_navigation_guard(self):
        sources = self._read_settings_sources()
        self.assertIn('id="settings-unsaved-indicator"', sources["settings_ui"])
        self.assertIn(".settings-unsaved-indicator", (REPO / "web/settings.css").read_text(encoding="utf-8"))
        self.assertIn("let settingsBaseline = '';", sources["settings"])
        self.assertIn("function updateSettingsDirtyState()", sources["settings"])
        self.assertIn("indicator.hidden = !settingsDirty;", sources["settings"])
        self.assertIn("page.addEventListener('input', updateSettingsDirtyState);", sources["settings"])
        self.assertIn("page.addEventListener('change', updateSettingsDirtyState);", sources["settings"])
        self.assertIn("[data-effort-value], .secret-clear", sources["settings"])

    def test_model_catalog_refresh_has_browser_timeout(self):
        source = self._read_settings_sources()["settings_catalog"]
        self.assertIn("MODEL_CATALOG_TIMEOUT_MS", source)
        self.assertIn("new AbortController()", source)
        self.assertIn("signal: controller.signal", source)
        self.assertIn("AbortError", source)

    def test_model_catalog_refresh_ignores_stale_responses(self):
        source = self._read_settings_sources()["settings_catalog"]
        self.assertIn("let catalogRefreshSeq = 0;", source)
        self.assertIn("const refreshSeq = ++catalogRefreshSeq;", source)
        self.assertIn("refreshSeq !== catalogRefreshSeq", source)
        self.assertIn("stale: true", source)
