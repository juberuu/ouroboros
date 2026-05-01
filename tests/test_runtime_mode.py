"""Phase 2 regression tests: Runtime Mode + Skills Repo Path plumbing.

Covers:

- ``ouroboros.config.SETTINGS_DEFAULTS`` declares both new keys with
  documented defaults.
- ``get_runtime_mode`` / ``get_skills_repo_path`` helpers clamp, normalize,
  and expand ``~`` correctly.
- ``apply_settings_to_env`` pushes both new keys into the environment.
- ``prepare_onboarding_settings`` accepts the new fields, validates the
  runtime-mode set, and defaults safely when missing.
- ``server.py::api_state`` emits ``runtime_mode`` + ``skills_repo_configured``
  and the keys are declared in ``ouroboros.contracts.api_v1.StateResponse``.
- ``web/modules/settings_ui.js`` renders the new controls.
- ``web/modules/settings.js`` round-trips both keys.
- ``web/modules/onboarding_wizard.js`` ships the runtime-mode selector +
  includes the new fields in the save payload.

These tests are source-level (AST/substring) to stay hermetic — no
network, no supervisor boot.
"""
from __future__ import annotations

import ast
import os
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# config.py: defaults + helpers + env propagation
# ---------------------------------------------------------------------------


def test_settings_defaults_include_phase2_keys():
    from ouroboros.config import SETTINGS_DEFAULTS

    assert SETTINGS_DEFAULTS["OUROBOROS_RUNTIME_MODE"] == "advanced"
    assert SETTINGS_DEFAULTS["OUROBOROS_SKILLS_REPO_PATH"] == ""


def test_valid_runtime_modes_is_frozen_tuple():
    from ouroboros.config import VALID_RUNTIME_MODES

    assert VALID_RUNTIME_MODES == ("light", "advanced", "pro")


def test_get_runtime_mode_accepts_all_three(monkeypatch):
    from ouroboros.config import get_runtime_mode

    for mode in ("light", "advanced", "pro"):
        monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
        assert get_runtime_mode() == mode


def test_get_runtime_mode_clamps_unknown_value(monkeypatch):
    """An unknown value must degrade to the default, not leak through."""
    from ouroboros.config import get_runtime_mode

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "ULTRA")
    assert get_runtime_mode() == "advanced"


def test_get_runtime_mode_is_case_insensitive(monkeypatch):
    from ouroboros.config import get_runtime_mode

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "Pro")
    assert get_runtime_mode() == "pro"


def test_get_runtime_mode_defaults_when_unset(monkeypatch):
    from ouroboros.config import get_runtime_mode

    monkeypatch.delenv("OUROBOROS_RUNTIME_MODE", raising=False)
    assert get_runtime_mode() == "advanced"


def test_get_skills_repo_path_defaults_to_empty(monkeypatch):
    from ouroboros.config import get_skills_repo_path

    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)
    assert get_skills_repo_path() == ""


def test_get_skills_repo_path_expands_home(monkeypatch):
    from ouroboros.config import get_skills_repo_path

    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", "~/Ouroboros/skills")
    expanded = get_skills_repo_path()
    assert expanded.startswith(os.path.expanduser("~"))
    assert expanded.endswith(os.path.join("Ouroboros", "skills"))


