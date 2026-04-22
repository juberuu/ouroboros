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
        extension_loader._tools.clear()
        extension_loader._routes.clear()
        extension_loader._ws_handlers.clear()
        extension_loader._ui_tabs.clear()
    yield
    with extension_loader._lock:
        extension_loader._extensions.clear()
        extension_loader._extension_modules.clear()
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

    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    # Attach drive_root to the INNER Starlette app's state
    # (``srv.app`` is the NetworkAuthGate wrapper; the inner Starlette
    # is at ``srv.app.app``).
    srv.app.app.state.drive_root = drive_root  # type: ignore[attr-defined]

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


def test_api_extension_dispatcher_404_for_unknown_route(tmp_path, monkeypatch):
    client, _, patches = _make_client(tmp_path, monkeypatch)
    try:
        resp = client.get("/api/extensions/nope/xyz")
        assert resp.status_code == 404
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
            reviewer_models=["openai/gpt-5.4"],
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
    ``type: "ext.*"`` WS messages through ``extension_loader.list_ws_handlers()``.
    AST-level check — the full runtime round-trip requires a live
    supervisor which is out of scope for this file."""
    import ast
    src = (pathlib.Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "ws_endpoint":
            body = ast.unparse(node)
            assert "ext." in body, "ws_endpoint has no ext.* dispatch branch"
            assert "list_ws_handlers" in body, (
                "ws_endpoint does not look up extension WS handlers via "
                "``extension_loader.list_ws_handlers``."
            )
            return
    assert False, "ws_endpoint not found in server.py"


def test_tool_registry_execute_dispatches_ext_tool():
    """Phase 5 regression: ``ToolRegistry.execute`` falls back to
    ``extension_loader.get_tool`` for ``ext.*`` names. Hermetic —
    registers a fake extension tool via the loader's internal dict
    to avoid booting the full skill machinery."""
    from ouroboros.tools import registry as tools_registry
    from ouroboros import extension_loader

    tmp_reg = tools_registry.ToolRegistry(repo_dir=pathlib.Path("/tmp"), drive_root=pathlib.Path("/tmp"))
    with extension_loader._lock:
        extension_loader._tools["ext.testskill.echo"] = {
            "name": "ext.testskill.echo",
            "handler": lambda ctx, **kwargs: f"hello {kwargs.get('who', 'world')}",
            "description": "echo",
            "schema": {},
            "timeout_sec": 10,
            "skill": "testskill",
        }
    try:
        result = tmp_reg.execute("ext.testskill.echo", {"who": "phase5"})
        assert result == "hello phase5"
        # get_timeout honours the extension's declared timeout.
        assert tmp_reg.get_timeout("ext.testskill.echo") == 10
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop("ext.testskill.echo", None)
