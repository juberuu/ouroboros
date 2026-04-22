"""Phase 4 regression tests for ``ouroboros.extension_loader``.

Covers PluginAPI surface: register_tool / register_route /
register_ws_handler / register_ui_tab + permission gating +
namespace enforcement + unload cleanup.
"""
from __future__ import annotations

import json
import pathlib
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
    save_enabled,
    save_review_state,
)


@pytest.fixture(autouse=True)
def _clear_loader_state():
    """Reset the module-level registries between tests."""
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


def _write_ext_skill(
    repo_root: pathlib.Path,
    name: str,
    *,
    plugin_body: str,
    permissions: list[str],
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
            "description: Phase 4 extension.\n"
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
    (skill_dir / "plugin.py").write_text(plugin_body, encoding="utf-8")
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
    tool = extension_loader.get_tool("ext.ext1.echo")
    assert tool is not None
    assert tool["name"] == "ext.ext1.echo"
    assert callable(tool["handler"])


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
    assert "ext.ws1.message" in handlers


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


def test_get_settings_respects_allowlist_and_denylist(tmp_path):
    """get_settings only returns keys in BOTH the manifest allowlist AND
    NOT in FORBIDDEN_EXTENSION_SETTINGS."""
    plugin = (
        "def register(api):\n"
        "    api.register_tool('n', lambda ctx: 'ok', description='n', schema={})\n"
    )
    loaded, _, _ = _prepare_extension(
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
    err = extension_loader.load_extension(loaded, lambda: settings_snapshot)
    assert err is None, err

    impl = extension_loader.PluginAPIImpl(
        skill_name="envtest",
        permissions=["read_settings"],
        env_allowlist=["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK"],
        state_dir=tmp_path,
        settings_reader=lambda: settings_snapshot,
    )
    got = impl.get_settings(["OPENROUTER_API_KEY", "TIMEZONE", "MY_OK", "RANDOM_OTHER"])
    # Forbidden key dropped:
    assert "OPENROUTER_API_KEY" not in got
    # Allowed non-secret key surfaced:
    assert got["TIMEZONE"] == "UTC"
    assert got["MY_OK"] == "visible"
    # Not in allowlist → not returned:
    assert "RANDOM_OTHER" not in got


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
        # Any reference to ``reload_all`` imported from
        # ouroboros.extension_loader counts.
        if isinstance(node, ast.ImportFrom) and node.module == "ouroboros.extension_loader":
            imported = {alias.name for alias in node.names}
            if "reload_all" in imported or any(
                alias.asname and "reload" in (alias.asname or "") for alias in node.names
            ):
                return
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "extension_loader" and node.attr == "reload_all":
                return
    assert False, (
        "server.py does not wire extension_loader.reload_all into startup — "
        "enabled extensions would not survive a process restart."
    )


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
    parent_key = "ouroboros._extensions.tree_ext"
    child_key = "ouroboros._extensions.tree_ext.helper"
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