def test_apply_settings_to_env_propagates_phase2_keys(monkeypatch):
    """Settings pushes both new keys into os.environ."""
    from ouroboros.config import SETTINGS_DEFAULTS, apply_settings_to_env

    monkeypatch.delenv("OUROBOROS_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)

    settings = dict(SETTINGS_DEFAULTS)
    settings["OUROBOROS_RUNTIME_MODE"] = "light"
    settings["OUROBOROS_SKILLS_REPO_PATH"] = "/tmp/skills"

    apply_settings_to_env(settings)

    assert os.environ["OUROBOROS_RUNTIME_MODE"] == "light"
    assert os.environ["OUROBOROS_SKILLS_REPO_PATH"] == "/tmp/skills"


# ---------------------------------------------------------------------------
# onboarding_wizard.py: validation + save
# ---------------------------------------------------------------------------


def _onboarding_payload_with_runtime(mode: str | None = None, skills_path: str = ""):
    from ouroboros.config import SETTINGS_DEFAULTS

    payload = {
        "OPENROUTER_API_KEY": "sk-or-v1-" + "a" * 30,
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "TOTAL_BUDGET": 10,
        "OUROBOROS_PER_TASK_COST_USD": 20,
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        "LOCAL_MODEL_SOURCE": "",
        "LOCAL_MODEL_FILENAME": "",
        "LOCAL_MODEL_CONTEXT_LENGTH": SETTINGS_DEFAULTS["LOCAL_MODEL_CONTEXT_LENGTH"],
        "LOCAL_MODEL_N_GPU_LAYERS": -1,
        "LOCAL_MODEL_CHAT_FORMAT": "",
        "LOCAL_ROUTING_MODE": "cloud",
        "OUROBOROS_MODEL": "anthropic/claude-opus-4.6",
        "OUROBOROS_MODEL_CODE": "anthropic/claude-opus-4.6",
        "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4.6",
        "OUROBOROS_MODEL_FALLBACK": "anthropic/claude-sonnet-4.6",
        "OUROBOROS_SKILLS_REPO_PATH": skills_path,
    }
    if mode is not None:
        payload["OUROBOROS_RUNTIME_MODE"] = mode
    return payload


def test_prepare_onboarding_settings_defaults_runtime_mode_when_missing():
    """Legacy onboarding payloads without the new field must still save cleanly."""
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode=None)
    prepared, error = prepare_onboarding_settings(payload, {})
    assert error is None, error
    assert prepared["OUROBOROS_RUNTIME_MODE"] == "advanced"
    # Empty skills path survives round-trip as an empty string.
    assert prepared["OUROBOROS_SKILLS_REPO_PATH"] == ""


@pytest.mark.parametrize("mode", ["light", "advanced", "pro"])
def test_prepare_onboarding_settings_accepts_each_runtime_mode(mode):
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode=mode)
    prepared, error = prepare_onboarding_settings(payload, {})
    assert error is None, error
    assert prepared["OUROBOROS_RUNTIME_MODE"] == mode


def test_prepare_onboarding_settings_rejects_unknown_runtime_mode():
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode="turbo")
    prepared, error = prepare_onboarding_settings(payload, {})
    assert prepared == {}
    assert error is not None
    assert "runtime mode" in error.lower()


def test_prepare_onboarding_settings_persists_skills_repo_path():
    from ouroboros.onboarding_wizard import prepare_onboarding_settings

    payload = _onboarding_payload_with_runtime(mode="advanced", skills_path="~/skills-dev")
    prepared, error = prepare_onboarding_settings(payload, {})
    assert error is None, error
    # The onboarding layer stores the value verbatim — ``get_skills_repo_path``
    # is what expands ``~`` at read time.
    assert prepared["OUROBOROS_SKILLS_REPO_PATH"] == "~/skills-dev"


def test_onboarding_bootstrap_exposes_runtime_mode():
    """The JS bootstrap payload includes runtimeMode + skillsRepoPath keys."""
    from ouroboros.onboarding_wizard import build_onboarding_html

    html = build_onboarding_html(
        {"OUROBOROS_RUNTIME_MODE": "pro", "OUROBOROS_SKILLS_REPO_PATH": "/opt/skills"}
    )
    assert '"runtimeMode": "pro"' in html
    assert '"skillsRepoPath": "/opt/skills"' in html


# ---------------------------------------------------------------------------
# server.py: /api/state surfaces both keys
# ---------------------------------------------------------------------------


