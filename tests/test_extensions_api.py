"""Phase 5 regression tests for the extension HTTP surface.

Covers:
- ``GET  /api/extensions``               catalogue snapshot
- ``GET  /api/extensions/<skill>/manifest``
- ``ALL  /api/extensions/<skill>/<rest>`` dispatcher
- ``POST /api/skills/<skill>/toggle``    UI-facing enable/disable

Uses Starlette TestClient so the full request path is exercised.
"""
from __future__ import annotations

import json
import pathlib

import pytest


@pytest.fixture(autouse=True)
def _clean_extensions():
    from ouroboros import extension_loader
    with extension_loader._lock:
        extension_loader._extensions.clear()
        extension_loader._extension_modules.clear()
        extension_loader._load_failures.clear()
        extension_loader._tools.clear()
        extension_loader._routes.clear()
        extension_loader._ws_handlers.clear()
        extension_loader._ui_tabs.clear()
    yield
    with extension_loader._lock:
        extension_loader._extensions.clear()
        extension_loader._extension_modules.clear()
        extension_loader._load_failures.clear()
        extension_loader._tools.clear()
        extension_loader._routes.clear()
        extension_loader._ws_handlers.clear()
        extension_loader._ui_tabs.clear()


def _write_ext(
    repo_root: pathlib.Path,
    name: str,
    *,
    permissions: list[str],
    plugin: str,
    env_from_settings: list[str] | None = None,
) -> pathlib.Path:
    skill_dir = repo_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    perms_yaml = json.dumps(permissions)
    env_yaml = json.dumps(env_from_settings or [])
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Test ext.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            f"permissions: {perms_yaml}\n"
            f"env_from_settings: {env_yaml}\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(plugin, encoding="utf-8")
    return skill_dir


def _make_client(tmp_path: pathlib.Path, monkeypatch):
    """Return a Starlette TestClient with drive_root pointed at tmp."""
    from unittest.mock import patch
    from starlette.testclient import TestClient

    import server as srv

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    # Attach drive_root to the INNER Starlette app's state
    # (``srv.app`` is the NetworkAuthGate wrapper; the inner Starlette
    # is at ``srv.app.app``).
    srv.app.app.state.drive_root = drive_root  # type: ignore[attr-defined]
    srv.app.app.state.repo_dir = tmp_path / "repo"  # type: ignore[attr-defined]

    # Minimal lifecycle patching — reuse the pattern from other tests.
    patches = [
        patch.object(srv, "_start_supervisor_if_needed", lambda *_a, **_k: None),
        patch.object(srv, "_apply_settings_to_env", lambda *_a, **_k: None),
        patch.object(srv, "apply_runtime_provider_defaults", lambda s: (s, False, [])),
        patch("ouroboros.server_auth.get_configured_network_password", return_value=""),
    ]
    for p in patches:
        p.start()
    client = TestClient(srv.app)
    return client, drive_root, patches


def _stop_patches(patches):
    for p in patches:
        try:
            p.stop()
        except RuntimeError:
            pass


