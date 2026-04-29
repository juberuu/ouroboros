"""v5.1.2 regression tests for the runtime_mode self-elevation ratchet.

Covers the four mechanical layers introduced to make ``OUROBOROS_RUNTIME_MODE``
owner-only:

1. ``ouroboros.config.save_settings`` chokepoint refuses elevation without
   ``allow_elevation=True`` (compares on-disk old vs incoming new mode).
2. ``ouroboros.tools.core._data_write`` refuses writes whose resolved
   absolute path matches ``SETTINGS_PATH`` (handles symlinks /
   case-insensitive filesystems).
3. ``server.py::_merge_settings_payload`` drops ``OUROBOROS_RUNTIME_MODE``
   from the API body so a loopback POST cannot raise the agent's
   privilege scope (with belt-and-braces ``api_settings_post`` revert).
4. ``_set_tool_timeout`` (the live-flip chain that bypasses /api/settings)
   no longer propagates a corrupted-disk runtime_mode into env once the
   chokepoint refuses the corrupting save in the first place.

Plus an onboarding-flow positive: launcher / wizard paths can set any
initial mode via ``allow_elevation=True``.

Hermetic — no network, no supervisor boot. Uses temp dirs for
``DATA_DIR`` / ``SETTINGS_PATH`` overrides via monkeypatching
``ouroboros.config`` module-level constants.
"""
from __future__ import annotations

