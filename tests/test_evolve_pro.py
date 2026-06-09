"""Unit coverage for the SWE-bench Pro evolutionary driver's non-LLM helpers + the
shared isolated-server runner.

The full driver runs a real isolated server + the model (a manual harness); these
tests exercise the pure, deterministic parts (instance prep, patch capture, settings
seed, isolation guard, and the server_runner helpers) without provider calls.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from devtools.benchmarks.swe_bench_pro import evolve_pro
from devtools.benchmarks.common.run_roots import ensure_outside_repo
from devtools.benchmarks.common import server_runner

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _has_bash() -> bool:
    """True only if bash actually RUNS. On Windows `bash.exe` is often the WSL
    launcher stub that exits non-zero with no distro; capture_patch.sh is a
    POSIX/Docker devtool, so those tests must skip there rather than fail."""
    if shutil.which("bash") is None:
        return False
    try:
        return subprocess.run(["bash", "-c", "exit 0"], capture_output=True, timeout=10).returncode == 0
    except OSError:
        return False


def test_make_demo_instances_are_valid_git_repos(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 2)
    assert len(rows) == 2
    for row in rows:
        assert row["instance_id"].startswith("demo-")
        assert row["problem_statement"]
        repo = Path(row["repo_dir"])
        assert (repo / ".git").is_dir()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
        assert head.stdout.strip() == row["base_commit"]


def test_prepare_workspace_uses_prepared_repo_dir(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 1)
    repo, base = evolve_pro._prepare_workspace(rows[0], tmp_path)
    assert repo == Path(rows[0]["repo_dir"])
    assert base == rows[0]["base_commit"]


def test_prepare_workspace_rejects_non_git_dir(tmp_path: Path):
    bad = tmp_path / "plain"
    bad.mkdir()
    with pytest.raises(RuntimeError, match="not a git worktree"):
        evolve_pro._prepare_workspace({"instance_id": "x", "repo_dir": str(bad)}, tmp_path)


def test_prepare_workspace_requires_worktree_root_not_subdir(tmp_path: Path):
    """repo_dir must be the git worktree ROOT (like gateway/tasks.py), not a subdir —
    a subdir would pass rev-parse but be rejected later at POST /api/tasks."""
    import subprocess
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "sub").mkdir()
    with pytest.raises(RuntimeError, match="worktree ROOT"):
        evolve_pro._prepare_workspace(
            {"instance_id": "x", "repo_dir": str(repo / "sub"), "base_commit": ""}, tmp_path)
    # The worktree root itself is accepted.
    out_repo, _base = evolve_pro._prepare_workspace(
        {"instance_id": "x", "repo_dir": str(repo), "base_commit": ""}, tmp_path)
    assert out_repo == repo.resolve(strict=False)


def test_prepare_workspace_requires_source(tmp_path: Path):
    with pytest.raises(RuntimeError, match="repo_dir.*or repo_url"):
        evolve_pro._prepare_workspace({"instance_id": "x"}, tmp_path)


@pytest.mark.skipif(not _has_bash(), reason="bash required for capture_patch.sh")
def test_capture_patch_captures_source_change(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 1)
    repo = Path(rows[0]["repo_dir"])
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n", encoding="utf-8")
    patch = evolve_pro._capture_patch(repo, rows[0]["base_commit"], tmp_path / "out" / "p.diff")
    assert "return a + b" in patch


@pytest.mark.skipif(not _has_bash(), reason="bash required for capture_patch.sh")
def test_capture_patch_empty_raises(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 1)
    repo = Path(rows[0]["repo_dir"])
    with pytest.raises(RuntimeError, match="empty patch"):
        evolve_pro._capture_patch(repo, rows[0]["base_commit"], tmp_path / "out" / "p.diff")


def test_run_outputs_never_under_repo():
    with pytest.raises(ValueError):
        ensure_outside_repo(evolve_pro.REPO_DIR / "devtools" / "x", evolve_pro.REPO_DIR)


def test_seed_settings_enables_post_task_evolution(tmp_path: Path):
    settings_path = evolve_pro._seed_settings(tmp_path, "every_n:1")
    cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    assert cfg["OUROBOROS_POST_TASK_EVOLUTION"] == "true"
    assert cfg["OUROBOROS_RUNTIME_MODE"] == "advanced"
    assert cfg["OUROBOROS_POST_TASK_EVOLUTION_CADENCE"] == "every_n:1"


# --- server_runner helpers (no server spawned) ---

def test_free_port_returns_distinct_usable_ports():
    a, b = server_runner.free_port(), server_runner.free_port()
    assert isinstance(a, int) and 1024 < a < 65536
    assert isinstance(b, int)


def test_seed_owner_state_writes_owner_and_evolution(tmp_path: Path):
    server_runner.seed_owner_state(tmp_path, evolution_enabled=True)
    st = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert st["owner_chat_id"] == 1
    assert st["evolution_mode_enabled"] is True


def test_seed_owner_state_preserves_existing(tmp_path: Path):
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "state.json").write_text(json.dumps({"spent_usd": 1.5}), encoding="utf-8")
    server_runner.seed_owner_state(tmp_path, evolution_enabled=False)
    st = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert st["spent_usd"] == 1.5
    assert st["owner_chat_id"] == 1
    assert "evolution_mode_enabled" not in st


def test_absorbed_cycles_done_parses_campaign(tmp_path: Path):
    assert server_runner.absorbed_cycles_done(tmp_path) == 0  # missing file -> 0
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "evolution_campaign.json").write_text(
        json.dumps({"absorbed_cycles_done": 3}), encoding="utf-8")
    assert server_runner.absorbed_cycles_done(tmp_path) == 3


def test_isolated_server_blocks_default_ports_for_subagents(monkeypatch):
    """The isolated server uses free ports; a subagent must still never browse the
    control plane. Cross-check the browser policy blocks the server's own port when
    set via env (the driver sets OUROBOROS_SERVER_PORT). monkeypatch.setenv auto-restores
    any pre-existing value so this never leaks into later tests in the same process."""
    from ouroboros.tools.browser import _is_subagent_blocked_browser_url
    from types import SimpleNamespace

    monkeypatch.setenv("OUROBOROS_SERVER_PORT", "8911")
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:8911", SimpleNamespace(workspace_root="")) is True


def test_solve_instance_timeout_cancels_then_records_true_status(monkeypatch, tmp_path):
    """On wait_task timeout, _solve_instance cancels the task, waits for a REAL terminal
    status, and records that status (never 'completed') — capture happens only after the
    worker is confirmed terminal, so it cannot race a live worker."""
    from devtools.benchmarks.swe_bench_pro import evolve_pro

    calls = {"wait": 0, "cancelled": []}

    class FakeServer:
        def submit(self, *a, **k):
            return "tid-1"

        def wait_task(self, task_id, timeout=0):
            calls["wait"] += 1
            return {"status": "timeout"} if calls["wait"] == 1 else {"status": "cancelled"}

        def cancel_task(self, task_id):
            calls["cancelled"].append(task_id)

    monkeypatch.setattr(evolve_pro, "_prepare_workspace", lambda item, rr: (tmp_path / "repo", "base"))
    monkeypatch.setattr(evolve_pro, "_capture_patch", lambda repo, base, out: "diff --git a/x b/x\n+y")
    item = {"instance_id": "i1", "problem_statement": "do x"}
    row, prediction, error = evolve_pro._solve_instance(
        FakeServer(), item, tmp_path, tmp_path / "patches", "forked", 1)
    assert calls["cancelled"] == ["tid-1"]   # cancelled on timeout
    assert calls["wait"] == 2                  # re-waited for a terminal status
    assert row["status"] == "cancelled"        # TRUE status recorded, not 'completed'
    assert prediction.get("model_patch")       # captured only after terminal
    assert error is None


def test_solve_instance_aborts_when_task_never_terminates(monkeypatch, tmp_path):
    """If a task stays non-terminal even after cancel, _solve_instance raises the FATAL
    _DriverAbort (NOT a recoverable per-instance error), so the run stops."""
    import pytest
    from devtools.benchmarks.swe_bench_pro import evolve_pro

    class StuckServer:
        def submit(self, *a, **k):
            return "tid-stuck"

        def wait_task(self, task_id, timeout=0):
            return {"status": "timeout"}  # never reaches a terminal status

        def cancel_task(self, task_id):
            pass

    monkeypatch.setattr(evolve_pro, "_prepare_workspace", lambda item, rr: (tmp_path / "repo", "base"))
    monkeypatch.setattr(evolve_pro, "_capture_patch", lambda *a, **k: "x")
    item = {"instance_id": "i2", "problem_statement": "do y"}
    with pytest.raises(evolve_pro._DriverAbort):
        evolve_pro._solve_instance(StuckServer(), item, tmp_path, tmp_path / "patches", "forked", 1)


def test_prepare_workspace_refuses_live_repo_or_data_overlap(tmp_path):
    """A pre-submit `git checkout` must never touch the live body: _prepare_workspace
    rejects a repo_dir that overlaps the live Ouroboros repo or data dir."""
    import pytest
    from devtools.benchmarks.swe_bench_pro import evolve_pro

    with pytest.raises(RuntimeError, match="overlaps the live"):
        evolve_pro._prepare_workspace(
            {"instance_id": "i", "repo_dir": str(evolve_pro.REPO_DIR), "base_commit": "x"}, tmp_path)
    with pytest.raises(RuntimeError, match="overlaps the live"):
        evolve_pro._prepare_workspace(
            {"instance_id": "i", "repo_dir": str(evolve_pro.LIVE_DATA / "sub"), "base_commit": "x"}, tmp_path)


def test_prepare_workspace_refuses_custom_launch_data_root(tmp_path, monkeypatch):
    """A repo_dir under a custom/Drive-backed OUROBOROS_DATA_DIR live root (captured at
    launch) is also refused — not just the default ~/Ouroboros/data."""
    import pytest
    custom_live = tmp_path / "drive" / "data"
    custom_live.mkdir(parents=True)
    monkeypatch.setattr(evolve_pro, "_LAUNCH_DATA_DIR", str(custom_live))
    with pytest.raises(RuntimeError, match="overlaps the live"):
        evolve_pro._prepare_workspace(
            {"instance_id": "i", "repo_dir": str(custom_live / "repo"), "base_commit": "x"}, tmp_path)


def test_isolated_server_env_strips_inherited_stale_keys(monkeypatch, tmp_path):
    """IsolatedServer._env must NOT inherit live/managed runtime keys (USE_LOCAL_*, stale
    OUROBOROS_* host/path, URL, MANAGED_BY_LAUNCHER) from a managed launch env — a leaked
    value would route the throwaway server through LIVE config; isolated overrides must win."""
    from devtools.benchmarks.common.server_runner import IsolatedServer

    for k in ("USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK",
              "OUROBOROS_URL", "OUROBOROS_MANAGED_BY_LAUNCHER", "OUROBOROS_SERVER_HOST",
              "OUROBOROS_BOOT_RUNTIME_MODE"):
        monkeypatch.setenv(k, "stale-live")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "owner-transport-secret")
    monkeypatch.setenv("MYSKILL_API_KEY", "skill-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "provider-ok")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", "/live/data")   # stale live value
    monkeypatch.setenv("OUROBOROS_REPO_DIR", "/live/repo")   # stale live value

    env = IsolatedServer(tmp_path / "clone", tmp_path / "data", tmp_path / "settings.json")._env()
    for k in ("USE_LOCAL_MAIN", "USE_LOCAL_CODE", "USE_LOCAL_LIGHT", "USE_LOCAL_FALLBACK",
              "OUROBOROS_URL", "OUROBOROS_MANAGED_BY_LAUNCHER", "OUROBOROS_BOOT_RUNTIME_MODE"):
        assert k not in env, k
    # Isolated overrides win over inherited stale live values.
    assert env["OUROBOROS_DATA_DIR"] == str(tmp_path / "data")
    assert env["OUROBOROS_REPO_DIR"] == str(tmp_path / "clone")
    assert env["OUROBOROS_SETTINGS_PATH"] == str(tmp_path / "settings.json")
    # Owner secrets must NOT survive into the isolated server's env either.
    assert "GITHUB_TOKEN" not in env
    assert "OUROBOROS_NETWORK_PASSWORD" not in env
    assert "TELEGRAM_BOT_TOKEN" not in env
    assert "MYSKILL_API_KEY" not in env
    assert env["OPENROUTER_API_KEY"] == "provider-ok"  # explicit provider env is allowed


def test_build_isolated_settings_allowlist_excludes_secrets():
    """The isolated settings seed copies ONLY provider/model/budget keys (explicit allowlist)
    and drops owner/control secrets — including a custom skill secret named like *_API_KEY —
    because untrusted benchmark tasks can read the isolated data root."""
    from devtools.benchmarks.common.server_runner import build_isolated_settings
    live = {
        "OPENROUTER_API_KEY": "k1", "ANTHROPIC_API_KEY": "k2", "OPENAI_BASE_URL": "u",
        "OUROBOROS_MODEL": "m", "OUROBOROS_MODEL_CODE": "m2", "OUROBOROS_REVIEW_MODELS": "a,b",
        "OUROBOROS_SCOPE_REVIEW_MODELS": "c", "GIGACHAT_PASSWORD": "gp",
        "LOCAL_MODEL_PORT": 8766, "OUROBOROS_EFFORT_REVIEW": "high", "TOTAL_BUDGET": 9.0,
        "OUROBOROS_CONTEXT_MODE": "max",
        "GITHUB_TOKEN": "ghp", "GITHUB_REPO": "o/r", "OUROBOROS_NETWORK_PASSWORD": "pw",
        "TELEGRAM_BOT_TOKEN": "tg", "OUROBOROS_OWNER_CHAT_ID": "1", "MYSKILL_API_KEY": "leak",
    }
    out = build_isolated_settings(live, OUROBOROS_RUNTIME_MODE="advanced")
    for k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "OUROBOROS_MODEL",
              "OUROBOROS_MODEL_CODE", "OUROBOROS_REVIEW_MODELS", "OUROBOROS_SCOPE_REVIEW_MODELS",
              "GIGACHAT_PASSWORD", "LOCAL_MODEL_PORT", "OUROBOROS_EFFORT_REVIEW", "TOTAL_BUDGET",
              "OUROBOROS_CONTEXT_MODE"):
        assert k in out, f"missing provider/model key {k}"
    for k in ("GITHUB_TOKEN", "GITHUB_REPO", "OUROBOROS_NETWORK_PASSWORD", "TELEGRAM_BOT_TOKEN",
              "OUROBOROS_OWNER_CHAT_ID", "MYSKILL_API_KEY"):
        assert k not in out, f"secret leaked into isolated settings: {k}"
    assert out["OUROBOROS_RUNTIME_MODE"] == "advanced"


def test_isolated_server_start_stops_on_wait_ready_failure(monkeypatch, tmp_path):
    """If _wait_ready raises after the process spawns, start() must stop the server before
    re-raising — otherwise the `with` (__enter__) path would orphan the process tree."""
    import pytest
    from devtools.benchmarks.common import server_runner

    stopped = {"called": False}

    class _FakeProc:
        pid = 999999

        def poll(self):
            return None

    srv = server_runner.IsolatedServer(tmp_path / "clone", tmp_path / "data", tmp_path / "settings.json")
    monkeypatch.setattr(srv, "_patch_settings_ports", lambda: None)
    monkeypatch.setattr(server_runner.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(srv, "_wait_ready", lambda _t: (_ for _ in ()).throw(RuntimeError("not ready")))
    monkeypatch.setattr(srv, "stop", lambda: stopped.__setitem__("called", True))
    with pytest.raises(RuntimeError, match="not ready"):
        srv.start(ready_timeout=1)
    assert stopped["called"] is True
