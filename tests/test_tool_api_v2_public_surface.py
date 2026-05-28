import pathlib
import re

from ouroboros.tools.registry import ToolRegistry


LEGACY_PUBLIC_TOOL_NAMES = {
    "repo_read",
    "repo_write",
    "repo_list",
    "str_replace_editor",
    "data_read",
    "data_write",
    "data_list",
    "code_search",
    "run_shell",
    "git_status",
    "git_diff",
    "repo_commit",
    "restore_to_head",
    "revert_commit",
    "rollback_to_target",
    "schedule_task",
    "wait_for_task",
    "wait_for_tasks",
    "advisory_pre_review",
    "review_skill",
    "multi_model_review",
}


def test_legacy_tool_names_are_not_public_schemas(tmp_path):
    registry = ToolRegistry(repo_dir=tmp_path / "repo", drive_root=tmp_path / "data")
    names = {schema["function"]["name"] for schema in registry.schemas()}

    assert names.isdisjoint(LEGACY_PUBLIC_TOOL_NAMES)
    for name in LEGACY_PUBLIC_TOOL_NAMES:
        assert registry.get_schema_by_name(name) is None
        assert registry.execute(name, {}).startswith("⚠️ Unknown tool")

    assert {
        "read_file",
        "write_file",
        "search_code",
        "run_command",
        "claude_code_edit",
        "commit_reviewed",
        "schedule_subagent",
        "skill_review",
        "task_acceptance_review",
    } <= names


def test_runtime_prompts_do_not_advertise_legacy_public_tool_names():
    root = pathlib.Path(__file__).resolve().parent.parent
    prompt_text = "\n".join(
        (root / path).read_text(encoding="utf-8")
        for path in ("prompts/SYSTEM.md", "prompts/SAFETY.md", "prompts/CONSCIOUSNESS.md")
    )
    for name in LEGACY_PUBLIC_TOOL_NAMES:
        assert re.search(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])", prompt_text) is None, name
    for name in (
        "read_file",
        "run_command",
        "commit_reviewed",
        "advisory_review",
        "schedule_subagent",
        "task_acceptance_review",
    ):
        assert name in prompt_text


def test_frozen_registry_includes_service_tools(monkeypatch, tmp_path):
    monkeypatch.setattr(__import__("sys"), "frozen", True, raising=False)
    registry = ToolRegistry(repo_dir=pathlib.Path(tmp_path), drive_root=pathlib.Path(tmp_path))
    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert {"start_service", "service_status", "service_logs", "stop_service"} <= names


def test_skill_payload_root_rejects_bucket_skill_traversal(tmp_path):
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    (data / "settings.json").parent.mkdir(parents=True)
    (data / "settings.json").write_text("secret", encoding="utf-8")
    registry = ToolRegistry(repo_dir=repo, drive_root=data)

    result = registry.execute(
        "read_file",
        {"root": "skill_payload", "bucket": "external", "skill_name": "../../settings.json", "path": "."},
    )

    assert "READ_FILE_ERROR" in result or "TOOL_ARG_ERROR" in result
    assert "secret" not in result


def test_skill_payload_write_named_bible_is_not_system_protected(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    (data / "skills" / "external" / "alpha").mkdir(parents=True)
    registry = ToolRegistry(repo_dir=repo, drive_root=data)

    result = registry.execute(
        "write_file",
        {
            "root": "skill_payload",
            "bucket": "external",
            "skill_name": "alpha",
            "path": "BIBLE.md",
            "content": "skill docs",
        },
    )

    assert result.startswith("OK:"), result
    assert (data / "skills" / "external" / "alpha" / "BIBLE.md").read_text(encoding="utf-8") == "skill docs"


def test_system_repo_write_blocks_when_active_workspace_differs(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    repo = tmp_path / "repo"
    active = tmp_path / "workspace"
    data = tmp_path / "data"
    repo.mkdir()
    active.mkdir()
    registry = ToolRegistry(repo_dir=repo, drive_root=data)
    registry._ctx.active_repo_dir = lambda: active
    registry._ctx.system_repo_dir = str(repo)

    result = registry.execute("write_file", {"root": "system_repo", "path": "x.txt", "content": "x"})

    assert "WRITE_FILE_BLOCKED" in result
    assert not (active / "x.txt").exists()
    assert not (repo / "x.txt").exists()


def test_light_mode_blocks_interpreter_inline_repo_writes(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    registry = ToolRegistry(repo_dir=repo, drive_root=data)

    result = registry.execute("run_script", {"script": "open('tmp_probe_no_write', 'w').write('x')"})

    assert "LIGHT_MODE_BLOCKED" in result
    assert not (repo / "tmp_probe_no_write").exists()


def test_light_mode_default_root_does_not_treat_repo_skills_path_as_payload(tmp_path, monkeypatch):
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    (repo / "skills" / "external" / "alpha").mkdir(parents=True)
    (data / "skills" / "external" / "alpha").mkdir(parents=True)
    registry = ToolRegistry(repo_dir=repo, drive_root=data)

    result = registry.execute(
        "write_file",
        {
            "path": "skills/external/alpha/plugin.py",
            "content": "x",
        },
    )

    assert "LIGHT_MODE_BLOCKED" in result
    assert not (repo / "skills" / "external" / "alpha" / "plugin.py").exists()


def test_get_runtime_mode_prefers_boot_baseline(monkeypatch):
    from ouroboros import config as cfg

    monkeypatch.setattr(cfg, "_BOOT_RUNTIME_MODE", "light", raising=True)
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")

    assert cfg.get_runtime_mode() == "light"