import json
import os
import pathlib
import types

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Point ``SETTINGS_PATH`` and ``DATA_DIR`` at a fresh temp dir so each
    test starts with no on-disk settings.json. The fixture monkeypatches
    the module-level constants; downstream modules that import
    ``SETTINGS_PATH`` at module load (e.g., ``ouroboros.tools.core``) get
    the live patched value through ``ouroboros.config.SETTINGS_PATH``.

    Also clears ``_BOOT_RUNTIME_MODE`` between tests so each case starts
    with a fresh baseline. Tests that need a pinned boot baseline call
    ``initialize_runtime_mode_baseline`` explicitly.
    """
    from ouroboros import config as cfg

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings_path = data_dir / "settings.json"

    monkeypatch.setattr(cfg, "DATA_DIR", data_dir, raising=True)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path, raising=True)
    # Lock file path is derived from SETTINGS_PATH at call time; refresh it.
    monkeypatch.setattr(cfg, "_SETTINGS_LOCK", pathlib.Path(str(settings_path) + ".lock"), raising=True)
    cfg.reset_runtime_mode_baseline_for_tests()
    yield settings_path
    cfg.reset_runtime_mode_baseline_for_tests()


def _seed_disk(settings_path: pathlib.Path, payload: dict) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. save_settings chokepoint
# ---------------------------------------------------------------------------


def test_save_settings_refuses_elevation_without_consent(isolated_settings):
    """Disk has light. Caller tries to save advanced without consent. Refused."""
    from ouroboros.config import save_settings

    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})

    with pytest.raises(PermissionError) as exc:
        save_settings({"OUROBOROS_RUNTIME_MODE": "advanced"})
    assert "elevation refused" in str(exc.value)
    assert "light" in str(exc.value) and "advanced" in str(exc.value)
    # On-disk value must NOT have been changed.
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert on_disk["OUROBOROS_RUNTIME_MODE"] == "light"


def test_save_settings_refuses_pro_elevation_from_advanced(isolated_settings):
    from ouroboros.config import save_settings

    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "advanced"})

    with pytest.raises(PermissionError):
        save_settings({"OUROBOROS_RUNTIME_MODE": "pro"})


def test_save_settings_allows_elevation_with_explicit_flag(isolated_settings):
    """Owner-driven flow (launcher, onboarding, lifespan) passes ``allow_elevation=True``."""
    from ouroboros.config import save_settings

    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    save_settings(
        {"OUROBOROS_RUNTIME_MODE": "advanced", "OPENAI_API_KEY": "irrelevant"},
        allow_elevation=True,
    )
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert on_disk["OUROBOROS_RUNTIME_MODE"] == "advanced"


def test_save_settings_allows_downgrade_without_consent(isolated_settings):
    """Lowering scope is always free."""
    from ouroboros.config import save_settings

    for old_mode, new_mode in (("pro", "advanced"), ("pro", "light"), ("advanced", "light")):
        _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": old_mode})
        save_settings({"OUROBOROS_RUNTIME_MODE": new_mode})
        on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
        assert on_disk["OUROBOROS_RUNTIME_MODE"] == new_mode


def test_save_settings_allows_same_mode(isolated_settings):
    """No elevation when in == out."""
    from ouroboros.config import save_settings

    for mode in ("light", "advanced", "pro"):
        _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": mode})
        save_settings({"OUROBOROS_RUNTIME_MODE": mode, "TOTAL_BUDGET": "42.0"})
        on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
        assert on_disk["OUROBOROS_RUNTIME_MODE"] == mode
        assert on_disk["TOTAL_BUDGET"] == "42.0"


def test_save_settings_initial_setup_uses_default_baseline(isolated_settings):
    """No on-disk settings yet -> baseline is the default ('advanced').
    Saving 'advanced' is same-mode; saving 'pro' would be elevation."""
    from ouroboros.config import save_settings

    # Initial advanced save (default baseline -> same mode).
    save_settings({"OUROBOROS_RUNTIME_MODE": "advanced"})
    assert isolated_settings.exists()
    # Initial pro save (default baseline -> elevation, blocked without consent).
    isolated_settings.unlink()
    with pytest.raises(PermissionError):
        save_settings({"OUROBOROS_RUNTIME_MODE": "pro"})


# ---------------------------------------------------------------------------
# 2. _data_write block on settings.json
# ---------------------------------------------------------------------------


def _make_drive_ctx(tmp_path):
    """Minimal ToolContext pointing drive_root at tmp_path/data."""
    from ouroboros.tools.registry import ToolContext

    drive_root = tmp_path / "data"
    drive_root.mkdir(exist_ok=True)
    return ToolContext(repo_dir=tmp_path / "repo", drive_root=drive_root)


def test_data_write_blocks_settings_json(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    settings_path = drive_root / "settings.json"
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(ctx, "settings.json", json.dumps({"OUROBOROS_RUNTIME_MODE": "pro"}))
    assert "DATA_WRITE_BLOCKED" in result
    assert "settings.json" in result
    # File must NOT have been written.
    assert not settings_path.exists()


def test_data_write_blocks_skill_grants_json(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", drive_root, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(
        ctx,
        "state/skills/weather/grants.json",
        json.dumps({"granted_keys": ["OPENROUTER_API_KEY"]}),
    )
    assert "DATA_WRITE_BLOCKED" in result
    assert "skill review" in result
    assert not (drive_root / "state" / "skills" / "weather" / "grants.json").exists()


@pytest.mark.parametrize("filename", ["review.json", "enabled.json", "clawhub.json"])
def test_data_write_blocks_skill_trust_state_json(filename, tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", drive_root, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(
        ctx,
        f"state/skills/weather/{filename}",
        json.dumps({"status": "pass", "enabled": True}),
    )
    assert "DATA_WRITE_BLOCKED" in result
    assert not (drive_root / "state" / "skills" / "weather" / filename).exists()


def test_data_write_blocks_skill_grants_case_variants(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", drive_root, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(
        ctx,
        "State/Skills/weather/grants.json",
        json.dumps({"granted_keys": ["OPENROUTER_API_KEY"]}),
    )
    assert "DATA_WRITE_BLOCKED" in result
    assert not (drive_root / "State" / "Skills" / "weather" / "grants.json").exists()


def test_data_write_blocks_skill_trust_state_under_symlinked_skill_dir(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    link_target = drive_root / "memory" / "linkstate"
    link_target.mkdir(parents=True)
    skills_root = drive_root / "state" / "skills"
    skills_root.mkdir(parents=True)
    try:
        (skills_root / "weather").symlink_to(link_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this filesystem")
    monkeypatch.setattr(cfg, "DATA_DIR", drive_root, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(ctx, "state/skills/weather/review.json", json.dumps({"status": "pass"}))
    assert "DATA_WRITE_BLOCKED" in result
    assert not (link_target / "review.json").exists()

    backing_result = _data_write(ctx, "memory/linkstate/enabled.json", json.dumps({"enabled": True}))
    assert "DATA_WRITE_BLOCKED" in backing_result
    assert not (link_target / "enabled.json").exists()


def test_data_write_allows_other_data_files(tmp_path, monkeypatch):
    """Defense doesn't break legitimate data writes."""
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    monkeypatch.setattr(cfg, "SETTINGS_PATH", drive_root / "settings.json", raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(ctx, "memory/scratchpad.md", "hello world")
    assert "DATA_WRITE_BLOCKED" not in result
    assert (drive_root / "memory" / "scratchpad.md").read_text(encoding="utf-8") == "hello world"


def test_data_write_blocks_settings_via_symlink(tmp_path, monkeypatch):
    """Symlink obfuscation: agent writes to ``alias.json`` which points to settings.json."""
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    settings_path = drive_root / "settings.json"
    settings_path.write_text("{}", encoding="utf-8")  # exist so symlink resolves
    alias_path = drive_root / "alias.json"
    try:
        alias_path.symlink_to(settings_path)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this filesystem (Windows non-admin?)")
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(ctx, "alias.json", json.dumps({"OUROBOROS_RUNTIME_MODE": "pro"}))
    assert "DATA_WRITE_BLOCKED" in result


def test_data_write_blocks_settings_via_env_override(tmp_path, monkeypatch):
    """OUROBOROS_SETTINGS_PATH override: SETTINGS_PATH is computed at module
    load, so monkeypatch the live constant directly."""
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    relocated = drive_root / "deep" / "alt-settings.json"
    relocated.parent.mkdir(parents=True)
    monkeypatch.setattr(cfg, "SETTINGS_PATH", relocated, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(ctx, "deep/alt-settings.json", "{}")
    assert "DATA_WRITE_BLOCKED" in result


# ---------------------------------------------------------------------------
# 3. /api/settings drops OUROBOROS_RUNTIME_MODE from the body
# ---------------------------------------------------------------------------


def test_merge_settings_payload_skips_runtime_mode():
    """``_merge_settings_payload`` is the chokepoint for /api/settings POST."""
    import server as server_mod

    old = {"OUROBOROS_RUNTIME_MODE": "light", "OPENAI_API_KEY": "old-key"}
    body = {"OUROBOROS_RUNTIME_MODE": "pro", "OPENAI_API_KEY": "new-key"}
    merged = server_mod._merge_settings_payload(old, body)
    # Mode comes from old (= disk), NOT from body.
    assert merged["OUROBOROS_RUNTIME_MODE"] == "light"
    # Other keys still flow through.
    assert merged["OPENAI_API_KEY"] == "new-key"


def test_merge_settings_payload_preserves_other_keys():
    """Sanity: dropping runtime_mode didn't accidentally drop everything else."""
    import server as server_mod

    old = {"OUROBOROS_RUNTIME_MODE": "advanced", "TOTAL_BUDGET": "10.0"}
    body = {"TOTAL_BUDGET": "20.0", "OUROBOROS_REVIEW_ENFORCEMENT": "blocking"}
    merged = server_mod._merge_settings_payload(old, body)
    assert merged["TOTAL_BUDGET"] == "20.0"
    assert merged["OUROBOROS_REVIEW_ENFORCEMENT"] == "blocking"
    assert merged["OUROBOROS_RUNTIME_MODE"] == "advanced"


# ---------------------------------------------------------------------------
# 4. set_tool_timeout regression: cannot propagate a poisoned disk mode
# ---------------------------------------------------------------------------


def test_set_tool_timeout_cannot_smuggle_elevation(isolated_settings, monkeypatch):
    """Belt-and-braces regression: if a (theoretical) bypass of the
    data_write block ever lands a corrupted runtime_mode on disk, the
    save_settings chokepoint inside _set_tool_timeout still refuses to
    write it back. The function reads disk, modifies timeout only,
    saves — but the save raises PermissionError when the in-memory dict
    carries an elevated mode that the on-disk baseline does not.
    """
    from ouroboros.config import load_settings
    from ouroboros.tools import control as control_mod

    # Step 1: legitimate baseline = light.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    # Step 2: simulate corruption (this is what the attack chain WOULD do):
    #   data_write block now refuses, but if it ever got around it, the
    #   in-memory dict that _set_tool_timeout builds would be:
    #     {OUROBOROS_RUNTIME_MODE: 'advanced', OUROBOROS_TOOL_TIMEOUT_SEC: N}
    #   Manually craft that dict and feed it to save_settings — it must raise.
    from ouroboros.config import save_settings
    poisoned = {"OUROBOROS_RUNTIME_MODE": "advanced", "OUROBOROS_TOOL_TIMEOUT_SEC": 600}
    with pytest.raises(PermissionError):
        save_settings(poisoned)

    # Disk remains at light.
    assert json.loads(isolated_settings.read_text())["OUROBOROS_RUNTIME_MODE"] == "light"

    # And the legitimate _set_tool_timeout flow (load -> mutate timeout
    # -> save) still works because load_settings preserves the on-disk
    # mode unchanged, so the chokepoint sees no elevation.
    settings = load_settings()
    settings["OUROBOROS_TOOL_TIMEOUT_SEC"] = 600
    save_settings(settings)  # no PermissionError
    # JSON preserves the int type — compare against int, not str.
    assert json.loads(isolated_settings.read_text())["OUROBOROS_TOOL_TIMEOUT_SEC"] == 600


# ---------------------------------------------------------------------------
# 5. Onboarding can set initial mode via allow_elevation
# ---------------------------------------------------------------------------


def test_onboarding_can_set_initial_runtime_mode_pro(isolated_settings):
    """First-launch wizard / launcher can choose any starting mode via
    the explicit consent flag."""
    from ouroboros.config import save_settings

    save_settings({"OUROBOROS_RUNTIME_MODE": "pro"}, allow_elevation=True)
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert on_disk["OUROBOROS_RUNTIME_MODE"] == "pro"


def test_launcher_runtime_mode_bridge_saves_after_confirmation(monkeypatch):
    import launcher

    saved = {}
    monkeypatch.setattr(launcher, "_load_settings", lambda: {"OUROBOROS_RUNTIME_MODE": "advanced"})
    monkeypatch.setattr(launcher, "_save_settings", lambda settings: saved.update(settings))

    result = launcher._request_runtime_mode_change("pro", lambda _title, _message: True)

    assert result["ok"] is True
    assert result["runtime_mode"] == "pro"
    assert result["restart_required"] is True
    assert saved["OUROBOROS_RUNTIME_MODE"] == "pro"


def test_launcher_skill_key_grant_validates_review_and_manifest(monkeypatch, tmp_path):
    import launcher

    class _Manifest:
        env_from_settings = ["OPENROUTER_API_KEY"]
        def is_script(self):
            return True
        def is_extension(self):
            return False

    class _Review:
        status = "pass"
        def is_stale_for(self, _hash):
            return False

    loaded = types.SimpleNamespace(
        name="demo",
        manifest=_Manifest(),
        review=_Review(),
        content_hash="hash-a",
    )
    captured = {}
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(launcher, "_load_settings", lambda: {"OUROBOROS_SKILLS_REPO_PATH": ""})
    monkeypatch.setattr("ouroboros.skill_loader.find_skill", lambda *_a, **_kw: loaded)
    monkeypatch.setattr(
        "ouroboros.skill_loader.save_skill_grants",
        lambda drive, name, keys, **kw: captured.update(
            {"drive": drive, "name": name, "keys": keys, **kw}
        ),
    )

    result = launcher._request_skill_key_grant(
        "demo",
        ["OPENROUTER_API_KEY"],
        lambda _title, _message: True,
    )

    assert result["ok"] is True
    assert captured["name"] == "demo"
    assert captured["keys"] == ["OPENROUTER_API_KEY"]
    assert captured["content_hash"] == "hash-a"
    assert captured["requested_keys"] == ["OPENROUTER_API_KEY"]
    # v5.2.2: scripts pick up grants on next ``_scrub_env`` call so no
    # server reconcile is invoked. ``extension_action`` and
    # ``extension_reason`` therefore stay ``None`` for script-type
    # skills.
    assert result.get("extension_action") is None
    assert result.get("extension_reason") is None


def test_launcher_skill_key_grant_supports_extensions(monkeypatch, tmp_path):
    """v5.2.2 dual-track grants: ``type: extension`` skills can be
    granted core keys and the launcher posts to the agent server's
    /api/skills/<name>/reconcile so the new grant reaches the live
    plugin without forcing a manual disable/enable.

    The launcher and server are independent OS processes — this test
    verifies the cross-process contract by stubbing ``urllib.request.urlopen``
    instead of stubbing ``reconcile_extension`` directly (which only
    runs in the launcher process and would not affect the server).
    """
    import launcher
    from io import BytesIO

    class _Manifest:
        env_from_settings = ["OPENROUTER_API_KEY"]
        def is_script(self):
            return False
        def is_extension(self):
            return True

    class _Review:
        status = "pass"
        def is_stale_for(self, _hash):
            return False

    loaded = types.SimpleNamespace(
        name="demo_ext",
        manifest=_Manifest(),
        review=_Review(),
        content_hash="ext-hash",
    )
    captured: dict = {}
    reconcile_calls: list = []
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(launcher, "_load_settings", lambda: {"OUROBOROS_SKILLS_REPO_PATH": ""})
    monkeypatch.setattr(launcher, "_read_port_file", lambda: 8765)
    monkeypatch.setattr("ouroboros.skill_loader.find_skill", lambda *_a, **_kw: loaded)
    monkeypatch.setattr(
        "ouroboros.skill_loader.save_skill_grants",
        lambda drive, name, keys, **kw: captured.update(
            {"drive": drive, "name": name, "keys": keys, **kw}
        ),
    )

    class _FakeResponse:
        def __init__(self, body: bytes):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False

    def _fake_urlopen(req, timeout=10):
        reconcile_calls.append({
            "url": req.full_url,
            "method": req.get_method(),
            "data": req.data,
        })
        return _FakeResponse(
            b'{"skill":"demo_ext","extension_action":"extension_loaded",'
            b'"extension_reason":"ready","live_loaded":true,"load_error":null}'
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    result = launcher._request_skill_key_grant(
        "demo_ext",
        ["OPENROUTER_API_KEY"],
        lambda _title, _message: True,
    )

    assert result["ok"] is True
    assert captured["name"] == "demo_ext"
    assert captured["keys"] == ["OPENROUTER_API_KEY"]
    assert len(reconcile_calls) == 1
    call = reconcile_calls[0]
    assert call["url"] == "http://127.0.0.1:8765/api/skills/demo_ext/reconcile"
    assert call["method"] == "POST"
    assert result.get("extension_action") == "extension_loaded"


def test_launcher_skill_key_grant_handles_reconcile_http_error(monkeypatch, tmp_path):
    """If the server-side reconcile HTTP call fails, the grant write
    succeeded but the response carries ``extension_reason='reconcile_call_failed'``
    so the UI can warn the user without throwing away the persisted grant."""
    import launcher

    class _Manifest:
        env_from_settings = ["OPENROUTER_API_KEY"]
        def is_script(self):
            return False
        def is_extension(self):
            return True

    class _Review:
        status = "pass"
        def is_stale_for(self, _hash):
            return False

    loaded = types.SimpleNamespace(
        name="demo_ext",
        manifest=_Manifest(),
        review=_Review(),
        content_hash="ext-hash",
    )
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(launcher, "_load_settings", lambda: {"OUROBOROS_SKILLS_REPO_PATH": ""})
    monkeypatch.setattr(launcher, "_read_port_file", lambda: 8765)
    monkeypatch.setattr("ouroboros.skill_loader.find_skill", lambda *_a, **_kw: loaded)
    monkeypatch.setattr(
        "ouroboros.skill_loader.save_skill_grants",
        lambda *_a, **_kw: None,
    )

    def _broken_urlopen(*_a, **_kw):
        raise ConnectionError("server not reachable")

    monkeypatch.setattr("urllib.request.urlopen", _broken_urlopen)

    result = launcher._request_skill_key_grant(
        "demo_ext",
        ["OPENROUTER_API_KEY"],
        lambda _title, _message: True,
    )

    # Grant itself succeeded (file persisted)
    assert result["ok"] is True
    assert result.get("granted_keys") == ["OPENROUTER_API_KEY"]
    # But the server reconcile failed and the UI is told
    assert result.get("extension_reason") == "reconcile_call_failed"
    assert result.get("extension_action") is None


def test_launcher_skill_key_grant_rejects_instruction_skill(monkeypatch, tmp_path):
    import launcher

    class _Manifest:
        env_from_settings = ["OPENROUTER_API_KEY"]
        def is_script(self):
            return False
        def is_extension(self):
            return False

    class _Review:
        status = "pass"
        def is_stale_for(self, _hash):
            return False

    loaded = types.SimpleNamespace(
        name="instr",
        manifest=_Manifest(),
        review=_Review(),
        content_hash="instr-hash",
    )
    monkeypatch.setattr(launcher, "DATA_DIR", tmp_path)
    monkeypatch.setattr(launcher, "_load_settings", lambda: {"OUROBOROS_SKILLS_REPO_PATH": ""})
    monkeypatch.setattr("ouroboros.skill_loader.find_skill", lambda *_a, **_kw: loaded)

    result = launcher._request_skill_key_grant(
        "instr",
        ["OPENROUTER_API_KEY"],
        lambda _title, _message: True,
    )
    assert result["ok"] is False
    assert "script and extension" in result["error"]


# ---------------------------------------------------------------------------
# 6. macOS APFS / Windows NTFS case-insensitive filesystem bypass
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "variant",
    [
        "Settings.json",
        "SETTINGS.JSON",
        "settings.JSON",
        "SettiNgs.json",
    ],
)
def test_data_write_blocks_settings_case_variants(variant, tmp_path, monkeypatch):
    """Adversarial-review iteration 1 (Gemini/GPT, verified empirically): on
    case-insensitive filesystems (APFS, NTFS) ``os.path.normcase`` is a
    no-op on darwin, so the previous string-equality compare let
    ``data_write("Settings.json", ...)`` route around the chokepoint
    even though the filesystem wrote to the same inode. The
    ``Path.samefile`` + case-insensitive name-compare fallback closes
    this. Parametrize over multiple case variants so a future regression
    that touches only one branch is caught."""
    from ouroboros import config as cfg
    from ouroboros.tools.core import _data_write

    drive_root = tmp_path / "data"
    drive_root.mkdir()
    settings_path = drive_root / "settings.json"
    monkeypatch.setattr(cfg, "SETTINGS_PATH", settings_path, raising=True)

    ctx = _make_drive_ctx(tmp_path)
    result = _data_write(ctx, variant, json.dumps({"OUROBOROS_RUNTIME_MODE": "pro"}))
    assert "DATA_WRITE_BLOCKED" in result, (
        f"Case variant {variant!r} bypassed the chokepoint. "
        "macOS APFS / Windows NTFS treat these as the same file; the "
        "block must too."
    )
    # On case-insensitive FS the file may exist (write went through
    # rejection path before opening). Ensure the actual on-disk
    # ``settings.json`` has not been written.
    if settings_path.exists():
        # We didn't seed it; if the chokepoint correctly refused the write,
        # this branch should be empty.
        assert "OUROBOROS_RUNTIME_MODE" not in settings_path.read_text()


# ---------------------------------------------------------------------------
# 7. Boot-time baseline closes the disk-corruption-then-roundtrip loophole
# ---------------------------------------------------------------------------


def test_save_settings_uses_boot_baseline_when_pinned(isolated_settings):
    """Once the boot baseline is pinned, the chokepoint compares against
    that fixed value — out-of-process disk corruption cannot move the
    fence."""
    from ouroboros.config import (
        initialize_runtime_mode_baseline,
        save_settings,
    )

    # Owner started the run in light.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    initialize_runtime_mode_baseline("light")

    # An out-of-process write corrupts disk to "pro" (simulated directly here).
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "pro"})

    # Now any in-process save_settings call that would propagate "pro"
    # (the chain through _set_tool_timeout / api_settings_post / etc.)
    # MUST be refused — even though disk old equals incoming, the boot
    # baseline of "light" still wins.
    with pytest.raises(PermissionError):
        save_settings({"OUROBOROS_RUNTIME_MODE": "pro"})