def test_api_state_declares_phase2_keys():
    """``api_state`` JSONResponse literal must include the new keys so the
    frozen ``StateResponse`` contract passes and the UI can consume them."""
    tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
    api_state_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_state":
            api_state_fn = node
            break
    assert api_state_fn is not None

    for node in ast.walk(api_state_fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "JSONResponse"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Dict):
            continue
        keys = {
            k.value for k in node.args[0].keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
        if keys == {"error"}:
            continue
        assert "runtime_mode" in keys, "api_state happy path must emit runtime_mode"
        assert "skills_repo_configured" in keys, (
            "api_state happy path must emit skills_repo_configured"
        )
        return
    raise AssertionError("api_state exposes no happy-path JSONResponse literal")


def test_state_response_typeddict_declares_phase2_keys():
    from ouroboros.contracts.api_v1 import StateResponse

    keys = set(StateResponse.__annotations__.keys())
    assert "runtime_mode" in keys
    assert "skills_repo_configured" in keys


# ---------------------------------------------------------------------------
# Web UI surfaces
# ---------------------------------------------------------------------------


def test_settings_ui_renders_runtime_mode_and_skills_path():
    src = (REPO / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    assert 'id="s-runtime-mode"' in src
    assert 'data-runtime-mode-group' in src
    for mode in ("light", "advanced", "pro"):
        assert f'data-effort-value="{mode}"' in src
    assert 'id="s-skills-repo-path"' in src


def test_settings_js_reads_and_writes_phase2_keys():
    src = (REPO / "web" / "modules" / "settings.js").read_text(encoding="utf-8")
    assert "OUROBOROS_RUNTIME_MODE" in src
    assert "OUROBOROS_SKILLS_REPO_PATH" in src
    # v5.1.2 Frame A: the load path still hydrates the segmented control
    # so the UI displays the current owner-set runtime_mode. The save
    # path no longer sends ``OUROBOROS_RUNTIME_MODE`` (mode is owner-only;
    # /api/settings drops it from the body merge), only the skills-repo
    # path round-trips through the form.
    assert "byId('s-runtime-mode').value = s.OUROBOROS_RUNTIME_MODE" in src
    assert "byId('s-skills-repo-path').value.trim()" in src


def test_onboarding_js_has_runtime_mode_selector_and_save_payload():
    src = (REPO / "web" / "modules" / "onboarding_wizard.js").read_text(encoding="utf-8")
    for mode in ("light", "advanced", "pro"):
        assert f'data-runtime-mode="{mode}"' in src
    assert "OUROBOROS_RUNTIME_MODE" in src
    assert "OUROBOROS_SKILLS_REPO_PATH" in src


def test_phase4_ui_copy_matches_shipped_runtime():
    settings_ui = (REPO / "web" / "modules" / "settings_ui.js").read_text(encoding="utf-8")
    onboarding_js = (REPO / "web" / "modules" / "onboarding_wizard.js").read_text(encoding="utf-8")

    assert "Phase 2 plumbing only" not in settings_ui
    assert "land in Phase 3" not in settings_ui
    # v4.50: skills moved to data/skills/{native,clawhub,external}/ —
    # the legacy ``repo/skills/`` reference no longer appears in the
    # settings copy.
    assert "data/skills/" in settings_ui
    assert "Pick both review enforcement and the initial runtime mode" in onboarding_js
    assert "normal triad + scope review" in onboarding_js
    assert "Phase 6+:" not in onboarding_js


def test_skills_ui_reads_live_extension_state_fields():
    src = (REPO / "web" / "modules" / "skills.js").read_text(encoding="utf-8")
    assert "live_loaded" in src
    assert "live_reason" in src
    assert "catalog only" in src
    assert "ui_tabs_pending" in src
    assert "result.error" in src


def test_onboarding_js_exposes_skills_repo_path_input_and_binding():
    """Regression for the Phase 2 round 3 finding: ``state.skillsRepoPath``
    must be configurable through an actual onboarding input, not just
    round-tripped from the bootstrap value."""
    src = (REPO / "web" / "modules" / "onboarding_wizard.js").read_text(encoding="utf-8")
    # Real input element with the expected id.
    assert 'id="skills-repo-path"' in src
    # A Clear button that wipes the state.
    assert 'data-clear="skills-repo-path"' in src
    # Input handler updates state.skillsRepoPath (no dead state).
    assert "state.skillsRepoPath = skillsInput.value" in src
    # Clear button branch wipes state.skillsRepoPath too.
    assert "target === 'skills-repo-path'" in src


def test_onboarding_css_has_three_column_variant():
    """`.wizard-choice-grid.three` must exist so the 3-button runtime-mode
    row renders as 3 columns instead of 2 + 1. Regression for a styling
    bug caught by the Phase 2 scope reviewer."""
    src = (REPO / "web" / "onboarding.css").read_text(encoding="utf-8")
    assert ".wizard-choice-grid.three" in src, (
        "web/onboarding.css lacks a .wizard-choice-grid.three rule — the "
        "runtime-mode row in the review_mode step will fall back to the "
        "default 2-column layout."
    )


# ---------------------------------------------------------------------------
# Route-level round-trip (Starlette TestClient)
# ---------------------------------------------------------------------------


def _patched_test_client():
    """Build a hermetic TestClient for the server app.

    Mirrors the patching pattern used by ``tests/test_settings_network_hint.py``
    so the live ``/api/settings`` POST/GET path can be exercised without
    booting the real supervisor, auth gate, or port-file state.
    """
    from unittest.mock import patch

    import server as srv
    from starlette.testclient import TestClient

    # ``with`` needs a single context we can return — build a small helper.
    class _Ctx:
        def __enter__(self):
            self._stack = []
            for patcher in (
                patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None),
                patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None),
                patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])),
                patch("ouroboros.server_auth.get_configured_network_password", return_value=""),
            ):
                self._stack.append(patcher.__enter__())
            self._client = TestClient(srv.app)
            return self._client

        def __exit__(self, exc_type, exc, tb):
            # unittest.mock patchers don't expose a direct stop when entered
            # via their own __enter__ returned value; closing the TestClient
            # and letting the GC release the patcher contexts is enough for
            # a single-test scope.
            self._client.close()
            return False

    return _Ctx()


