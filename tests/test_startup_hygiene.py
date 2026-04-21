import pathlib
import types

import ouroboros.agent_startup_checks as startup_mod
import ouroboros.world_profiler as world_profiler
from ouroboros.memory import Memory


def test_check_version_sync_ignores_non_release_tag(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("4.7.0\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('version = "4.7.0"\n', encoding="utf-8")
    (tmp_path / "README.md").write_text("**Version:** 4.7.0\n", encoding="utf-8")
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "ARCHITECTURE.md").write_text("# Ouroboros v4.7.0\n", encoding="utf-8")

    env = types.SimpleNamespace(
        repo_dir=tmp_path,
        repo_path=lambda rel: tmp_path / rel,
    )

    monkeypatch.setattr(
        startup_mod.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout="v4.6.0-test1\n"),
    )

    result, issues = startup_mod.check_version_sync(env)

    assert issues == 0
    assert result["status"] == "ok"
    assert result["latest_tag"] == "4.6.0-test1"
    assert result["tag_sync"] == "ignored_non_release_tag"


def test_memory_ensure_files_generates_world_profile(tmp_path, monkeypatch):
    calls = []

    def fake_generate(output_path: str):
        calls.append(output_path)
        pathlib.Path(output_path).write_text("# WORLD\n", encoding="utf-8")

    monkeypatch.setattr(world_profiler, "generate_world_profile", fake_generate)

    memory = Memory(drive_root=tmp_path, repo_dir=tmp_path)
    memory.ensure_files()
    memory.ensure_files()

    assert calls == [str(memory.world_path())]
    assert memory.world_path().read_text(encoding="utf-8") == "# WORLD\n"


def test_check_uncommitted_changes_never_commits_outside_launcher(monkeypatch, tmp_path):
    """Worker-side check_uncommitted_changes is warning-only; never commits."""
    env = types.SimpleNamespace(
        repo_dir=tmp_path,
        repo_path=lambda rel: tmp_path / rel,
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return types.SimpleNamespace(returncode=0, stdout=" M server.py\n")
        raise AssertionError(f"Unexpected subprocess call: {cmd}")

    monkeypatch.delenv("OUROBOROS_MANAGED_BY_LAUNCHER", raising=False)
    monkeypatch.setattr(startup_mod.subprocess, "run", fake_run)

    result, issues = startup_mod.check_uncommitted_changes(env)

    assert issues == 1
    assert result["status"] == "warning"
    assert result["auto_committed"] is False
    assert result["auto_rescue_skipped"] == "supervisor_side_rescue_owns_this"
    assert calls == [["git", "status", "--porcelain"]]


def test_lifespan_calls_apply_settings_to_env_before_supervisor(monkeypatch):
    """apply_settings_to_env must be called in server lifespan before _start_supervisor_if_needed.

    Regression test for: ANTHROPIC_API_KEY from settings.json not visible to
    resolve_claude_runtime at server startup because apply_settings_to_env was
    only called inside the _run_supervisor background thread.
    """
    import ast
    import pathlib

    server_src = (pathlib.Path(__file__).parent.parent / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(server_src)

    # Find the async lifespan function
    lifespan_fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan":
            lifespan_fn = node
            break
    assert lifespan_fn is not None, "lifespan async function not found in server.py"

    # Collect (lineno, name) for every Call node anywhere inside the lifespan,
    # sorted by source line so the ordering check is meaningful.
    calls_by_line = []
    for node in ast.walk(lifespan_fn):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                calls_by_line.append((node.lineno, fn.id))
            elif isinstance(fn, ast.Attribute):
                calls_by_line.append((node.lineno, fn.attr))
    calls_by_line.sort()
    call_names = [name for _, name in calls_by_line]

    assert "_apply_settings_to_env" in call_names, (
        "_apply_settings_to_env must be called inside lifespan"
    )
    assert "_start_supervisor_if_needed" in call_names, (
        "_start_supervisor_if_needed must be called inside lifespan"
    )

    env_line = next(ln for ln, name in calls_by_line if name == "_apply_settings_to_env")
    supervisor_line = next(ln for ln, name in calls_by_line if name == "_start_supervisor_if_needed")
    assert env_line < supervisor_line, (
        f"_apply_settings_to_env (line {env_line}) must appear before "
        f"_start_supervisor_if_needed (line {supervisor_line}) in lifespan"
    )


def test_check_uncommitted_changes_never_commits_even_when_launcher_managed(monkeypatch, tmp_path):
    """Regression for v4.36.1: worker-side startup check must never run git
    add/commit, even under OUROBOROS_MANAGED_BY_LAUNCHER=1. Rescue is owned by
    supervisor-side safe_restart(rescue_and_reset) in _bootstrap_supervisor_repo.
    """
    env = types.SimpleNamespace(
        repo_dir=tmp_path,
        repo_path=lambda rel: tmp_path / rel,
        branch_dev="ouroboros",
        launcher_managed=True,
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return types.SimpleNamespace(returncode=0, stdout=" M server.py\n")
        raise AssertionError(
            f"Unexpected subprocess call {cmd}: worker-side check_uncommitted_changes "
            "must not mutate git state (no add/commit)"
        )

    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    monkeypatch.setattr(startup_mod.subprocess, "run", fake_run)

    result, issues = startup_mod.check_uncommitted_changes(env)

    assert issues == 1
    assert result["status"] == "warning"
    assert result["auto_committed"] is False
    assert result["auto_rescue_skipped"] == "supervisor_side_rescue_owns_this"
    assert calls == [["git", "status", "--porcelain"]]