def test_set_tool_timeout_sanitizes_corrupted_disk_to_env(isolated_settings, monkeypatch):
    """End-to-end regression for the iteration-1 GPT/Gemini finding:
    even if a subprocess managed to write ``pro`` directly to
    settings.json (bypassing both ``_data_write`` and the chokepoint),
    a follow-up ``_set_tool_timeout`` call must NOT propagate the
    elevation into ``os.environ``. ``_set_tool_timeout`` now anchors
    ``OUROBOROS_RUNTIME_MODE`` to the live env value before saving, so
    the corrupted disk gets restored, not propagated."""
    from ouroboros.config import initialize_runtime_mode_baseline
    from ouroboros.tools import control as control_mod

    # Owner started the run in light.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    initialize_runtime_mode_baseline("light")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")

    # Adversary corrupts disk directly to "pro".
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "pro"})

    # Agent calls set_tool_timeout (innocent-looking, POLICY_SKIP).
    ctx = _make_drive_ctx(isolated_settings.parent.parent)
    result = control_mod._set_tool_timeout(ctx, 60)
    assert "OK:" in result, result

    # Disk should now be restored to "light" (the live env value),
    # NOT "pro" (the corrupted disk read).
    on_disk = json.loads(isolated_settings.read_text(encoding="utf-8"))
    assert on_disk["OUROBOROS_RUNTIME_MODE"] == "light", (
        "set_tool_timeout propagated corrupted disk back to disk "
        "instead of restoring from env."
    )

    # And the live env must still be light (apply_settings_to_env was
    # called with the sanitised dict).
    assert os.environ.get("OUROBOROS_RUNTIME_MODE") == "light", (
        "set_tool_timeout propagated corrupted disk into os.environ — "
        "elevation chain succeeded."
    )