def test_api_settings_post_clamps_unknown_runtime_mode(tmp_path, monkeypatch):
    """POSTing an invalid runtime mode must be normalized to 'advanced'
    before save — so /api/settings and /api/state can never disagree."""
    import server as srv
    from starlette.testclient import TestClient
    from unittest.mock import patch

    saved: dict = {}

    def fake_load_settings():
        from ouroboros.config import SETTINGS_DEFAULTS
        out = dict(SETTINGS_DEFAULTS)
        out.update(saved)
        return out

    def fake_save_settings(payload):
        saved.clear()
        saved.update(payload)

    with patch.object(srv, "load_settings", side_effect=fake_load_settings), \
            patch.object(srv, "save_settings", side_effect=fake_save_settings), \
            patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None), \
            patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None), \
            patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])), \
            patch("ouroboros.server_auth.get_configured_network_password", return_value=""):
        client = TestClient(srv.app)
        resp = client.post(
            "/api/settings",
            json={"OUROBOROS_RUNTIME_MODE": "turbo"},
        )
        assert resp.status_code == 200, resp.text
        # v5.1.2: /api/settings drops OUROBOROS_RUNTIME_MODE entirely — even
        # invalid inputs do not reach the body merge. The persisted value
        # must equal the SETTINGS_DEFAULTS baseline ("advanced") via the
        # belt-and-braces revert in api_settings_post, not via clamping.
        assert saved["OUROBOROS_RUNTIME_MODE"] == "advanced"


