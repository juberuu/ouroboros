import os
import subprocess
import time

import supervisor.git_ops as git_ops


def test_git_capture_repairs_corrupt_index(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "index").write_text("broken", encoding="utf-8")
    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)

    calls = {"status": 0, "rebuild": 0}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd == ["git", "status", "--porcelain"]:
            calls["status"] += 1
            if calls["status"] == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    128,
                    stdout="",
                    stderr="fatal: .git/index: index file smaller than expected\n",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout=" M changed.py\n", stderr="")
        if cmd == ["git", "reset", "--mixed", "HEAD"]:
            calls["rebuild"] += 1
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    rc, stdout, stderr = git_ops.git_capture(["git", "status", "--porcelain"])

    assert rc == 0
    assert stdout == "M changed.py"
    assert stderr == ""
    assert calls["status"] == 2
    assert calls["rebuild"] == 1
    assert any(path.name.startswith("index.corrupt.") for path in git_dir.iterdir())


def test_checkout_and_reset_removes_stale_index_lock(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    lock_path = git_dir / "index.lock"
    lock_path.write_text("lock", encoding="utf-8")
    stale_ts = time.time() - 60
    os.utime(lock_path, (stale_ts, stale_ts))

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_has_remote", lambda: False)
    monkeypatch.setattr(git_ops, "load_state", lambda: {})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    calls = {"checkout": 0}

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "checkout"]:
            calls["checkout"] += 1
            if calls["checkout"] == 1:
                return subprocess.CompletedProcess(
                    cmd,
                    128,
                    stdout="",
                    stderr=f"fatal: Unable to create '{git_dir / 'index.lock'}': File exists.\n",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="abc123\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset("ouroboros", unsynced_policy="ignore")

    assert ok
    assert message == "ok"
    assert calls["checkout"] == 2
    assert not lock_path.exists()
    assert saved_state["current_branch"] == "ouroboros"
    assert saved_state["current_sha"] == "abc123"


def test_checkout_and_reset_continues_when_fetch_fails(monkeypatch, tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()

    monkeypatch.setattr(git_ops, "REPO_DIR", tmp_path)
    monkeypatch.setattr(git_ops, "_has_remote", lambda: True)
    monkeypatch.setattr(git_ops, "load_state", lambda: {})

    saved_state = {}
    monkeypatch.setattr(git_ops, "save_state", lambda state: saved_state.update(state))

    events = []
    monkeypatch.setattr(git_ops, "append_jsonl", lambda path, payload: events.append(payload))

    def fake_git_capture(cmd):
        if cmd == ["git", "fetch", "origin"]:
            return 1, "", "network down"
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops, "git_capture", fake_git_capture)

    def fake_run(cmd, cwd=None, capture_output=False, text=False, check=False):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "checkout"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "reset"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "rev-parse"] and cmd[-1] == "HEAD":
            return subprocess.CompletedProcess(cmd, 0, stdout="def456\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(git_ops.subprocess, "run", fake_run)

    ok, message = git_ops.checkout_and_reset("ouroboros", reason="restart", unsynced_policy="ignore")

    assert ok
    assert message == "ok"
    assert saved_state["current_branch"] == "ouroboros"
    assert saved_state["current_sha"] == "def456"
    assert events
    assert events[0]["type"] == "reset_fetch_failed"
    assert events[0]["continuing_local_reset"] is True
