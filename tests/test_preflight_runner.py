import os
import pathlib
import subprocess
import textwrap
import time
import inspect

import pytest

from ouroboros.platform_layer import force_kill_pid, pid_is_alive


def _git(repo: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def test_hermetic_pytest_applies_candidate_diff_and_scrubs_live_env(tmp_path, monkeypatch):
    from ouroboros.preflight_runner import run_hermetic_pytest

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "ouroboros")
    (repo / "value.py").write_text("FLAG = False\n", encoding="utf-8")
    tests = repo / "tests"
    tests.mkdir()
    (tests / "test_env_and_diff.py").write_text(
        textwrap.dedent(
            """
            import os
            import extra_value
            import value


            def test_candidate_diff_and_env_are_hermetic():
                assert value.FLAG is True
                assert extra_value.FLAG is True
                assert "OUROBOROS_MANAGED_BY_LAUNCHER" not in os.environ
                assert "ouroboros-preflight-" in os.environ["OUROBOROS_DATA_DIR"]
                assert os.environ["OUROBOROS_SETTINGS_PATH"].startswith(os.environ["OUROBOROS_DATA_DIR"])
                assert "ouroboros-preflight-" in os.environ["OUROBOROS_REPO_DIR"]
            """
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "value.py").write_text("FLAG = True\n", encoding="utf-8")
    (repo / "extra_value.py").write_text("FLAG = True\n", encoding="utf-8")

    monkeypatch.setenv("OUROBOROS_MANAGED_BY_LAUNCHER", "1")
    result = run_hermetic_pytest(repo, timeout=30)

    assert result is None


def test_hermetic_pytest_timeout_invokes_full_tree_reaper():
    """The timeout path must delegate to the full-tree reaper (not a bare killpg),
    and that reaper must use the recursive PID-tree kill, escaped process-group
    kill, and the temp-root sweep so detached/reparented children cannot survive."""
    from ouroboros import preflight_runner

    runner_src = inspect.getsource(preflight_runner.run_hermetic_pytest)
    assert "subprocess.Popen" in runner_src
    assert "_terminate_preflight_tree" in runner_src

    reaper_src = inspect.getsource(preflight_runner._terminate_preflight_tree)
    assert "kill_process_tree" in reaper_src
    assert "kill_pid_tree" in reaper_src
    assert "kill_process_group_id" in reaper_src
    assert "kill_processes_referencing" in reaper_src
    # Platform-specific process discovery stays behind platform_layer helpers.
    assert "collect_descendant_pids" in reaper_src


def test_resolve_preflight_timeout_env_override(monkeypatch):
    from ouroboros.preflight_runner import _resolve_preflight_timeout

    monkeypatch.delenv("OUROBOROS_PREFLIGHT_TIMEOUT_SEC", raising=False)
    assert _resolve_preflight_timeout(300) == 300
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TIMEOUT_SEC", "450")
    assert _resolve_preflight_timeout(300) == 450
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TIMEOUT_SEC", "not-an-int")
    assert _resolve_preflight_timeout(300) == 300
    monkeypatch.setenv("OUROBOROS_PREFLIGHT_TIMEOUT_SEC", "0")
    assert _resolve_preflight_timeout(300) == 300


def test_hermetic_pytest_prefers_agent_python_env():
    from ouroboros import preflight_runner

    runner_src = inspect.getsource(preflight_runner.run_hermetic_pytest)
    assert 'os.environ.get("OUROBOROS_AGENT_PYTHON") or sys.executable' in runner_src


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group reaping behaviour")
def test_hermetic_pytest_timeout_reaps_detached_session_child(tmp_path, monkeypatch):
    """A child that escapes pytest's process group (its own session) must still be
    reaped on timeout — the orphan class the QA hit (96% CPU survivor)."""
    from ouroboros.preflight_runner import run_hermetic_pytest
    monkeypatch.delenv("OUROBOROS_PREFLIGHT_TIMEOUT_SEC", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "ouroboros")
    marker = tmp_path / "child.pid"
    tests = repo / "tests"
    tests.mkdir()
    # NOTE: this fixture is generated user-style code that runs inside the
    # hermetic worktree (where `ouroboros` is not on sys.path), so it must stay
    # stdlib-only. start_new_session=True simulates an arbitrary skill test
    # spawning a detached child in its own session — exactly the orphan class the
    # reaper must catch. The test HARNESS itself uses platform_layer helpers.
    (tests / "test_hang.py").write_text(
        textwrap.dedent(
            f"""
            import sys, subprocess, time

            def test_spawns_detached_child_and_hangs():
                # Detached child in its OWN session escapes the pytest group's killpg.
                subprocess.Popen(
                    [sys.executable, "-c",
                     "import os,time;open(r'{marker}','w').write(str(os.getpid()));time.sleep(180)"],
                    start_new_session=True,
                )
                time.sleep(180)
            """
        ),
        encoding="utf-8",
    )
    _git(repo, "add", ".")
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "init"],
        cwd=str(repo), check=True, capture_output=True, text=True,
    )

    result = run_hermetic_pytest(repo, timeout=5)
    assert result is not None and "timed out" in result

    assert marker.exists(), "detached child never recorded its pid"
    child_pid = int(marker.read_text().strip())
    deadline = time.time() + 10
    alive = True
    while time.time() < deadline:
        if not pid_is_alive(child_pid):
            alive = False
            break
        time.sleep(0.2)
    if alive:  # cleanup so a reaping regression does not leak a 180s sleeper
        force_kill_pid(child_pid)
    assert not alive, f"detached child {child_pid} survived preflight timeout reaping"
