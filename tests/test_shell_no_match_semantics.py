from __future__ import annotations

from subprocess import CompletedProcess
from types import SimpleNamespace

from ouroboros.tools.shell import _run_shell


def _ctx(tmp_path):
    return SimpleNamespace(repo_dir=tmp_path)


def test_grep_exit_one_without_stderr_is_no_match_not_shell_error(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        return CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})

    result = _run_shell(_ctx(tmp_path), ["grep", "-n", "missing", "file.py"])

    assert "SHELL_EXIT_ERROR" not in result
    assert "exit_code=1 (no matches)" in result
    assert "STDOUT:\n(empty)" in result


def test_rg_exit_one_without_stderr_is_no_match_not_shell_error(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        return CompletedProcess(cmd, 1, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})

    result = _run_shell(_ctx(tmp_path), ["rg", "missing", "."])

    assert "SHELL_EXIT_ERROR" not in result
    assert "exit_code=1 (no matches)" in result


def test_grep_exit_one_with_stderr_still_surfaces_shell_error(tmp_path, monkeypatch):
    def fake_run(cmd, **_kwargs):
        return CompletedProcess(cmd, 1, "", "grep: file.py: No such file or directory\n")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})

    result = _run_shell(_ctx(tmp_path), ["grep", "missing", "file.py"])

    assert "SHELL_EXIT_ERROR" in result
    assert "No such file or directory" in result
