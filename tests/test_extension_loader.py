"""Phase 4 regression tests for ``ouroboros.extension_loader``.

Covers PluginAPI surface: register_tool / register_route /
register_ws_handler / register_ui_tab + permission gating +
namespace enforcement + unload cleanup.
"""
from __future__ import annotations

import json
import pathlib
import re
from typing import Any, Dict

import pytest

from ouroboros import extension_loader
from ouroboros.contracts.plugin_api import (
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    PluginAPI,
    VALID_EXTENSION_PERMISSIONS,
)
from ouroboros.skill_loader import (
    SkillReviewState,
    compute_content_hash,
    find_skill,
    save_enabled,
    save_review_state,
)


@pytest.fixture(autouse=True)
def _clear_loader_state(monkeypatch):
    """Reset the module-level registries between tests."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
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


def _write_ext_skill(
    repo_root: pathlib.Path,
    name: str,
    *,
    plugin_body: str,
    permissions: list[str],
    env_from_settings: list[str] | None = None,
    entry: str = "plugin.py",
) -> pathlib.Path:
    skill_dir = repo_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    perms_yaml = json.dumps(permissions)
    env_yaml = json.dumps(env_from_settings or [])
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            f"name: {name}\n"
            "description: Phase 4 extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            f"entry: {entry}\n"
            f"permissions: {perms_yaml}\n"
            f"env_from_settings: {env_yaml}\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    entry_path = skill_dir / entry
    entry_path.parent.mkdir(parents=True, exist_ok=True)
    entry_path.write_text(plugin_body, encoding="utf-8")
    return skill_dir


def _prepare_extension(
    tmp_path: pathlib.Path,
    name: str,
    plugin_body: str,
    permissions: list[str],
    env_from_settings: list[str] | None = None,
):
    """Write + enable + PASS-review an extension so the loader accepts it."""
    from ouroboros.skill_loader import find_skill
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    skill_dir = _write_ext_skill(
        repo_root,
        name,
        plugin_body=plugin_body,
        permissions=permissions,
        env_from_settings=env_from_settings,
    )
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    save_enabled(drive_root, name, True)
    save_review_state(
        drive_root,
        name,
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    # Refetch with fresh state on the loaded struct.
    loaded = find_skill(drive_root, name, repo_path=str(repo_root))
    assert loaded is not None
    return loaded, repo_root, drive_root


# ---------------------------------------------------------------------------
# PluginAPI contract shape
# ---------------------------------------------------------------------------


def test_plugin_api_impl_matches_protocol():
    """Runtime-checkable Protocol must structurally accept PluginAPIImpl."""
    impl = extension_loader.PluginAPIImpl(
        skill_name="x",
        permissions=(),
        env_allowlist=(),
        state_dir=pathlib.Path("/tmp"),
        settings_reader=lambda: {},
    )
    assert isinstance(impl, PluginAPI)


def test_forbidden_extension_settings_carries_repo_secrets():
    """The forbidden-settings tuple must match the repo-credentials set
    ``skill_exec`` already refuses to forward."""
    assert "OPENROUTER_API_KEY" in FORBIDDEN_EXTENSION_SETTINGS
    assert "GITHUB_TOKEN" in FORBIDDEN_EXTENSION_SETTINGS
    assert "OUROBOROS_NETWORK_PASSWORD" in FORBIDDEN_EXTENSION_SETTINGS


def test_valid_permissions_is_closed_set():
    for needed in ("tool", "route", "ws_handler", "widget", "read_settings", "net", "fs", "subprocess"):
        assert needed in VALID_EXTENSION_PERMISSIONS


# ---------------------------------------------------------------------------
# Successful load + registration
# ---------------------------------------------------------------------------


def test_load_extension_registers_tool(tmp_path):
    plugin = (
        "def _echo(ctx, message='hi'):\n"
        "    return f'echo: {message}'\n"
        "def register(api):\n"
        "    api.register_tool(\n"
        "        'echo',\n"
        "        _echo,\n"
        "        description='echo',\n"
        "        schema={'type': 'object', 'properties': {'message': {'type': 'string'}}},\n"
        "    )\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ext1", plugin, permissions=["tool"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    tool_name = extension_loader.extension_surface_name("ext1", "echo")
    tool = extension_loader.get_tool(tool_name)
    assert tool is not None
    assert tool["name"] == tool_name
    assert callable(tool["handler"])


def test_extension_surface_names_are_provider_safe_without_renaming_skill_identity():
    from ouroboros.skill_loader import _sanitize_skill_name

    dotted = "foo.bar"
    unicode_name = "погода"
    dotted_tool = extension_loader.extension_surface_name(dotted, "fetch")
    unicode_tool = extension_loader.extension_surface_name(unicode_name, "fetch")
    generated_token_twin = "foo_bar_336d1b3d72"

    assert _sanitize_skill_name(dotted) == dotted
    assert _sanitize_skill_name("foo_bar") == "foo_bar"
    assert dotted_tool != extension_loader.extension_surface_name("foo_bar", "fetch")
    assert dotted_tool != extension_loader.extension_surface_name(generated_token_twin, "fetch")
    assert extension_loader.extension_surface_name("foo", "bar_baz") != extension_loader.extension_surface_name("foo_bar", "baz")
    for tool_name in (dotted_tool, unicode_tool):
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", tool_name)
        assert "." not in tool_name
        assert extension_loader.parse_extension_surface_name(tool_name) is not None


def test_load_extension_rejects_outward_symlink_in_skill_tree(tmp_path):
    import os, platform

    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    skill_dir = tmp_path / "skills" / "symlinked"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: symlinked\n"
            "description: Symlink escape regression.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import SECRET\n"
            "def _echo(ctx):\n"
            "    return SECRET\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "symlinked", True)
    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "symlinked",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    outside = tmp_path / "outside_helper.py"
    outside.write_text("SECRET = 'escape'\n", encoding="utf-8")
    os.symlink(outside, skill_dir / "helper.py")

    loaded = find_skill(drive_root, "symlinked", repo_path=str(skill_dir.parent))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is not None
    assert "symlink" in err.lower()
    assert extension_loader.get_tool(extension_loader.extension_surface_name("symlinked", "echo")) is None


def test_load_extension_registers_route_with_prefix(tmp_path):
    plugin = (
        "def _handler(request): return {'ok': True}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods=('GET',))\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ext2", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    snap = extension_loader.snapshot()
    assert "/api/extensions/ext2/weather" in snap["routes"]


def test_load_extension_rejects_absolute_route(tmp_path):
    plugin = (
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('/absolute', _handler)\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ext_abs", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None
    assert "absolute" in err.lower()


def test_load_extension_rejects_traversal_route(tmp_path):
    plugin = (
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('../escape', _handler)\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ext_traverse", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None


def test_load_extension_rejects_unsupported_route_method(tmp_path):
    plugin = (
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods=('TRACE',))\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ext_trace", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None
    assert "unsupported" in err.lower()


def test_load_extension_accepts_string_route_method(tmp_path):
    plugin = (
        "def _handler(r): return {}\n"
        "def register(api):\n"
        "    api.register_route('weather', _handler, methods='GET')\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ext_get_string", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    snap = extension_loader.snapshot()
    assert "/api/extensions/ext_get_string/weather" in snap["routes"]


def test_load_extension_supports_nested_entry_relative_imports(tmp_path):
    repo_root = tmp_path / "skills"
    skill_dir = _write_ext_skill(
        repo_root,
        "ext_nested",
        permissions=["tool"],
        entry="pkg/plugin.py",
        plugin_body=(
            "from .helper import VALUE\n"
            "def register(api):\n"
            "    api.register_tool('t', lambda ctx: VALUE, description='', schema={})\n"
        ),
    )
    (skill_dir / "pkg" / "helper.py").write_text("VALUE = 'nested-ok'\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "ext_nested", True)
    content_hash = compute_content_hash(skill_dir, manifest_entry="pkg/plugin.py")
    save_review_state(
        drive_root,
        "ext_nested",
        SkillReviewState(status="pass", content_hash=content_hash),
    )
    loaded = find_skill(drive_root, "ext_nested", repo_path=str(repo_root))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("ext_nested", "t"))
    assert tool is not None
    assert tool["handler"](None) == "nested-ok"


def test_unload_dotted_prefix_skill_does_not_break_neighbor_imports(tmp_path):
    repo_root = tmp_path / "skills"
    foo_dir = _write_ext_skill(
        repo_root,
        "foo",
        permissions=["tool"],
        plugin_body=(
            "def register(api):\n"
            "    api.register_tool('t', lambda ctx: 'foo', description='', schema={})\n"
        ),
    )
    dotted_dir = _write_ext_skill(
        repo_root,
        "foo.bar",
        permissions=["tool"],
        plugin_body=(
            "def _lazy(ctx):\n"
            "    from .helper import VALUE\n"
            "    return VALUE\n"
            "def register(api):\n"
            "    api.register_tool('lazy', _lazy, description='', schema={})\n"
        ),
    )
    (dotted_dir / "helper.py").write_text("VALUE = 'still-live'\n", encoding="utf-8")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    for name, skill_dir in (("foo", foo_dir), ("foo.bar", dotted_dir)):
        save_enabled(drive_root, name, True)
        save_review_state(
            drive_root,
            name,
            SkillReviewState(status="pass", content_hash=compute_content_hash(skill_dir, manifest_entry="plugin.py")),
        )
        loaded = find_skill(drive_root, name, repo_path=str(repo_root))
        assert loaded is not None
        assert extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root) is None

    extension_loader.unload_extension("foo")
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("foo.bar", "lazy"))
    assert tool is not None
    assert tool["handler"](None) == "still-live"


def test_load_extension_registers_ws_handler_with_namespace(tmp_path):
    plugin = (
        "async def _handler(payload):\n"
        "    return {'acked': True}\n"
        "def register(api):\n"
        "    api.register_ws_handler('message', _handler)\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "ws1", plugin, permissions=["ws_handler"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    handlers = extension_loader.list_ws_handlers()
    assert extension_loader.extension_surface_name("ws1", "message") in handlers


def test_register_ui_tab_surfaces_hostable_widget(tmp_path):
    loaded, _, _ = _prepare_extension(
        tmp_path,
        "uiwait",
        "def register(api):\n"
        "    api.register_ui_tab('weather', 'Weather', render={'kind': 'card'})\n",
        permissions=["widget"],
    )
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["ui_tabs_pending"] == []
    assert snap["ui_tabs"][0]["key"] == "uiwait:weather"
    assert snap["ui_tabs"][0]["render"]["kind"] == "card"

    extension_loader.unload_extension("uiwait")
    snap = extension_loader.snapshot()
    assert snap["ui_tabs"] == []


def test_load_extension_permission_gate_tool(tmp_path):
    """Extension without 'tool' permission cannot register a tool."""
    plugin = (
        "def _h(ctx): return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('x', _h, description='', schema={})\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "nopoerm", plugin, permissions=["route"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None
    assert "'tool'" in err


def test_load_extension_enforces_review_pass(tmp_path):
    """Unreviewed extension is refused (after being enabled)."""
    from ouroboros.skill_loader import find_skill
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = "def register(api): pass\n"
    _write_ext_skill(repo_root, "unreviewed", plugin_body=plugin, permissions=[])
    # Enable to get past the "disabled" gate — we want to exercise the
    # review-status gate specifically.
    save_enabled(drive_root, "unreviewed", True)
    loaded = find_skill(drive_root, "unreviewed", repo_path=str(repo_root))
    assert loaded is not None
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None
    assert "PASS review" in err


def test_load_extension_refuses_disabled(tmp_path):
    from ouroboros.skill_loader import find_skill
    repo_root = tmp_path / "skills"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    plugin = "def register(api): pass\n"
    _write_ext_skill(repo_root, "d1", plugin_body=plugin, permissions=[])
    loaded = find_skill(drive_root, "d1", repo_path=str(repo_root))
    assert loaded is not None
    save_review_state(
        drive_root,
        "d1",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    # NOT enabled.
    loaded = find_skill(drive_root, "d1", repo_path=str(repo_root))
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None
    assert "disabled" in err


def test_reconcile_extension_stays_loaded_in_light_mode(tmp_path, monkeypatch):
    """v5.1.2 Frame A: ``light`` no longer unloads extensions. The
    ``runtime_mode_light`` reason is gone from
    ``_extension_runtime_state``. Extensions follow the same
    enabled / review / content-hash gates regardless of mode.
    """
    plugin = (
        "def _echo(ctx):\n"
        "    return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "lightstop",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    assert "lightstop" in extension_loader.snapshot()["extensions"]

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    state = extension_loader.reconcile_extension(
        "lightstop",
        drive_root,
        lambda: {},
        repo_path=repo_root,
    )
    # The ``runtime_mode_light`` reason was removed in v5.1.2; the
    # extension stays live.
    assert state["reason"] != "runtime_mode_light"
    assert state["action"] != "extension_unloaded"
    assert "lightstop" in extension_loader.snapshot()["extensions"]


def test_reconcile_extension_keeps_live_extension_loaded(tmp_path, monkeypatch):
    plugin = (
        "def _echo(ctx):\n"
        "    return 'ok'\n"
        "def register(api):\n"
        "    api.register_tool('echo', _echo, description='echo', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "steady",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    unload_calls: list[str] = []
    monkeypatch.setattr(extension_loader, "unload_extension", unload_calls.append)

    state = extension_loader.reconcile_extension(
        "steady",
        drive_root,
        lambda: {},
        repo_path=repo_root,
    )
    assert state["reason"] == "ready"
    assert state["action"] == "extension_already_live"
    assert unload_calls == []
    assert "steady" in extension_loader.snapshot()["extensions"]


def test_reconcile_extension_reloads_when_live_code_changes(tmp_path):
    from ouroboros.skill_loader import find_skill

    skill_dir = tmp_path / "skills" / "reloadme"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: reloadme\n"
            "description: Live reload.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text(
        (
            "def _echo(ctx):\n"
            "    return 'v1'\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    save_enabled(drive_root, "reloadme", True)
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "reloadme",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reloadme", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "v1"

    (skill_dir / "plugin.py").write_text(
        (
            "def _echo(ctx):\n"
            "    return 'v2'\n"
            "def register(api):\n"
            "    api.register_tool('echo', _echo, description='echo', schema={})\n"
        ),
        encoding="utf-8",
    )
    loaded = find_skill(drive_root, "reloadme", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "reloadme",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )

    state = extension_loader.reconcile_extension(
        "reloadme",
        drive_root,
        lambda: {},
        repo_path=skill_dir.parent,
        retry_load_error=True,
    )
    assert state["action"] == "extension_loaded"
    assert state["live_loaded"] is True
    tool = extension_loader.get_tool(extension_loader.extension_surface_name("reloadme", "echo"))
    assert tool is not None
    assert tool["handler"](None) == "v2"


def test_runtime_state_preserves_matching_load_error(tmp_path):
    plugin = (
        "def _hello(request):\n"
        "    return {'hello': 'world'}\n"
        "def register(api):\n"
        "    api.register_route('/absolute', _hello, methods=('GET',))\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "brokenlive",
        plugin,
        permissions=["route"],
    )
    state = extension_loader.reconcile_extension(
        "brokenlive",
        drive_root,
        lambda: {},
        repo_path=repo_root,
        retry_load_error=True,
    )
    assert state["action"] == "extension_load_error"
    refreshed = extension_loader.runtime_state_for_skill_name(
        "brokenlive",
        drive_root,
        repo_path=repo_root,
    )
    assert refreshed["reason"] == "load_error"
    assert "absolute" in str(refreshed["load_error"])
    assert refreshed["live_loaded"] is False


def test_runtime_state_for_skill_name_reports_missing_skill(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    state = extension_loader.runtime_state_for_skill_name(
        "ghost",
        drive_root,
        repo_path=tmp_path / "skills",
    )
    assert state["desired_live"] is False
    assert state["live_loaded"] is False
    assert state["reason"] == "missing"


def test_get_settings_blocks_core_keys_without_grant(tmp_path):
    """An extension that lists a core key in env_from_settings without
    an owner grant fails to load and ``PluginAPIImpl.get_settings``
    silently drops the key — the dual-track grant model deliberately
    keeps the failure mode the same as the script path."""
    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "envtest",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK"],
    )
    settings_snapshot = {
        "OPENROUTER_API_KEY": "sk-leak",
        "TIMEZONE": "UTC",
        "MY_OK": "visible",
        "RANDOM_OTHER": "not-allowed",
    }
    err = extension_loader.load_extension(loaded, lambda: settings_snapshot, drive_root=drive_root)
    assert err is not None
    assert "missing owner grants" in err
    assert "OPENROUTER_API_KEY" in err

    impl = extension_loader.PluginAPIImpl(
        skill_name="envtest",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
        granted_keys=[],
    )
    got = impl.get_settings(["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK", "RANDOM_OTHER"])
    assert "OPENROUTER_API_KEY" not in got
    assert got["TIMEZONE"] == "UTC"
    assert got["MY_OK"] == "visible"
    assert "RANDOM_OTHER" not in got


def test_load_extension_rejects_grant_with_stale_content_hash(tmp_path):
    """v5.2.2 dual-track grants: the loader binds the persisted grant
    to the current content hash. A grants.json written for a prior
    revision must NOT authorise the freshly-edited plugin (defense in
    depth — even if ``grant_status_for_skill`` is bypassed)."""
    from ouroboros.skill_loader import save_skill_grants

    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "stale_grant",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY"],
    )
    # Persist a grant with the WRONG content hash — simulates a manifest
    # / plugin edit that the operator has not re-authorised.
    save_skill_grants(
        drive_root,
        "stale_grant",
        ["OPENROUTER_API_KEY"],
        content_hash="some-other-hash",
        requested_keys=["OPENROUTER_API_KEY"],
    )
    err = extension_loader.load_extension(
        loaded,
        lambda: {"OPENROUTER_API_KEY": "sk-secret"},
        drive_root=drive_root,
    )
    assert err is not None
    assert "missing owner grants" in err


def test_get_settings_returns_core_key_with_grant(tmp_path):
    """An owner-granted core key is forwarded to the in-process plugin
    via ``PluginAPIImpl.get_settings``. The grant must be bound to the
    current content hash + manifest-requested set; ``load_extension``
    enforces both before constructing the API impl."""
    from ouroboros.skill_loader import save_skill_grants

    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, drive_root = _prepare_extension(
        tmp_path,
        "granted_ext",
        plugin,
        permissions=["tool", "read_settings"],
        env_from_settings=["OPENROUTER_API_KEY", "TIMEZONE"],
    )
    save_skill_grants(
        drive_root,
        "granted_ext",
        ["OPENROUTER_API_KEY"],
        content_hash=loaded.content_hash,
        requested_keys=["OPENROUTER_API_KEY"],
    )
    settings_snapshot = {
        "OPENROUTER_API_KEY": "sk-allowed",
        "TIMEZONE": "UTC",
    }
    err = extension_loader.load_extension(loaded, lambda: settings_snapshot, drive_root=drive_root)
    assert err is None, err

    impl = extension_loader.PluginAPIImpl(
        skill_name="granted_ext",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
        granted_keys=["OPENROUTER_API_KEY"],
    )
    got = impl.get_settings(["OPENROUTER_API_KEY", "TIMEZONE"])
    assert got.get("OPENROUTER_API_KEY") == "sk-allowed"
    assert got.get("TIMEZONE") == "UTC"

    # Grant on the WRONG content hash must not authorise — the loader
    # builds an empty granted_keys list and drops the value.
    impl_no_grant = extension_loader.PluginAPIImpl(
        skill_name="granted_ext",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
        granted_keys=[],
    )
    assert "OPENROUTER_API_KEY" not in impl_no_grant.get_settings(["OPENROUTER_API_KEY"])


def test_unload_removes_all_registrations(tmp_path):
    plugin = (
        "def _t(c): return 'x'\n"
        "def _r(req): return {}\n"
        "def _w(p): return {}\n"
        "def register(api):\n"
        "    api.register_tool('t', _t, description='', schema={})\n"
        "    api.register_route('r', _r)\n"
        "    api.register_ws_handler('w', _w)\n"
    )
    loaded, _, _ = _prepare_extension(
        tmp_path,
        "full",
        plugin,
        permissions=["tool", "route", "ws_handler"],
    )
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None, err
    snap = extension_loader.snapshot()
    assert snap["tools"] and snap["routes"] and snap["ws_handlers"]

    extension_loader.unload_extension("full")
    snap = extension_loader.snapshot()
    assert snap["tools"] == []
    assert snap["routes"] == []
    assert snap["ws_handlers"] == []
    assert snap["extensions"] == []


def test_reload_all_called_on_settings_save():
    """Phase 4 regression: ``server.py::api_settings_post`` must
    reconcile the live extension registry when OUROBOROS_SKILLS_REPO_PATH
    changes; otherwise switching repo path leaves stale extensions
    registered from the old path."""
    import ast
    src = (pathlib.Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "api_settings_post"
        ):
            body_text = ast.unparse(node)
            assert "reload_all" in body_text or "_reload_extensions" in body_text, (
                "api_settings_post must call extension_loader.reload_all on "
                "OUROBOROS_SKILLS_REPO_PATH change."
            )
            assert "OUROBOROS_SKILLS_REPO_PATH" in body_text
            assert "OUROBOROS_RUNTIME_MODE" in body_text, (
                "api_settings_post must also reconcile extensions when "
                "runtime mode changes."
            )
            return
    assert False, "api_settings_post function not found in server.py"


def test_reload_all_called_from_server_startup():
    """Phase 4 regression: server.py main() must call
    ``extension_loader.reload_all`` during startup so enabled extensions
    survive a restart. Without this, only ``toggle_skill`` could ever
    load a plugin."""
    import ast
    src = (pathlib.Path(__file__).resolve().parent.parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            body_text = ast.unparse(node)
            assert "_reload_extensions(" in body_text or "reload_all(" in body_text, (
                "server.py does not wire extension_loader.reload_all into startup — "
                "enabled extensions would not survive a process restart."
            )
            assert "if repo_path" not in body_text, (
                "startup extension reload must run even when only bundled "
                "skills are present."
            )
            return
    assert False, "lifespan function not found in server.py"


def test_reload_all_tears_down_stale_extensions(tmp_path):
    """reload_all must unload extensions that no longer exist on disk."""
    plugin = (
        "def register(api):\n"
        "    api.register_tool('t', lambda ctx: 'ok', description='', schema={})\n"
    )
    loaded, repo_root, drive_root = _prepare_extension(
        tmp_path,
        "staleish",
        plugin,
        permissions=["tool"],
    )
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is None
    assert "staleish" in extension_loader.snapshot()["extensions"]
    # Nuke the skill directory; reload_all should tear it down.
    import shutil
    shutil.rmtree(repo_root / "staleish")
    extension_loader.reload_all(drive_root, lambda: {}, repo_path=str(repo_root))
    assert "staleish" not in extension_loader.snapshot()["extensions"]


def test_unload_clears_child_module_cache(tmp_path):
    """Phase 4 round 3 regression: unload must purge EVERY
    ``ouroboros._extensions.<skill>.*`` entry from sys.modules, not
    just the top-level module. Otherwise a helper-file edit sticks to
    the stale cached module on reload."""
    import sys as _sys
    skill_dir = tmp_path / "skills" / "tree_ext"
    (skill_dir).mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: tree_ext\n"
            "description: Multi-file extension.\n"
            "version: 0.1.0\n"
            "type: extension\n"
            "entry: plugin.py\n"
            "permissions: [\"tool\"]\n"
            "env_from_settings: []\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    (skill_dir / "helper.py").write_text("X = 'v1'\n", encoding="utf-8")
    (skill_dir / "plugin.py").write_text(
        (
            "from .helper import X\n"
            "def _t(ctx): return X\n"
            "def register(api):\n"
            "    api.register_tool('echo', _t, description='', schema={})\n"
        ),
        encoding="utf-8",
    )
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    from ouroboros.skill_loader import find_skill
    save_enabled(drive_root, "tree_ext", True)
    loaded = find_skill(drive_root, "tree_ext", repo_path=str(skill_dir.parent))
    assert loaded is not None
    save_review_state(
        drive_root,
        "tree_ext",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "tree_ext", repo_path=str(skill_dir.parent))

    err = extension_loader.load_extension(loaded, lambda: {}, drive_root=drive_root)
    assert err is None, err
    # Both the package module and its helper child module must live in
    # sys.modules after import, and BOTH must be purged on unload.
    parent_key = extension_loader._module_key("tree_ext")
    child_key = f"{parent_key}.helper"
    assert parent_key in _sys.modules
    assert child_key in _sys.modules
    extension_loader.unload_extension("tree_ext")
    assert parent_key not in _sys.modules
    assert child_key not in _sys.modules


def test_tool_registration_collision_raises(tmp_path):
    """Two plugins registering the same tool namespace collide."""
    plugin_a = (
        "def register(api):\n"
        "    api.register_tool('same', lambda ctx: 'a', description='', schema={})\n"
        "    api.register_tool('same', lambda ctx: 'b', description='', schema={})\n"
    )
    loaded, _, _ = _prepare_extension(tmp_path, "collider", plugin_a, permissions=["tool"])
    err = extension_loader.load_extension(loaded, lambda: {})
    assert err is not None
    assert "already registered" in err
    # Collision raised mid-registration must tear down the first tool too.
    assert extension_loader.snapshot()["tools"] == []