# ---------------------------------------------------------------------------
# 8. Light-mode shell filter catches pathlib write_text / save_settings
#    subprocess imports (defense-in-depth complement to the chokepoint)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_cmd_substring",
    [
        ".write_text(",
        ".write_bytes(",
        "os.replace(",
        "os.rename(",
    ],
)
def test_light_mode_shell_filter_catches_pathlib_write_patterns(bad_cmd_substring):
    """Adversarial-review iteration 1 finding: ``Path.write_text(...)``,
    ``Path.write_bytes(...)``, and OS rename primitives are NOT
    substrings of the previous indicators (`.write(` does not match
    ``.write_text(`` because of the ``_text`` separator). Verify each
    is now in the light-mode mutation list. String-level test — no
    subprocess spawned."""
    import ouroboros.tools.registry as reg_mod
    source = pathlib.Path(reg_mod.__file__).read_text(encoding="utf-8")
    assert f'"{bad_cmd_substring}"' in source, (
        f"Light-mode mutation indicator {bad_cmd_substring!r} missing "
        "from registry.py — the substring filter would let a subprocess "
        "write to settings.json without tripping LIGHT_MODE_BLOCKED."
    )


@pytest.mark.parametrize(
    "blocked_cmd",
    [
        # Combination: save_settings + OUROBOROS_RUNTIME_MODE → blocked.
        "python -c \"from ouroboros.config import save_settings; save_settings({'OUROBOROS_RUNTIME_MODE': 'pro'}, allow_elevation=True)\"",
        "python3 -c \"import ouroboros.config; ouroboros.config.save_settings({'OUROBOROS_RUNTIME_MODE': 'pro'})\"",
        # Dotted-path short-circuit: ouroboros.config.save_settings.
        "python -c \"import ouroboros.config; ouroboros.config.save_settings({})\"",
    ],
)
def test_elevation_indicators_block_attack_patterns_in_all_modes(blocked_cmd, tmp_path, monkeypatch):
    """Iteration-2 fix (real triad finding T1, iter-2 multi-critic F2-6):
    the elevation indicators block actual attack patterns — runs
    ``ToolRegistry.execute("run_shell", ...)`` end-to-end in each
    runtime mode and asserts ``ELEVATION_BLOCKED`` is returned. The
    earlier string-level test only verified substring presence; this
    covers the dispatch wiring."""
    from ouroboros.tools.registry import ToolRegistry

    for mode in ("light", "advanced", "pro"):
        monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
        reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
        result = reg.execute("run_shell", {"cmd": blocked_cmd})
        assert "ELEVATION_BLOCKED" in result, (
            f"mode={mode!r} cmd={blocked_cmd!r}: "
            f"got {result[:200]!r}"
        )


