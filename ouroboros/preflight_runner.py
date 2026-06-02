"""Hermetic pytest preflight for reviewed repository changes."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import List, Optional, Sequence


DEFAULT_PYTEST_ARGS = ["tests/", "-q", "--tb=line", "--no-header"]


def _run_git(repo_dir: pathlib.Path, args: Sequence[str], *, input_text: str = "", timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        input=input_text if input_text else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _apply_diff(worktree: pathlib.Path, diff_text: str) -> None:
    if not diff_text.strip():
        return
    proc = _run_git(
        worktree,
        ["apply", "--whitespace=nowarn", "--binary"],
        input_text=diff_text,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git apply failed")


def _copy_untracked(repo_dir: pathlib.Path, worktree: pathlib.Path) -> None:
    listed = _run_git(repo_dir, ["ls-files", "--others", "--exclude-standard", "-z"])
    if listed.returncode != 0:
        raise RuntimeError(listed.stderr.strip() or "git ls-files failed")
    raw = listed.stdout or ""
    for rel in [part for part in raw.split("\0") if part]:
        src = (repo_dir / rel).resolve()
        dst = (worktree / rel).resolve()
        try:
            dst.relative_to(worktree.resolve())
            src.relative_to(repo_dir.resolve())
        except ValueError as exc:
            raise RuntimeError(f"Unsafe untracked path: {rel}") from exc
        if not src.is_file():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _preflight_env(temp_root: pathlib.Path, repo_worktree: pathlib.Path) -> dict:
    env = dict(os.environ)
    env.pop("OUROBOROS_MANAGED_BY_LAUNCHER", None)
    data_dir = temp_root / "data"
    env["OUROBOROS_DATA_DIR"] = str(data_dir)
    env["OUROBOROS_SETTINGS_PATH"] = str(data_dir / "settings.json")
    env["OUROBOROS_REPO_DIR"] = str(repo_worktree)
    env["PYTHONPYCACHEPREFIX"] = str(temp_root / "pycache")
    return env


def run_hermetic_pytest(
    repo_dir: pathlib.Path | str,
    *,
    timeout: int = 180,
    pytest_args: Optional[Sequence[str]] = None,
    max_output: int = 8000,
) -> Optional[str]:
    """Run pytest against the candidate diff in a disposable worktree.

    Returns ``None`` on success, otherwise a bounded human-readable error.
    """
    repo = pathlib.Path(repo_dir).resolve()
    if not (repo / ".git").exists():
        return None
    if not (repo / "tests").exists():
        return None
    agent_python = sys.executable or os.environ.get("OUROBOROS_AGENT_PYTHON") or "python3"
    args = list(pytest_args or DEFAULT_PYTEST_ARGS)

    temp_root_path = tempfile.mkdtemp(prefix="ouroboros-preflight-")
    temp_root = pathlib.Path(temp_root_path)
    worktree = temp_root / "repo"
    worktree_added = False
    try:
        add = _run_git(repo, ["worktree", "add", "--detach", str(worktree), "HEAD"], timeout=60)
        if add.returncode != 0:
            return f"⚠️ PRE_PUSH_TEST_ERROR: could not create hermetic worktree: {add.stderr.strip()}"
        worktree_added = True

        staged_proc = _run_git(repo, ["diff", "--cached", "--binary"])
        unstaged_proc = _run_git(repo, ["diff", "--binary"])
        if staged_proc.returncode != 0:
            raise RuntimeError(staged_proc.stderr.strip() or "git diff --cached failed")
        if unstaged_proc.returncode != 0:
            raise RuntimeError(unstaged_proc.stderr.strip() or "git diff failed")
        staged = staged_proc.stdout or ""
        unstaged = unstaged_proc.stdout or ""
        _apply_diff(worktree, staged)
        _apply_diff(worktree, unstaged)
        _copy_untracked(repo, worktree)

        from ouroboros.platform_layer import kill_process_tree, subprocess_new_group_kwargs

        proc = subprocess.Popen(
            [agent_python, "-m", "pytest", *args],
            cwd=str(worktree),
            env=_preflight_env(temp_root, worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **subprocess_new_group_kwargs(),
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            stdout, stderr = proc.communicate(timeout=10)
            return f"⚠️ PRE_PUSH_TEST_ERROR: pytest timed out after {timeout} seconds"
        result_returncode = proc.returncode
        if result_returncode == 0:
            return None
        output = (stdout or "") + (stderr or "")
        if len(output) > max_output:
            output = output[:max_output] + "\n...(truncated)..."
        return output.strip() or f"pytest exited with code {result_returncode}"
    except subprocess.TimeoutExpired:
        return f"⚠️ PRE_PUSH_TEST_ERROR: pytest timed out after {timeout} seconds"
    except FileNotFoundError:
        return f"⚠️ PRE_PUSH_TEST_ERROR: pytest not available via interpreter: {agent_python}"
    except Exception as exc:
        return f"⚠️ PRE_PUSH_TEST_ERROR: hermetic preflight failed: {exc}"
    finally:
        if worktree_added:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
            )
        shutil.rmtree(temp_root, ignore_errors=True)