def test_api_settings_post_silently_drops_runtime_mode_changes():
    """v5.1.2 elevation ratchet: even a VALID runtime_mode in the body
    is silently dropped — the API never accepts mode changes."""
    import server as srv
    from starlette.testclient import TestClient
    from unittest.mock import patch

    saved: dict = {}

    def fake_load_settings():
        from ouroboros.config import SETTINGS_DEFAULTS
        out = dict(SETTINGS_DEFAULTS)
        out["OUROBOROS_RUNTIME_MODE"] = "light"  # baseline on disk
        out.update(saved)
        return out

    def fake_save_settings(payload, *, allow_elevation: bool = False):
        # Mirror real save_settings semantics so the test reflects the
        # actual chokepoint behaviour (no need to verify the elevation
        # PermissionError here — that is covered by
        # tests/test_runtime_mode_elevation.py; here we only assert the
        # API-level drop).
        saved.clear()
        saved.update(payload)

    with patch.object(srv, "load_settings", side_effect=fake_load_settings), \
            patch.object(srv, "save_settings", side_effect=fake_save_settings), \
            patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None), \
            patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None), \
            patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])), \
            patch("ouroboros.server_auth.get_configured_network_password", return_value=""):
        client = TestClient(srv.app)
        resp = client.post(
            "/api/settings",
            json={"OUROBOROS_RUNTIME_MODE": "pro", "OUROBOROS_SKILLS_REPO_PATH": "  /tmp/sk  "},
        )
        assert resp.status_code == 200, resp.text
        # Mode change is dropped: persisted value comes from on-disk old.
        assert saved["OUROBOROS_RUNTIME_MODE"] == "light"
        # Other (non-owner-only) keys still flow through.
        assert saved["OUROBOROS_SKILLS_REPO_PATH"] == "/tmp/sk"


def test_normalize_runtime_mode_clamps_unknown_inputs():
    """Direct helper test for the save-path normalizer."""
    from ouroboros.config import normalize_runtime_mode

    assert normalize_runtime_mode("light") == "light"
    assert normalize_runtime_mode("ADVANCED") == "advanced"
    assert normalize_runtime_mode("Pro") == "pro"
    assert normalize_runtime_mode("turbo") == "advanced"
    assert normalize_runtime_mode("") == "advanced"
    assert normalize_runtime_mode(None) == "advanced"
    assert normalize_runtime_mode(123) == "advanced"


def test_load_settings_clamps_legacy_invalid_runtime_mode(tmp_path, monkeypatch):
    """Read-path normalization: a pre-existing settings.json containing
    an invalid runtime mode must be clamped at load time so /api/settings
    (GET) and the onboarding bootstrap cannot echo stale invalid values.

    Regression for the Phase 2 read-path drift bug caught in review round
    2 — fix lives in ``ouroboros.config._coerce_setting_value``.

    Because ``ouroboros.config`` computes ``SETTINGS_PATH`` / ``APP_ROOT`` /
    ``REPO_DIR`` at import time, this test isolates its ``importlib.reload``
    with a ``try/finally`` that reloads the module back against the original
    environment. Without that restore, later tests in the same pytest
    session would inherit a stale ``SETTINGS_PATH`` pointing at the
    already-deleted ``tmp_path``.
    """
    import importlib
    import json

    import ouroboros.config as cfg

    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "OUROBOROS_RUNTIME_MODE": "turbo",
                "OUROBOROS_SKILLS_REPO_PATH": "   ",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(settings_path))
    monkeypatch.delenv("OUROBOROS_RUNTIME_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_SKILLS_REPO_PATH", raising=False)

    try:
        cfg_reloaded = importlib.reload(cfg)
        loaded = cfg_reloaded.load_settings()
        assert loaded["OUROBOROS_RUNTIME_MODE"] == "advanced", (
            "Legacy 'turbo' runtime mode was not clamped at load time."
        )
        assert loaded["OUROBOROS_SKILLS_REPO_PATH"] == "", (
            "Whitespace-only skills repo path was not trimmed at load time."
        )
    finally:
        # ``ouroboros.config`` computes its path globals at import time, so
        # the reload above left ``SETTINGS_PATH`` pointing at ``tmp_path``.
        # Drop the temp override directly on ``os.environ`` (monkeypatch's
        # teardown order for stacked setenv+delenv of the same key is subtle
        # and can run AFTER subsequent tests import the module) and reload
        # the module one more time so its globals rebind against the real
        # user environment before pytest wipes the temp dir.
        os.environ.pop("OUROBOROS_SETTINGS_PATH", None)
        importlib.reload(cfg)