@pytest.mark.parametrize(
    "diagnostic_cmd",
    [
        # Diagnostic queries about the chokepoint must NOT be blocked.
        "echo \"$OUROBOROS_RUNTIME_MODE\"",
        "printenv OUROBOROS_RUNTIME_MODE",
        "grep save_settings ouroboros/config.py",
        "rg save_settings ouroboros/",
        "git log -S save_settings",
        # save_settings without OUROBOROS_RUNTIME_MODE: legitimate dev work.
        "grep -n 'def save_settings' ouroboros/config.py",
    ],
)
def test_elevation_indicators_do_not_false_positive(diagnostic_cmd, tmp_path, monkeypatch):
    """Iteration-2 fix (multi-critic F2-2): diagnostic shell commands
    that mention ``save_settings`` OR ``OUROBOROS_RUNTIME_MODE`` (but
    not both, and not the dotted-path attack form) must NOT trip
    ELEVATION_BLOCKED. The conjunctive check is the discriminator."""
    from ouroboros.tools.registry import ToolRegistry

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    result = reg.execute("run_shell", {"cmd": diagnostic_cmd})
    assert "ELEVATION_BLOCKED" not in result, (
        f"Diagnostic cmd {diagnostic_cmd!r} was wrongly blocked as "
        "elevation attempt. The conjunctive check should let this pass."
    )


