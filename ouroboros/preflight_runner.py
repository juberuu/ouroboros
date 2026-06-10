"""Hermetic pytest preflight for reviewed repository changes."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Optional, Sequence


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
    # Scrub secret-class variables: the hermetic preflight pytest must not
    # inherit live credentials — a (possibly self-written) test could exfiltrate
    # or spend with them. OUROBOROS_* wiring stays (suite needs it).
    secret_suffixes = ("_API_KEY", "_TOKEN", "_PASSWORD", "_CREDENTIALS", "_SECRET")
    for key in list(env):
        if key.startswith("OUROBOROS_"):
            continue
        if key.endswith(secret_suffixes) or key.startswith("GH_"):
            env.pop(key, None)
    temp_root = pathlib.Path(temp_root).resolve(strict=False)
    repo_worktree = pathlib.Path(repo_worktree).resolve(strict=False)
    data_dir = (temp_root / "data").resolve(strict=False)
    env["OUROBOROS_DATA_DIR"] = str(data_dir)
    env["OUROBOROS_SETTINGS_PATH"] = str(data_dir / "settings.json")
    env["OUROBOROS_REPO_DIR"] = str(repo_worktree)
    env["PYTHONPYCACHEPREFIX"] = str((temp_root / "pycache").resolve(strict=False))
    return env


_DEFAULT_PREFLIGHT_TIMEOUT_SEC = 300


def _resolve_preflight_timeout(timeout: int) -> int:
    """Env override (`OUROBOROS_PREFLIGHT_TIMEOUT_SEC`) takes precedence so the
    timeout is one SSOT across callers without editing each call site."""
    raw = os.environ.get("OUROBOROS_PREFLIGHT_TIMEOUT_SEC")
    if raw:
        try:
            parsed = int(float(raw))
            if parsed > 0:
                return parsed
        except (TypeError, ValueError):
            pass
    return timeout


def _terminate_preflight_tree(proc: "subprocess.Popen", temp_root: pathlib.Path) -> None:
    """Reap pytest and its WHOLE tree on timeout/crash.

    ``killpg`` alone reaps only pytest's own process group; descendants that
    started their own session/group (Ouroboros spawns servers/browsers/extension
    children with new sessions) or double-forked to init survive it. So collect
    the live descendant PIDs and their group ids FIRST (the ``pgrep -P`` chain
    breaks once pytest dies and children reparent), then kill pytest's group, the
    recursive PID tree, each captured escapee group, and finally sweep any
    straggler still rooted under the disposable temp root. All platform-specific
    process discovery/termination lives behind platform_layer helpers."""
    from ouroboros.platform_layer import (
        IS_WINDOWS,
        collect_descendant_pids,
        kill_pid_tree,
        kill_process_group_id,
        kill_process_tree,
        kill_processes_referencing,
        process_group_id,
    )

    pid = getattr(proc, "pid", 0) or 0
    descendant_pgids: set[int] = set()
    if pid and not IS_WINDOWS:
        for dpid in collect_descendant_pids(pid):
            gid = process_group_id(dpid)
            if gid and gid != pid:
                descendant_pgids.add(gid)
    try:
        kill_process_tree(proc)
    except Exception:
        pass
    if pid:
        try:
            kill_pid_tree(pid)
        except Exception:
            pass
    for gid in descendant_pgids:
        kill_process_group_id(gid)
    kill_processes_referencing(str(temp_root))


def run_hermetic_pytest(
    repo_dir: pathlib.Path | str,
    *,
    timeout: int = _DEFAULT_PREFLIGHT_TIMEOUT_SEC,
    pytest_args: Optional[Sequence[str]] = None,
    max_output: int = 8000,
) -> Optional[str]:
    """Run pytest against the candidate diff in a disposable worktree.

    Returns ``None`` on success, otherwise a bounded human-readable error.
    ``OUROBOROS_PREFLIGHT_TIMEOUT_SEC`` overrides ``timeout`` for all callers.
    """
    timeout = _resolve_preflight_timeout(timeout)
    repo = pathlib.Path(repo_dir).resolve()
    if not (repo / ".git").exists():
        return None
    if not (repo / "tests").exists():
        return None
    agent_python = os.environ.get("OUROBOROS_AGENT_PYTHON") or sys.executable or "python3"
    args = list(pytest_args or DEFAULT_PYTEST_ARGS)

    temp_root_path = tempfile.mkdtemp(prefix="ouroboros-preflight-")
    temp_root = pathlib.Path(temp_root_path).resolve(strict=False)
    worktree = temp_root / "repo"
    worktree_added = False
    proc: Optional[subprocess.Popen] = None
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

        from ouroboros.platform_layer import subprocess_new_group_kwargs

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
            _terminate_preflight_tree(proc, temp_root)
            # The group is gone, but an escaped grandchild may still hold the
            # inherited stdout/stderr pipe open; don't let a second communicate
            # block forever waiting on it.
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
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
        # Reap any process still rooted in the disposable tree before deleting it
        # — a crash/exception path (not only timeout) can leak a detached child.
        try:
            if proc is not None and proc.poll() is None:
                _terminate_preflight_tree(proc, temp_root)
        except Exception:
            pass
        from ouroboros.platform_layer import kill_processes_referencing
        kill_processes_referencing(str(temp_root))
        if worktree_added:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree)],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
            )
        shutil.rmtree(temp_root, ignore_errors=True)
