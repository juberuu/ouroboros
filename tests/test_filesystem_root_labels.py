"""Issue #40: the OK messages emitted by ``data_write`` and ``repo_write``
must name the filesystem root they targeted (``data_root/`` and
``repo_root/`` respectively).

Background: the agent operates against three logical roots —
``repo_dir`` (used by ``repo_*`` and as ``run_shell``'s default cwd),
``drive_root`` (used by ``data_*``), and ``run_shell``'s subprocess cwd.
Pre-issue-#40 the OK messages returned only the relative path, so the
agent could not tell which root a write landed under and recovered slowly
from cross-tool path mismatches. These tests pin the new tokens so the
shape stays consistent and stays paired with ``run_shell``'s
``(cwd=<abs>)`` echo.
"""
from __future__ import annotations

import importlib
import os
import pathlib
import subprocess
import sys
from dataclasses import dataclass

import pytest


REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


# ---------------------------------------------------------------------------
# data_write: OK message contains the ``data_root/`` token
# ---------------------------------------------------------------------------


@dataclass
class _DriveCtx:
    """Minimal ToolContext-shaped object for ``_data_write`` tests."""
    repo_dir: pathlib.Path
    drive_root: pathlib.Path

    def repo_path(self, rel: str) -> pathlib.Path:
        return self.repo_dir / rel

    def drive_path(self, rel: str) -> pathlib.Path:
        return self.drive_root / rel


def _make_drive_ctx(tmp_path: pathlib.Path) -> _DriveCtx:
    drive = tmp_path / "data"
    drive.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    return _DriveCtx(repo_dir=repo, drive_root=drive)


class TestDataWriteNamesDataRoot:
    def test_overwrite_ok_message_carries_data_root_prefix(self, tmp_path, monkeypatch):
        from ouroboros import config as cfg
        from ouroboros.tools.core import _data_write

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data", raising=False)
        monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "data" / "settings.json", raising=False)

        ctx = _make_drive_ctx(tmp_path)
        result = _data_write(ctx, "memory/scratchpad.md", "hello world")
        assert "DATA_WRITE_BLOCKED" not in result
        assert "OK: wrote overwrite data_root/memory/scratchpad.md" in result
        assert "(11 chars)" in result

    def test_append_ok_message_carries_data_root_prefix(self, tmp_path, monkeypatch):
        from ouroboros import config as cfg
        from ouroboros.tools.core import _data_write

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data", raising=False)
        monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "data" / "settings.json", raising=False)

        ctx = _make_drive_ctx(tmp_path)
        result = _data_write(ctx, "logs/run.log", "line\n", mode="append")
        assert "OK: wrote append data_root/logs/run.log" in result

    def test_path_with_leading_dot_slash_is_normalised(self, tmp_path, monkeypatch):
        """``./memory/foo.md`` and ``memory/foo.md`` produce the same token
        so the agent does not have to second-guess the formatting."""
        from ouroboros import config as cfg
        from ouroboros.tools.core import _data_write

        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path / "data", raising=False)
        monkeypatch.setattr(cfg, "SETTINGS_PATH", tmp_path / "data" / "settings.json", raising=False)

        ctx = _make_drive_ctx(tmp_path)
        result = _data_write(ctx, "./memory/foo.md", "x")
        assert "OK: wrote overwrite data_root/memory/foo.md" in result


# ---------------------------------------------------------------------------
# repo_write: each per-file entry in the summary names ``repo_root/``
# ---------------------------------------------------------------------------


def _make_repo_ctx(tmp_path):
    """Create a minimal ToolContext with a temporary git repo (mirrors
    ``tests/test_phase7_pipeline.py::_make_ctx`` so repo_write's git-aware
    hooks fire identically)."""
    from ouroboros.tools.registry import ToolContext
    repo = tmp_path / "repo"
    repo.mkdir()
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / "logs").mkdir(parents=True)
    (drive / "locks").mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=str(repo), capture_output=True)
    (repo / "dummy.txt").write_text("init", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), capture_output=True)
    subprocess.run(["git", "branch", "-M", "ouroboros"], cwd=str(repo), capture_output=True)
    return ToolContext(repo_dir=repo, drive_root=drive)


class TestRepoWriteNamesRepoRoot:
    def test_single_file_summary_contains_repo_root_token(self, tmp_path):
        git_mod = importlib.import_module("ouroboros.tools.git")
        ctx = _make_repo_ctx(tmp_path)
        result = git_mod._repo_write(ctx, path="hello.py", content="print('hello')")
        assert "Written 1 file" in result
        assert "repo_root/hello.py" in result
        assert "(14 chars)" in result

    def test_multi_file_summary_lists_each_path_with_repo_root_prefix(self, tmp_path):
        git_mod = importlib.import_module("ouroboros.tools.git")
        ctx = _make_repo_ctx(tmp_path)
        result = git_mod._repo_write(ctx, files=[
            {"path": "a.py", "content": "# a"},
            {"path": "sub/b.py", "content": "# bbb"},
        ])
        assert "Written 2 file" in result
        assert "repo_root/a.py" in result
        assert "repo_root/sub/b.py" in result

    def test_envelope_still_carries_advisory_stale_marker(self, tmp_path):
        """Regression guard for the surrounding envelope — the new
        per-file token must NOT displace the existing 'NOT committed'
        and 'advisory_pre_review' lines that downstream UI / tests
        already rely on."""
        git_mod = importlib.import_module("ouroboros.tools.git")
        ctx = _make_repo_ctx(tmp_path)
        result = git_mod._repo_write(ctx, path="ok.py", content="x")
        assert "NOT committed" in result
        assert "advisory_pre_review" in result


# ---------------------------------------------------------------------------
# Cross-tool consistency: the three roots are now self-describing in unison
# ---------------------------------------------------------------------------


def test_data_root_and_repo_root_tokens_are_stable_strings():
    """Pin the exact tokens so a rename of either constant breaks here
    rather than silently in agent prompts. ``data_root/`` already appears
    in the friendly ``repo_read`` NOT_FOUND hint (see
    ``test_memory_tool_hints``) and ``repo_root=`` appears inside the
    ``SHELL_CWD_ERROR`` body; both tokens are part of the agent-facing
    vocabulary now."""
    from ouroboros.tools import core as core_mod, git as git_mod, shell as shell_mod
    import inspect

    data_src = inspect.getsource(core_mod._data_write)
    assert "data_root/" in data_src

    repo_src = inspect.getsource(git_mod._repo_write)
    assert "repo_root/" in repo_src

    shell_src = inspect.getsource(shell_mod._run_shell)
    assert "repo_root=" in shell_src
    assert "SHELL_CWD_ERROR" in shell_src