def test_save_settings_consent_inert_after_boot_baseline_pinned(isolated_settings):
    """Real triad finding T1: ``allow_elevation=True`` MUST become inert
    once the boot baseline is pinned. Otherwise an agent-reachable
    subprocess that imports ``save_settings`` could pass the public
    consent kwarg and bypass the rank check.

    Pre-pin: ``allow_elevation=True`` works (legitimate launcher /
    lifespan path).
    Post-pin: ``allow_elevation=True`` is silently demoted; rank check
    still applies and refuses the elevation.
    """
    from ouroboros.config import (
        initialize_runtime_mode_baseline,
        save_settings,
    )

    # Pre-pin path — owner-driven elevation works.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    save_settings({"OUROBOROS_RUNTIME_MODE": "pro"}, allow_elevation=True)
    assert json.loads(isolated_settings.read_text())["OUROBOROS_RUNTIME_MODE"] == "pro"

    # Pin the boot baseline to the legitimately-elevated mode (simulating
    # a real launcher run that came up in pro). Now further elevation
    # attempts must be refused — even with the consent flag.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    initialize_runtime_mode_baseline("light")
    with pytest.raises(PermissionError) as exc:
        save_settings({"OUROBOROS_RUNTIME_MODE": "pro"}, allow_elevation=True)
    # Error message must clarify that consent is inert after baseline pin.
    assert "boot baseline is pinned" in str(exc.value)


def test_initialize_baseline_prefers_inherited_boot_env_over_runtime_mode(isolated_settings, monkeypatch):
    """Iteration-2 real scope finding SR1: a fresh subprocess inherits
    BOTH ``OUROBOROS_RUNTIME_MODE`` and ``OUROBOROS_BOOT_RUNTIME_MODE``.
    The agent CAN clobber its own ``OUROBOROS_RUNTIME_MODE`` to poison
    the baseline, but ``OUROBOROS_BOOT_RUNTIME_MODE`` is owner-pinned
    and must take precedence in ``initialize_runtime_mode_baseline``."""
    from ouroboros import config as cfg
    from ouroboros.config import (
        BOOT_RUNTIME_MODE_ENV_KEY,
        initialize_runtime_mode_baseline,
        save_settings,
    )

    # Parent ran in light, exported the boot baseline via env.
    monkeypatch.setenv(BOOT_RUNTIME_MODE_ENV_KEY, "light")
    # Adversarial child mutates its own RUNTIME_MODE env to "pro" (this
    # is just the agent's own os.environ — no special privilege).
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")

    # Simulate fresh-subprocess module state (the in-memory pin is
    # absent because the module was just imported).
    monkeypatch.setattr(cfg, "_BOOT_RUNTIME_MODE", None, raising=True)

    # Init must read BOOT_RUNTIME_MODE_ENV_KEY (= "light"), NOT the
    # poisoned RUNTIME_MODE (= "pro"). Otherwise the child poisons its
    # own baseline and the chokepoint becomes ineffective.
    initialize_runtime_mode_baseline()
    assert cfg._BOOT_RUNTIME_MODE == "light", (
        "initialize_runtime_mode_baseline must prefer the inherited "
        "OUROBOROS_BOOT_RUNTIME_MODE over the (clobberable) "
        "OUROBOROS_RUNTIME_MODE."
    )

    # And the chokepoint should now refuse the elevation that the agent
    # tried to set up by poisoning RUNTIME_MODE.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    with pytest.raises(PermissionError):
        save_settings({"OUROBOROS_RUNTIME_MODE": "pro"}, allow_elevation=True)


def test_files_api_write_blocks_settings_json(isolated_settings, monkeypatch):
    """Iteration-2 real triad+scope finding SR2: the Files API
    (``/api/files/write``) is a parallel write path that previously
    bypassed both ``_data_write`` and the ``save_settings`` chokepoint.
    Verify the owner-only guard rejects
    writes to the owner-only file. String-level test against the source
    so the assertion is hermetic (full HTTP round-trip belongs in a
    Starlette TestClient suite, but the guard helper is the SSOT)."""
    from ouroboros import file_browser_api as fba_mod

    source = pathlib.Path(fba_mod.__file__).read_text(encoding="utf-8")
    # The shared helpers must exist...
    assert "_is_owner_only_settings_file" in source
    assert "_is_owner_only_file" in source
    # ...and must be invoked from each mutating endpoint.
    for endpoint in (
        "api_files_write",
        "api_files_delete",
        "api_files_transfer",
        "api_files_upload",
    ):
        endpoint_idx = source.find(f"async def {endpoint}(")
        assert endpoint_idx != -1, f"Endpoint {endpoint} not found"
        # Find the next async def boundary so we scope the guard search.
        next_idx = source.find("\nasync def ", endpoint_idx + 1)
        body = source[endpoint_idx:next_idx if next_idx != -1 else len(source)]
        assert "_is_owner_only_file" in body or "_contains_owner_only_file" in body, (
            f"Endpoint {endpoint} must call ``_is_owner_only_file`` "
            "to refuse writes/deletes/transfers/uploads against the "
            "owner-only settings.json and skill trust-state JSON. Otherwise the Files API is a "
            "parallel privilege-escalation channel."
        )


@pytest.mark.parametrize("filename", ["grants.json", "review.json", "enabled.json", "clawhub.json"])
def test_files_api_owner_only_helper_blocks_skill_state_case_variants(filename, tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros import file_browser_api as fba_mod

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir, raising=True)
    target = data_dir / "State" / "Skills" / "weather" / filename
    assert fba_mod._is_owner_only_file(target) is True


