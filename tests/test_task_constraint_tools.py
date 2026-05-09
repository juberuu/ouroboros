import pathlib

from ouroboros.contracts.task_constraint import TaskConstraint, resolve_payload_path
from ouroboros.tools.core import _data_write
from ouroboros.tools.git import _str_replace_editor
from ouroboros.tools.registry import ToolContext


def _ctx(tmp_path):
    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir()
    skill = drive / "skills" / "external" / "alpha"
    skill.mkdir(parents=True)
    return ToolContext(repo_dir=repo, drive_root=drive, task_constraint=TaskConstraint(mode="skill_repair", skill_name="alpha", payload_root="skills/external/alpha", allow_enable=False)), skill


def test_payload_relative_resolver_accepts_short_paths(tmp_path):
    ctx, skill = _ctx(tmp_path)
    assert resolve_payload_path(ctx.drive_root, ctx.task_constraint, "plugin.py") == skill / "plugin.py"
    assert resolve_payload_path(ctx.drive_root, ctx.task_constraint, "skills/external/alpha/plugin.py") == skill / "plugin.py"


def test_str_replace_editor_uses_payload_relative_path(tmp_path):
    ctx, skill = _ctx(tmp_path)
    target = skill / "plugin.py"
    target.write_text("hello = 1\n", encoding="utf-8")
    result = _str_replace_editor(ctx, "plugin.py", "hello = 1", "hello = 2")
    assert "Replaced" in result
    assert target.read_text(encoding="utf-8") == "hello = 2\n"
    assert not (ctx.repo_dir / "plugin.py").exists()


def test_data_write_uses_payload_relative_path(tmp_path):
    ctx, skill = _ctx(tmp_path)
    result = _data_write(ctx, "new_file.py", "VALUE = 1\n")
    assert "OK:" in result
    assert (skill / "new_file.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_data_read_and_list_use_payload_relative_paths(tmp_path):
    from ouroboros.tools.core import _data_list, _data_read
    ctx, skill = _ctx(tmp_path)
    (skill / "plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ctx.drive_root / "memory").mkdir()
    (ctx.drive_root / "memory" / "identity.md").write_text("secret\n", encoding="utf-8")

    assert "VALUE = 1" in _data_read(ctx, "plugin.py")
    listing = _data_list(ctx, ".")
    assert "plugin.py" in listing
    assert "secret" not in _data_read(ctx, "memory/identity.md")


def test_payload_absolute_other_skill_path_is_blocked(tmp_path):
    from ouroboros.tools.core import _data_read
    ctx, _skill = _ctx(tmp_path)
    assert "DATA_READ_BLOCKED" in _data_read(ctx, "skills/external/beta/plugin.py")


def test_repair_mode_blocks_code_search(tmp_path):
    from ouroboros.tools.registry import ToolRegistry
    ctx, _skill = _ctx(tmp_path)
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry._ctx = ctx
    result = registry.execute("code_search", {"query": "ToolRegistry"})
    assert "HEAL_MODE_BLOCKED" in result


def test_claude_code_edit_reverts_repair_sidecars(tmp_path, monkeypatch):
    from types import ModuleType, SimpleNamespace
    import sys
    from ouroboros.tools.shell import _claude_code_edit

    gateway = ModuleType("ouroboros.gateways.claude_code")
    gateway.resolve_claude_code_model = lambda: "test-model"
    gateway.DEFAULT_CLAUDE_CODE_MAX_TURNS = 1
    sys.modules["ouroboros.gateways.claude_code"] = gateway

    ctx, skill = _ctx(tmp_path)
    sidecar = skill / ".self_authored.json"
    sidecar.write_text("original", encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    def fake_run_edit(**kwargs):
        sidecar.write_text("modified", encoding="utf-8")
        return SimpleNamespace(
            success=True,
            error="",
            result_text="ok",
            cost_usd=0.0,
            usage={},
            changed_files=[],
            diff_stat="",
            validation_summary="",
            to_tool_output=lambda: "OK",
        )

    gateway.run_edit = fake_run_edit

    result = _claude_code_edit(ctx, "edit", cwd=".")

    assert "HEAL_MODE_BLOCKED" in result
    assert sidecar.read_text(encoding="utf-8") == "original"


def test_repair_data_write_manifest_does_not_create_self_authored_markers(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    ctx, skill = _ctx(tmp_path)
    monkeypatch.setattr(cfg, "DATA_DIR", ctx.drive_root)
    result = _data_write(ctx, "SKILL.md", "---\nname: alpha\ndescription: x\nversion: 0.1\ntype: instruction\n---\n")
    assert "OK:" in result
    assert not (skill / ".self_authored.json").exists()
    assert not (ctx.drive_root / "state" / "skills" / "alpha" / "self_authored.json").exists()


def test_payload_root_must_match_skill_name(tmp_path):
    bad = TaskConstraint(mode="skill_repair", skill_name="alpha", payload_root="skills/external/beta")
    try:
        resolve_payload_path(tmp_path / "data", bad, "plugin.py")
    except ValueError as exc:
        assert "does not match" in str(exc)
    else:
        raise AssertionError("mismatched skill_name/payload_root was accepted")


def test_light_mode_allows_constrained_str_replace_editor_payload_edit(tmp_path, monkeypatch):
    from ouroboros import config as cfg
    from ouroboros.tools.registry import ToolRegistry

    ctx, skill = _ctx(tmp_path)
    target = skill / "plugin.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    registry = ToolRegistry(repo_dir=ctx.repo_dir, drive_root=ctx.drive_root)
    registry._ctx = ctx
    monkeypatch.setattr(cfg, "get_runtime_mode", lambda: "light")

    result = registry.execute(
        "str_replace_editor",
        {"path": "plugin.py", "old_str": "VALUE = 1", "new_str": "VALUE = 2"},
    )

    assert "LIGHT_MODE_BLOCKED" not in result
    assert "Replaced" in result
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"