def test_api_extensions_index_lists_extension_skills(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    plugin = (
        "def register(api):\n"
        "    api.register_tool('t', lambda ctx: 'ok', description='', schema={})\n"
    )
    _write_ext(skills_root, "ext_a", permissions=["tool"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.get("/api/extensions")
        assert resp.status_code == 200
        data = resp.json()
        names = {s["name"] for s in data.get("skills", [])}
        assert "ext_a" in names
        assert "live" in data
        ext_meta = next(s for s in data["skills"] if s["name"] == "ext_a")
        assert ext_meta["live_reason"] == "disabled"
    finally:
        _stop_patches(patches)


def test_api_extension_manifest_returns_metadata(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    plugin = "def register(api):\n    pass\n"
    _write_ext(skills_root, "ext_b", permissions=[], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.get("/api/extensions/ext_b/manifest")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "ext_b"
        assert data["manifest"]["type"] == "extension"
    finally:
        _stop_patches(patches)


def test_api_extension_manifest_prefers_runtime_load_error(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        find_skill,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    skill_dir = _write_ext(
        skills_root,
        "ext_manifest_error",
        permissions=["route"],
        plugin=(
            "def _hello(request):\n"
            "    return {'hello': 'world'}\n"
            "def register(api):\n"
            "    api.register_route('/absolute', _hello, methods=('GET',))\n"
        ),
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_manifest_error", True)
        save_review_state(
            drive_root,
            "ext_manifest_error",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        loaded = find_skill(drive_root, "ext_manifest_error", repo_path=str(skills_root))
        assert loaded is not None
        state = extension_loader.reconcile_extension(
            "ext_manifest_error",
            drive_root,
            lambda: {},
            repo_path=str(skills_root),
            retry_load_error=True,
        )
        assert state["action"] == "extension_load_error"

        resp = client.get("/api/extensions/ext_manifest_error/manifest")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "absolute" in str(data["load_error"])
    finally:
        _stop_patches(patches)


def test_api_extensions_index_marks_widget_only_extensions_as_ui_pending(
    tmp_path, monkeypatch
):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        find_skill,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    skill_dir = _write_ext(
        skills_root,
        "ext_widget",
        permissions=["widget"],
        plugin=(
            "def register(api):\n"
            "    api.register_ui_tab('weather', 'Weather', render={'kind': 'card'})\n"
        ),
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_widget", True)
        save_review_state(
            drive_root,
            "ext_widget",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        loaded = find_skill(drive_root, "ext_widget", repo_path=str(skills_root))
        assert loaded is not None
        err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
        assert err is None, err

        resp = client.get("/api/extensions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        entry = next(s for s in data["skills"] if s["name"] == "ext_widget")
        assert entry["live_loaded"] is True
        assert entry["dispatch_live"] is False
        assert entry["ui_tabs_pending"] == []
        assert data["live"]["ui_tabs"][0]["key"] == "ext_widget:weather"
        assert data["live"]["ui_tabs_pending"] == []
    finally:
        _stop_patches(patches)


def test_api_skill_toggle_enables_and_loads_extension(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import SkillReviewState, save_review_state, find_skill
    from ouroboros.skill_loader import compute_content_hash

    skills_root = tmp_path / "skills"
    plugin = (
        "def register(api):\n"
        "    api.register_tool('t', lambda ctx: 'ok', description='', schema={})\n"
    )
    skill_dir = _write_ext(skills_root, "ext_toggle", permissions=["tool"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        # Pre-mark review PASS so enable actually loads.
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_review_state(
            drive_root,
            "ext_toggle",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        resp = client.post(
            "/api/skills/ext_toggle/toggle",
            json={"enabled": True},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["enabled"] is True
        assert data["extension_action"] == "extension_loaded"
        assert "ext_toggle" in extension_loader.snapshot()["extensions"]

        # Disable → unload.
        resp = client.post(
            "/api/skills/ext_toggle/toggle",
            json={"enabled": False},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["enabled"] is False
        assert data["extension_action"] == "extension_unloaded"
        assert "ext_toggle" not in extension_loader.snapshot()["extensions"]
    finally:
        _stop_patches(patches)


def test_api_skill_toggle_collision_disable_does_not_write_shared_state(
    tmp_path, monkeypatch
):
    skills_root = tmp_path / "skills"
    plugin = "def register(api):\n    return None\n"
    _write_ext(skills_root, "hello world", permissions=[], plugin=plugin)
    _write_ext(skills_root, "hello_world", permissions=[], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.post("/api/skills/hello_world/toggle", json={"enabled": False})
        assert resp.status_code == 400, resp.text
        data = resp.json()
        assert data["extension_reason"] == "name_collision"
        state_file = drive_root / "state" / "skills" / "hello_world" / "enabled.json"
        assert not state_file.exists()
    finally:
        _stop_patches(patches)


def test_api_extension_dispatcher_routes_to_registered_handler(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    plugin = (
        "from starlette.responses import JSONResponse\n"
        "def _hello(request):\n"
        "    return JSONResponse({'hello': 'world'})\n"
        "def register(api):\n"
        "    api.register_route('greet', _hello, methods=('GET',))\n"
    )
    skill_dir = _write_ext(skills_root, "ext_route", permissions=["route"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_route", True)
        save_review_state(
            drive_root,
            "ext_route",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        from ouroboros.skill_loader import find_skill
        from ouroboros.config import load_settings
        refreshed = find_skill(drive_root, "ext_route", repo_path=str(skills_root))
        err = extension_loader.load_extension(refreshed, load_settings, drive_root=drive_root)
        assert err is None, err

        resp = client.get("/api/extensions/ext_route/greet")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"hello": "world"}
    finally:
        _stop_patches(patches)


def test_api_extension_dispatcher_allows_head_for_get_route(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        find_skill,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    skill_dir = _write_ext(
        skills_root,
        "ext_head",
        permissions=["route"],
        plugin=(
            "from starlette.responses import JSONResponse\n"
            "def _hello(request):\n"
            "    return JSONResponse({'hello': 'world'})\n"
            "def register(api):\n"
            "    api.register_route('greet', _hello, methods=('GET',))\n"
        ),
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_head", True)
        save_review_state(
            drive_root,
            "ext_head",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        loaded = find_skill(drive_root, "ext_head", repo_path=str(skills_root))
        assert loaded is not None
        err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
        assert err is None, err

        resp = client.head("/api/extensions/ext_head/greet")
        assert resp.status_code == 200, resp.text
    finally:
        _stop_patches(patches)


def test_api_extension_dispatcher_404_for_unknown_route(tmp_path, monkeypatch):
    client, _, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.get("/api/extensions/nope/xyz")
        assert resp.status_code == 404
    finally:
        _stop_patches(patches)


def test_api_extension_dispatcher_surfaces_lazy_load_error(tmp_path, monkeypatch):
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    plugin = (
        "def _hello(request):\n"
        "    return {'hello': 'world'}\n"
        "def register(api):\n"
        "    api.register_route('/absolute', _hello, methods=('GET',))\n"
    )
    skill_dir = _write_ext(skills_root, "ext_broken", permissions=["route"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_broken", True)
        save_review_state(
            drive_root,
            "ext_broken",
            SkillReviewState(status="pass", content_hash=content_hash),
        )

        resp = client.get("/api/extensions/ext_broken/greet")
        assert resp.status_code == 409, resp.text
        data = resp.json()
        assert data["state"]["action"] == "extension_load_error"
        assert data["state"]["reason"] == "load_error"
    finally:
        _stop_patches(patches)


def test_api_extension_dispatcher_rejects_not_live_route(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    plugin = (
        "from starlette.responses import JSONResponse\n"
        "def _hello(request):\n"
        "    return JSONResponse({'hello': 'world'})\n"
        "def register(api):\n"
        "    api.register_route('greet', _hello, methods=('GET',))\n"
    )
    skill_dir = _write_ext(skills_root, "ext_guarded", permissions=["route"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_guarded", True)
        save_review_state(
            drive_root,
            "ext_guarded",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        from ouroboros.skill_loader import find_skill

        loaded = find_skill(drive_root, "ext_guarded", repo_path=str(skills_root))
        assert loaded is not None
        err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
        assert err is None, err
        assert "ext_guarded" in extension_loader.snapshot()["extensions"]

        # Leave stale registrations in memory but mark the skill disabled on disk.
        save_enabled(drive_root, "ext_guarded", False)

        resp = client.get("/api/extensions/ext_guarded/greet")
        assert resp.status_code == 409, resp.text
        data = resp.json()
        assert data["state"]["reason"] == "disabled"
        assert "ext_guarded" not in extension_loader.snapshot()["extensions"]
    finally:
        _stop_patches(patches)


def test_api_extension_dispatcher_reloads_stale_live_route(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        find_skill,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    skill_dir = _write_ext(
        skills_root,
        "ext_route_reload",
        permissions=["route"],
        plugin=(
            "from starlette.responses import JSONResponse\n"
            "def _hello(request):\n"
            "    return JSONResponse({'hello': 'v1'})\n"
            "def register(api):\n"
            "    api.register_route('greet', _hello, methods=('GET',))\n"
        ),
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_route_reload", True)
        save_review_state(
            drive_root,
            "ext_route_reload",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        loaded = find_skill(drive_root, "ext_route_reload", repo_path=str(skills_root))
        assert loaded is not None
        err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
        assert err is None, err

        (skill_dir / "plugin.py").write_text(
            (
                "from starlette.responses import JSONResponse\n"
                "def _hello(request):\n"
                "    return JSONResponse({'hello': 'v2'})\n"
                "def register(api):\n"
                "    api.register_route('greet', _hello, methods=('GET',))\n"
            ),
            encoding="utf-8",
        )
        refreshed = find_skill(drive_root, "ext_route_reload", repo_path=str(skills_root))
        assert refreshed is not None
        save_review_state(
            drive_root,
            "ext_route_reload",
            SkillReviewState(status="pass", content_hash=refreshed.content_hash),
        )

        resp = client.get("/api/extensions/ext_route_reload/greet")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"hello": "v2"}
    finally:
        _stop_patches(patches)


def test_api_skill_toggle_rejects_non_boolean_enabled(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    plugin = "def register(api):\n    pass\n"
    _write_ext(skills_root, "ext_toggle_bad", permissions=[], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, _, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.post("/api/skills/ext_toggle_bad/toggle", json={"enabled": "definitely"})
        assert resp.status_code == 400
        assert "boolean" in resp.text
    finally:
        _stop_patches(patches)


def test_api_skill_grants_requires_owner_bridge(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    _write_ext(
        skills_root,
        "grant_api",
        permissions=["tool"],
        plugin="def register(api):\n    pass\n",
        env_from_settings=["OPENROUTER_API_KEY"],
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, _drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.post(
            "/api/skills/grant_api/grants",
            json={"granted_keys": ["OPENROUTER_API_KEY"]},
        )
        assert resp.status_code == 403
        assert resp.json()["code"] == "owner_confirmation_required"
    finally:
        _stop_patches(patches)


def test_api_skill_reconcile_clears_cached_load_error(tmp_path, monkeypatch):
    """v5.2.2 dual-track grants: ``POST /api/skills/<name>/reconcile``
    is the loopback endpoint the desktop launcher pings after a
    successful core-key grant. It must clear the server's cached
    ``_load_failures`` entry and re-run ``load_extension`` so the
    plugin picks up the freshly-granted key without forcing the user
    to disable/enable.
    """
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        find_skill,
        save_enabled,
        save_review_state,
        save_skill_grants,
    )

    skills_root = tmp_path / "skills"
    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    _write_ext(
        skills_root,
        "reconcile_demo",
        permissions=["tool", "read_settings"],
        plugin=plugin,
        env_from_settings=["OPENROUTER_API_KEY"],
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        first = find_skill(drive_root, "reconcile_demo", repo_path=str(skills_root))
        assert first is not None
        save_enabled(drive_root, "reconcile_demo", True)
        save_review_state(
            drive_root,
            "reconcile_demo",
            SkillReviewState(status="pass", content_hash=first.content_hash),
        )
        loaded = find_skill(drive_root, "reconcile_demo", repo_path=str(skills_root))
        assert loaded is not None and loaded.enabled

        # First load attempt — no grant on disk → fails with the new
        # informative error and seeds ``_load_failures``.
        err = extension_loader.load_extension(
            loaded, lambda: {"OPENROUTER_API_KEY": "sk-secret"}, drive_root=drive_root,
        )
        assert err is not None
        assert "missing owner grants" in err
        with extension_loader._lock:
            extension_loader._load_failures["reconcile_demo"] = (
                extension_loader._ExtensionLoadFailure(
                    content_hash=loaded.content_hash,
                    skill_dir=str(loaded.skill_dir.resolve()),
                    error=err,
                )
            )

        # Owner grants → simulate the launcher writing grants.json.
        save_skill_grants(
            drive_root,
            "reconcile_demo",
            ["OPENROUTER_API_KEY"],
            content_hash=loaded.content_hash,
            requested_keys=["OPENROUTER_API_KEY"],
        )

        # The endpoint must clear the cached failure and load the plugin.
        resp = client.post("/api/skills/reconcile_demo/reconcile")
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["skill"] == "reconcile_demo"
        assert payload["live_loaded"] is True
        assert payload["extension_action"] == "extension_loaded"
        with extension_loader._lock:
            assert "reconcile_demo" in extension_loader._extensions
            assert "reconcile_demo" not in extension_loader._load_failures
    finally:
        _stop_patches(patches)


def test_api_skill_reconcile_rejects_missing_skill_name(tmp_path, monkeypatch):
    client, _drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        # Starlette path params with empty trailing segment → 404 path,
        # but explicit empty skill via direct call returns 400 from the
        # endpoint's own validation.
        resp = client.post("/api/skills/ /reconcile")
        # Whitespace-only path param hits the endpoint with stripped
        # empty name → 400.
        assert resp.status_code == 400
    finally:
        _stop_patches(patches)


def test_api_skill_review_offloads_to_thread_and_returns_outcome(tmp_path, monkeypatch):
    """Phase 5 regression: ``POST /api/skills/<skill>/review`` must
    trigger the tri-model review and return the outcome. The async
    Starlette endpoint offloads to ``asyncio.to_thread`` so the event
    loop stays responsive."""
    from unittest.mock import patch

    from ouroboros.skill_review import SkillReviewOutcome

    skills_root = tmp_path / "skills"
    plugin = "def register(api): pass\n"
    _write_ext(skills_root, "ext_r", permissions=[], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        canned = SkillReviewOutcome(
            skill_name="ext_r",
            status="pass",
            findings=[{"item": "manifest_schema", "verdict": "PASS"}],
            reviewer_models=["openai/gpt-5.5"],
            content_hash="abcd",
            error="",
        )
        with patch(
            "ouroboros.extensions_api._review_skill_impl",
            create=True,
            return_value=canned,
        ), patch(
            "ouroboros.skill_review.review_skill", return_value=canned,
        ):
            resp = client.post("/api/skills/ext_r/review", json={})
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "pass"
            assert data["skill"] == "ext_r"
    finally:
        _stop_patches(patches)


def test_ws_endpoint_dispatches_ext_prefixed_messages():
    """Phase 5 regression: server.py::ws_endpoint must route
    provider-safe extension WS messages through ``extension_loader.list_ws_handlers()``.
    AST-level check — the full runtime round-trip requires a live
    supervisor which is out of scope for this file."""
    import ast
    src = (pathlib.Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ws_endpoint":
            body = ast.unparse(node)
            assert "parse_extension_surface_name" in body, "ws_endpoint has no extension dispatch branch"
            assert "list_ws_handlers" in body, (
                "ws_endpoint does not look up extension WS handlers via "
                "``extension_loader.list_ws_handlers``."
            )
            return
    assert False, "ws_endpoint not found in server.py"


def test_ws_endpoint_reconciles_and_unloads_not_live_extension(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        find_skill,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    plugin = (
        "async def _handler(payload):\n"
        "    return {'acked': True}\n"
        "def register(api):\n"
        "    api.register_ws_handler('message', _handler)\n"
    )
    skill_dir = _write_ext(skills_root, "ext_ws_guarded", permissions=["ws_handler"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_ws_guarded", True)
        save_review_state(
            drive_root,
            "ext_ws_guarded",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        loaded = find_skill(drive_root, "ext_ws_guarded", repo_path=str(skills_root))
        assert loaded is not None
        err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
        assert err is None, err
        assert "ext_ws_guarded" in extension_loader.snapshot()["extensions"]

        save_enabled(drive_root, "ext_ws_guarded", False)

        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": extension_loader.extension_surface_name("ext_ws_guarded", "message")}))
            reply = json.loads(ws.receive_text())
        assert reply["type"] == "log"
        assert "not live" in reply["data"]["message"]
        assert "ext_ws_guarded" not in extension_loader.snapshot()["extensions"]
    finally:
        _stop_patches(patches)


def test_ws_endpoint_dispatches_first_message_after_lazy_load(tmp_path, monkeypatch):
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    plugin = (
        "async def _handler(payload):\n"
        "    return {'acked': payload.get('payload')}\n"
        "def register(api):\n"
        "    api.register_ws_handler('message', _handler)\n"
    )
    skill_dir = _write_ext(skills_root, "ext_ws_lazy", permissions=["ws_handler"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_ws_lazy", True)
        save_review_state(
            drive_root,
            "ext_ws_lazy",
            SkillReviewState(status="pass", content_hash=content_hash),
        )
        extension_loader.unload_extension("ext_ws_lazy")
        msg_type = extension_loader.extension_surface_name("ext_ws_lazy", "message")
        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": msg_type, "payload": "first"}))
            reply = json.loads(ws.receive_text())
        assert reply == {"type": f"{msg_type}.reply", "data": {"acked": "first"}}
    finally:
        _stop_patches(patches)


def test_ws_endpoint_surfaces_extension_load_error(tmp_path, monkeypatch):
    skills_root = tmp_path / "skills"
    skill_dir = _write_ext(
        skills_root,
        "ext_ws_broken",
        permissions=["ws_handler"],
        plugin=(
            "async def _handler(payload):\n"
            "    return {'acked': True}\n"
            "def register(api):\n"
            "    api.register_ws_handler('bad-type', _handler)\n"
        ),
    )
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    client, drive_root, patches = _make_client(tmp_path, monkeypatch)
    try:
        from ouroboros import extension_loader
        from ouroboros.skill_loader import SkillReviewState, compute_content_hash, save_enabled, save_review_state

        content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
        save_enabled(drive_root, "ext_ws_broken", True)
        save_review_state(
            drive_root,
            "ext_ws_broken",
            SkillReviewState(status="pass", content_hash=content_hash),
        )

        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"type": extension_loader.extension_surface_name("ext_ws_broken", "message")}))
            reply = json.loads(ws.receive_text())
        assert reply["type"] == "log"
        assert "failed to go live" in reply["data"]["message"]
    finally:
        _stop_patches(patches)


def test_tool_registry_execute_dispatches_ext_tool(tmp_path, monkeypatch):
    """Phase 5 regression: ``ToolRegistry.execute`` falls back to
    ``extension_loader.get_tool`` for extension names, but only for
    reviewed/live extensions that are surfaced through the normal
    registry schema lookup."""
    from ouroboros.tools import registry as tools_registry
    from ouroboros import extension_loader
    from ouroboros.skill_loader import (
        SkillReviewState,
        compute_content_hash,
        find_skill,
        save_enabled,
        save_review_state,
    )

    skills_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = (
        "def _echo(ctx, who='world'):\n"
        "    return f'hello {who}'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={}, timeout_sec=10)\n"
    )
    skill_dir = _write_ext(skills_root, "testskill", permissions=["tool"], plugin=plugin)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(skills_root))
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    save_enabled(drive_root, "testskill", True)
    save_review_state(
        drive_root,
        "testskill",
        SkillReviewState(status="pass", content_hash=content_hash),
    )
    loaded = find_skill(drive_root, "testskill", repo_path=str(skills_root))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    try:
        tmp_reg = tools_registry.ToolRegistry(repo_dir=tmp_path, drive_root=drive_root)
        tool_name = extension_loader.extension_surface_name("testskill", "echo")
        schema = tmp_reg.get_schema_by_name(tool_name)
        assert schema is not None
        assert schema["function"]["name"] == tool_name
        result = tmp_reg.execute(tool_name, {"who": "phase5"})
        # v5.1.2 iter-2: extension dispatch now goes through
        # ``ouroboros.safety.check_safety``. In test envs without a
        # safety backend, the supervisor returns a visible
        # ``SAFETY_WARNING`` prefix while still letting the call run
        # (fail-open). Assert the handler ran and produced its output;
        # the warning prefix is acceptable.
        assert "hello phase5" in result, result
        # get_timeout honours the extension's declared timeout.
        assert tmp_reg.get_timeout(tool_name) == 10
    finally:
        extension_loader.unload_extension("testskill")