def test_files_api_owner_only_helper_blocks_symlinked_skill_state_dir(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros import file_browser_api as fba_mod

    data_dir = tmp_path / "data"
    link_target = data_dir / "memory" / "linkstate"
    link_target.mkdir(parents=True)
    skills_root = data_dir / "state" / "skills"
    skills_root.mkdir(parents=True)
    try:
        (skills_root / "weather").symlink_to(link_target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable on this filesystem")
    monkeypatch.setattr(cfg, "DATA_DIR", data_dir, raising=True)

    target = data_dir / "state" / "skills" / "weather" / "enabled.json"
    assert fba_mod._is_owner_only_file(target) is True
    backing_target = link_target / "review.json"
    assert fba_mod._is_owner_only_file(backing_target) is True


# ---------------------------------------------------------------------------
# 9. File-based subprocess bypass (v5.1.2 iter-3 fix)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["light", "advanced", "pro"])
def test_run_shell_blocks_script_file_with_save_settings_elevation(mode, tmp_path, monkeypatch):
    """Iteration-3 fix (file-based subprocess bypass): a helper script
    that imports save_settings and mentions OUROBOROS_RUNTIME_MODE
    must be blocked at run_shell entry, not after the script runs.
    The argv-level _ELEVATION_INDICATORS only sees ``python helper.py``
    in argv; without file-content scanning, the elevation chain
    succeeds in any mode (``data_write helper.py + run_shell python
    helper.py`` → settings.json poisoned → next restart picks up
    elevated mode)."""
    from ouroboros.tools.registry import ToolRegistry

    helper_path = tmp_path / "evil.py"
    helper_path.write_text(
        "from ouroboros.config import save_settings\n"
        "save_settings({'OUROBOROS_RUNTIME_MODE': 'pro'}, allow_elevation=True)\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", mode)
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    result = reg.execute("run_shell", {"cmd": ["python3", str(helper_path)]})
    assert "ELEVATION_BLOCKED" in result, (
        f"mode={mode}: file-based subprocess bypass NOT blocked. "
        f"Got: {result[:300]}"
    )
    # The block message must include the offending script path so the
    # operator can trace which file was refused.
    assert "evil.py" in result


def test_run_shell_in_light_blocks_script_file_with_pathlib_write(tmp_path, monkeypatch):
    """Light mode: a helper script that uses pathlib.Path.write_text
    to clobber settings.json directly does NOT need to import
    save_settings — but it still bypasses the in-process chokepoint
    once the file is on disk. Light-mode content scan must catch the
    ``.write_text(`` pattern in the file and refuse the run."""
    from ouroboros.tools.registry import ToolRegistry

    helper_path = tmp_path / "writer.py"
    helper_path.write_text(
        "import pathlib\n"
        "pathlib.Path('settings.json').write_text('{\"x\":1}')\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    result = reg.execute("run_shell", {"cmd": ["python3", str(helper_path)]})
    assert "LIGHT_MODE_BLOCKED" in result, (
        f"light: pathlib.write_text in script file not blocked. "
        f"Got: {result[:300]}"
    )
    assert "writer.py" in result


@pytest.mark.parametrize("filename", ["grants.json", "review.json", "enabled.json", "Review.JSON"])
def test_run_shell_blocks_obfuscated_skill_owner_state_write(filename, tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry

    drive_root = tmp_path / "data"
    skill_state_dir = drive_root / "state" / "skills" / "weather"
    skill_state_dir.mkdir(parents=True)
    helper_path = tmp_path / "owner_state_writer.py"
    stem, suffix = filename.split(".", 1)
    helper_path.write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        f"name = {stem!r} + '.{suffix}'\n"
        "target = root / 'state' / 'skills' / 'weather' / name\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "target.write_text(json.dumps({'status':'pass','enabled':True}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=drive_root)
    result = reg.execute("run_shell", {"cmd": ["python3", str(helper_path), str(drive_root)]})
    assert "SKILL_STATE_WRITE_BLOCKED" in result
    assert not (skill_state_dir / filename).exists()


def test_run_shell_blocks_delayed_skill_owner_state_writer(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    import sys
    import time

    drive_root = tmp_path / "data"
    skill_state_dir = drive_root / "state" / "skills" / "weather"
    skill_state_dir.mkdir(parents=True)
    child_code = (
        "import json, pathlib, sys, time\n"
        "time.sleep(1.0)\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "name = 'review' + '.json'\n"
        "target = root / 'state' / 'skills' / 'weather' / name\n"
        "target.write_text(json.dumps({'status':'pass'}))\n"
    )
    parent_code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[2], sys.argv[1]], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=drive_root)
    result = reg.execute("run_shell", {"cmd": [sys.executable, "-c", parent_code, str(drive_root), child_code]})
    assert "SKILL_STATE_WRITE_BLOCKED" in result
    time.sleep(1.4)
    assert not (skill_state_dir / "review.json").exists()


def test_run_shell_blocks_detached_skill_state_command(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    import sys

    drive_root = tmp_path / "data"
    (drive_root / "state" / "skills" / "weather").mkdir(parents=True)
    code = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'pass'], start_new_session=True)\n"
        "print('state skills')\n"
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=drive_root)
    result = reg.execute("run_shell", {"cmd": [sys.executable, "-c", code]})
    assert "SKILL_STATE_WRITE_BLOCKED" in result


def test_run_shell_scans_scripts_relative_to_cwd(tmp_path, monkeypatch):
    from ouroboros.tools.registry import ToolRegistry
    import sys

    repo_dir = tmp_path / "repo"
    subdir = repo_dir / "sub"
    subdir.mkdir(parents=True)
    drive_root = tmp_path / "data"
    (drive_root / "state" / "skills" / "weather").mkdir(parents=True)
    helper = subdir / "evil.py"
    helper.write_text(
        "import json, pathlib, sys\n"
        "root = pathlib.Path(sys.argv[1])\n"
        "name = 'review' + '.json'\n"
        "target = root / 'state' / 'skills' / 'weather' / name\n"
        "target.write_text(json.dumps({'status':'pass'}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=repo_dir, drive_root=drive_root)
    result = reg.execute("run_shell", {"cmd": [sys.executable, "evil.py", str(drive_root)], "cwd": "sub"})
    assert "SKILL_STATE_WRITE_BLOCKED" in result
    assert not (drive_root / "state" / "skills" / "weather" / "review.json").exists()


def test_run_shell_does_not_scan_files_outside_agent_areas(tmp_path, monkeypatch):
    """Negative: a helper script outside repo_dir AND drive_root
    must NOT be scanned (those files are not agent-writable, so the
    scan would only be a false-positive risk on system Python helper
    scripts the agent invokes legitimately)."""
    from ouroboros.tools.registry import ToolRegistry

    outside = tmp_path / "outside_data.py"
    outside.write_text(
        "from ouroboros.config import save_settings\n"
        "save_settings({'OUROBOROS_RUNTIME_MODE': 'pro'})\n",
        encoding="utf-8",
    )
    inner_repo = tmp_path / "repo"
    inner_repo.mkdir()
    inner_drive = tmp_path / "drive"
    inner_drive.mkdir()
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=inner_repo, drive_root=inner_drive)
    result = reg.execute("run_shell", {"cmd": ["python3", str(outside)]})
    assert "ELEVATION_BLOCKED" not in result, (
        "Files outside agent areas should not trigger content scan."
    )


@pytest.mark.parametrize(
    "benign_content",
    [
        "print('hello world')\n",
        "import os\nprint(os.getcwd())\n",
        "def main():\n    return 42\n\nif __name__ == '__main__':\n    print(main())\n",
    ],
)
def test_run_shell_does_not_false_positive_on_benign_script_files(benign_content, tmp_path, monkeypatch):
    """Negative: benign scripts inside the agent area must NOT trip
    the file-content scan. The conjunctive elevation check + the
    targeted light-mutation list keeps false-positive rate low."""
    from ouroboros.tools.registry import ToolRegistry

    helper_path = tmp_path / "benign.py"
    helper_path.write_text(benign_content, encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    result = reg.execute("run_shell", {"cmd": ["python3", str(helper_path)]})
    assert "ELEVATION_BLOCKED" not in result, (
        f"Benign content {benign_content!r} false-positive: {result[:200]}"
    )
    assert "LIGHT_MODE_BLOCKED" not in result, (
        f"Benign content {benign_content!r} false-positive in light filter: {result[:200]}"
    )


def test_run_shell_inline_python_dash_c_skips_file_scan(tmp_path, monkeypatch):
    """``python -c "..."`` has inline code in argv (already covered by
    argv-level checks); the file-scan must not try to interpret the
    ``-c`` argument as a file path."""
    from ouroboros.tools.registry import ToolRegistry

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)
    # Inline code that does NOT match the elevation pattern.
    result = reg.execute("run_shell", {"cmd": ["python3", "-c", "print(42)"]})
    assert "ELEVATION_BLOCKED" not in result
    assert "LIGHT_MODE_BLOCKED" not in result


def test_extract_script_file_args_recognises_common_interpreters():
    """Helper-level: confirm the parser walks past flags and stops at
    ``-c`` / ``-m``."""
    from ouroboros.tools.registry import _extract_script_file_args

    assert _extract_script_file_args(["python3", "evil.py"]) == ["evil.py"]
    assert _extract_script_file_args(["python", "-u", "-O", "evil.py"]) == ["evil.py"]
    assert _extract_script_file_args(["bash", "evil.sh"]) == ["evil.sh"]
    assert _extract_script_file_args(["node", "--inspect", "evil.js"]) == ["evil.js"]
    # ``-c`` / ``-m`` mean no file argument.
    assert _extract_script_file_args(["python3", "-c", "print(1)"]) == []
    assert _extract_script_file_args(["python3", "-m", "pytest"]) == []
    # Full-path interpreter still recognised.
    assert _extract_script_file_args(["/usr/bin/python3", "evil.py"]) == ["evil.py"]
    # Versioned interpreter (python3.10) recognised via prefix match.
    assert _extract_script_file_args(["python3.10", "evil.py"]) == ["evil.py"]
    # Non-interpreter top-level command — no script files extracted.
    assert _extract_script_file_args(["ls", "-la"]) == []


def test_save_settings_consent_inert_in_subprocess_via_env_propagation(isolated_settings, monkeypatch):
    """Iteration-2 multi-critic finding F2-1 (verified empirically by
    Gemini): a fresh subprocess that re-imports ``ouroboros.config``
    starts with ``_BOOT_RUNTIME_MODE = None``, which previously let
    ``allow_elevation=True`` work again, defeating the chokepoint. The
    fix exports the pinned baseline to ``OUROBOROS_BOOT_RUNTIME_MODE``
    env var so subprocesses inherit it. This test simulates the
    subprocess scenario by clearing the in-memory pin while keeping
    the env var (which is what a fresh subprocess sees)."""
    from ouroboros import config as cfg
    from ouroboros.config import (
        BOOT_RUNTIME_MODE_ENV_KEY,
        initialize_runtime_mode_baseline,
        save_settings,
    )

    # Parent pins the baseline → env var is set.
    _seed_disk(isolated_settings, {"OUROBOROS_RUNTIME_MODE": "light"})
    initialize_runtime_mode_baseline("light")
    assert os.environ.get(BOOT_RUNTIME_MODE_ENV_KEY) == "light"

    # Simulate a fresh subprocess: clear the in-memory module global
    # (this is what a re-imported module looks like) but keep the env
    # var (which subprocess.Popen / mp.spawn inherit).
    monkeypatch.setattr(cfg, "_BOOT_RUNTIME_MODE", None, raising=True)
    assert os.environ.get(BOOT_RUNTIME_MODE_ENV_KEY) == "light"

    # An attempt to elevate via ``allow_elevation=True`` from the
    # "subprocess" must be refused — env-inherited baseline takes over.
    with pytest.raises(PermissionError) as exc:
        save_settings({"OUROBOROS_RUNTIME_MODE": "pro"}, allow_elevation=True)
    assert "env-var" in str(exc.value), (
        "Subprocess save_settings must report the baseline source as "
        "'env-var' so the operator can trace which path refused."
    )
