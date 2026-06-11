"""Tool API v2 filesystem observability.

The public tools keep their existing root policies, but user-facing output must
name the resolved logical root so agents do not confuse workspace, runtime data,
task-drive, and skill-payload paths.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from ouroboros.tools.core import _code_search, _edit_text, _is_search_skippable, _read_file, _write_file
from ouroboros.tools.registry import ToolContext


def _make_ctx(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    drive = tmp_path / "data"
    drive.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("hello workspace\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return ToolContext(repo_dir=repo, drive_root=drive)


def test_read_file_headers_are_root_qualified(tmp_path):
    ctx = _make_ctx(tmp_path)
    (ctx.drive_root / "notes.txt").write_text("hello data\n", encoding="utf-8")

    workspace = _read_file(ctx, "README.md", root="active_workspace")
    runtime = _read_file(ctx, "notes.txt", root="runtime_data")

    assert workspace.startswith("# active_workspace:README.md")
    assert runtime.startswith("# runtime_data:notes.txt")


def test_write_file_outputs_use_normalized_root_paths(tmp_path, monkeypatch):
    ctx = _make_ctx(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    workspace = _write_file(ctx, "/tmp/tool.py", "print('x')", root="active_workspace")
    runtime = _write_file(ctx, "/tmp/tool.txt", "x", root="runtime_data")
    task_drive = _write_file(ctx, "./artifact.txt", "x", root="task_drive")
    user_file = _write_file(ctx, "Desktop/tool.txt", "x", root="user_files")

    assert "active_workspace:tmp/tool.py" in workspace
    assert "runtime_data:tmp/tool.txt" in runtime
    assert "task_drive:artifact.txt" in task_drive
    assert "user_files:Desktop/tool.txt" in user_file


def test_edit_text_and_search_outputs_are_root_qualified(tmp_path):
    ctx = _make_ctx(tmp_path)

    edit_result = _edit_text(ctx, "README.md", "hello", "hi", root="active_workspace")
    search_result = _code_search(ctx, "hi workspace", path=".", root="active_workspace")
    missing_result = _code_search(ctx, "missing", path="/not-there", root="active_workspace")

    assert "Replaced in active_workspace:README.md" in edit_result
    assert "active_workspace:README.md:1: hi workspace" in search_result
    assert "path not found: active_workspace:not-there" in missing_result


def test_absolute_path_under_root_is_not_double_prefixed(tmp_path):
    """NW-8: an absolute path that already points inside the active root must
    resolve to that file, not be re-nested (``/app`` + ``/app/x`` -> ``/app/app/x``).

    The double-prefix silently wrote deliverables to the wrong place and pushed
    agents toward the blocked ``user_files`` root on Terminal-Bench (/app/answer.txt
    etc.). Writing via an absolute in-root path must round-trip through read.
    """
    ctx = _make_ctx(tmp_path)
    root = ctx.repo_dir.resolve()
    abs_path = str(root / "deliverable.txt")

    _write_file(ctx, abs_path, "answer-payload", root="active_workspace")
    # The file lands at root/deliverable.txt, NOT root/<root>/deliverable.txt.
    assert (root / "deliverable.txt").read_text(encoding="utf-8") == "answer-payload"
    assert not (root / str(root).lstrip("/")).exists()
    # And reading the same absolute path returns it (no NOT_FOUND detour).
    read_back = _read_file(ctx, abs_path, root="active_workspace")
    assert "answer-payload" in read_back
    # Path traversal stays blocked.
    assert ctx.repo_path(str(root / "sub" / "f.py")) == root / "sub" / "f.py"


@pytest.mark.skipif(os.name == "nt", reason="os.mkfifo is POSIX-only")
def test_search_skips_non_regular_files(tmp_path):
    """NW-3: search must never read pseudo-files / device nodes / FIFOs.

    ``read_text`` on ``/dev/zero`` (or any non-regular file) never terminates
    and grows memory without bound — the search_code OOM that SIGKILLed a
    worker when a search root resolved to ``/``. ``_is_search_skippable`` must
    skip them, and a search over a directory containing one must still complete
    and find matches in adjacent regular files.
    """
    fifo = tmp_path / "a_fifo"
    os.mkfifo(str(fifo))
    regular = tmp_path / "code.py"
    regular.write_text("needle_token = 1\n", encoding="utf-8")

    assert _is_search_skippable(fifo) is True
    assert _is_search_skippable(regular) is False

    ctx = _make_ctx(tmp_path)
    (ctx.repo_dir / "src.py").write_text("needle_token = 2\n", encoding="utf-8")
    fifo_in_repo = ctx.repo_dir / "pipe"
    os.mkfifo(str(fifo_in_repo))

    # Must complete without hanging and find the regular-file match.
    result = _code_search(ctx, "needle_token", path=".", root="active_workspace")
    assert "active_workspace:src.py:1: needle_token = 2" in result
