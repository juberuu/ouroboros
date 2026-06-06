"""Lifecycle for acting-subagent ``self_worktree`` checkouts.

Acting (mutative) subagents that modify the Ouroboros body itself run inside an
isolated ``git worktree`` checked out from the parent's base commit, under a root
that lives OUTSIDE ``repo/`` and ``data/``. The child writes only there and
returns a ``workspace.patch``; the parent integrates and is the sole committer.

git has no automatic worktree garbage collection, so we keep a durable JSON
registry (``data/state/subagent_worktrees.json``) and prune orphans on startup.
All worktree mutations are serialized by a portable cross-process lock because
``git worktree add/remove/prune`` mutate shared ``.git/worktrees`` metadata and
the existing repo git lock is drive-root scoped, not ``.git`` scoped.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from ouroboros.platform_layer import acquire_exclusive_file_lock, release_exclusive_file_lock
from ouroboros.utils import atomic_write_json
from ouroboros.config import (
    DATA_DIR,
    get_subagent_worktree_retention_days,
    get_subagent_worktree_root,
)

_REGISTRY_NAME = "subagent_worktrees.json"
_LOCK_NAME = ".worktree_ops.lock"
_LOCK_TIMEOUT_SEC = 120.0
_LOCK_STALE_SEC = 600.0
_BRANCH_PREFIX = "subagent/"

# Serializes worktree mutations within this process; the on-disk lock serializes
# across processes (parent worker, supervisor startup prune, etc.).
_inproc_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# Paths and registry
# --------------------------------------------------------------------------- #
def _data_dir(data_dir: Optional[Any] = None) -> Path:
    if data_dir:
        return Path(data_dir)
    env = os.environ.get("OUROBOROS_DATA_DIR")
    if env:
        return Path(env)
    return Path(DATA_DIR)


def _registry_path(data_dir: Optional[Any] = None) -> Path:
    return _data_dir(data_dir) / "state" / _REGISTRY_NAME


def _resolve_root(worktree_root: Optional[Any] = None) -> Path:
    root = Path(worktree_root) if worktree_root else Path(get_subagent_worktree_root())
    return root.expanduser().resolve()


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (ValueError, OSError):
        return False


def _assert_root_isolated(root: Path, repo_dir: Path, data_dir: Path) -> None:
    """Refuse a worktree root that overlaps the live repo or runtime data."""
    if _is_within(root, repo_dir) or _is_within(repo_dir, root):
        raise ValueError(f"subagent worktree root {root} overlaps the Ouroboros repo {repo_dir}")
    if _is_within(root, data_dir) or _is_within(data_dir, root):
        raise ValueError(f"subagent worktree root {root} overlaps runtime data {data_dir}")


def _safe_name(task_id: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id or "").strip())
    return safe or f"wt_{int(time.time())}"


def _load_registry(data_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    path = _registry_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    entries = raw.get("worktrees") if isinstance(raw, dict) else raw
    if isinstance(entries, list):
        return [e for e in entries if isinstance(e, dict)]
    return []


def _save_registry(entries: List[Dict[str, Any]], data_dir: Optional[Any] = None) -> None:
    path = _registry_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"worktrees": entries}, trailing_newline=True)


# --------------------------------------------------------------------------- #
# Locking
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def _ops_lock(root: Path):
    """Serialize worktree mutations in-process (threading.Lock) and across
    processes via the shared portable file-lock SSOT (platform_layer)."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / _LOCK_NAME
    with _inproc_lock:
        fd = acquire_exclusive_file_lock(
            lock_path,
            timeout_sec=_LOCK_TIMEOUT_SEC,
            stale_sec=_LOCK_STALE_SEC,
            metadata=str(os.getpid()),
        )
        if fd is None:
            raise TimeoutError(f"subagent worktree ops lock timeout: {lock_path}")
        try:
            yield
        finally:
            release_exclusive_file_lock(lock_path, fd)


# --------------------------------------------------------------------------- #
# git helpers
# --------------------------------------------------------------------------- #
def _git(repo_dir: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_dir),
        capture_output=True,
        text=True,
        check=check,
    )


def _remove_paths(repo_dir: Path, wt_path: Path, branch: str, *, allowed_root: Optional[Any] = None) -> None:
    """Best-effort teardown: drop the worktree checkout, dir, and branch.

    When ``allowed_root`` is given, refuse to touch any path that is empty or not
    strictly inside it. The registry is durable runtime state; a corrupt/malformed
    entry must never cause deletion of an arbitrary filesystem path.
    """
    wt_path = Path(wt_path)
    wt_text = str(wt_path).strip()
    if allowed_root is not None and (
        not wt_text or wt_text in (".", "/", "//") or not _is_within(wt_path, Path(allowed_root))
    ):
        return
    try:
        _git(repo_dir, "worktree", "remove", "--force", str(wt_path), check=False)
    except Exception:
        pass
    if wt_path.exists():
        shutil.rmtree(wt_path, ignore_errors=True)
    try:
        _git(repo_dir, "worktree", "prune", check=False)
    except Exception:
        pass
    if branch:
        try:
            _git(repo_dir, "branch", "-D", branch, check=False)
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class WorktreeHandle:
    task_id: str
    path: str
    branch: str
    base_sha: str
    repo_dir: str
    created_at: float
    parent_task_id: str = ""


def provision_worktree(
    *,
    repo_dir: Any,
    task_id: Any,
    base_sha: str = "",
    parent_task_id: str = "",
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
) -> WorktreeHandle:
    """Create an isolated worktree branched from ``base_sha`` (default HEAD).

    The returned branch is a delta base for the child; the child's patch is a
    diff against ``base_sha`` so the parent can integrate it deliberately.
    """
    repo_dir = Path(repo_dir).resolve()
    root = _resolve_root(worktree_root)
    _assert_root_isolated(root, repo_dir, _data_dir(data_dir))
    safe_task = _safe_name(task_id)
    with _ops_lock(root):
        if base_sha:
            _git(repo_dir, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
            base_sha = _git(repo_dir, "rev-parse", base_sha).stdout.strip()
        else:
            base_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        wt_path = (root / safe_task).resolve()
        branch = f"{_BRANCH_PREFIX}{safe_task}"
        # Clear any stale checkout/branch left by a crashed run.
        _remove_paths(repo_dir, wt_path, branch, allowed_root=root)
        wt_path.parent.mkdir(parents=True, exist_ok=True)
        _git(repo_dir, "worktree", "add", "--force", "-b", branch, str(wt_path), base_sha)
        handle = WorktreeHandle(
            task_id=str(task_id),
            path=str(wt_path),
            branch=branch,
            base_sha=base_sha,
            repo_dir=str(repo_dir),
            created_at=time.time(),
            parent_task_id=str(parent_task_id or ""),
        )
        entries = [e for e in _load_registry(data_dir) if e.get("path") != str(wt_path)]
        entries.append(asdict(handle))
        _save_registry(entries, data_dir)
        return handle


def remove_worktree(
    *,
    task_id: str = "",
    path: str = "",
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
) -> bool:
    """Tear down a worktree by task_id or path; unregister it. Returns success."""
    want_path = str(Path(path).resolve()) if path else ""
    entries = _load_registry(data_dir)
    match: Optional[Dict[str, Any]] = None
    for entry in entries:
        if task_id and entry.get("task_id") == str(task_id):
            match = entry
            break
        if want_path and entry.get("path") == want_path:
            match = entry
            break
    root = _resolve_root(worktree_root)
    with _ops_lock(root):
        if match is not None:
            _remove_paths(Path(match.get("repo_dir") or "."), Path(match.get("path") or ""), match.get("branch") or "", allowed_root=root)
            survivors = [e for e in _load_registry(data_dir) if e.get("path") != match.get("path")]
            _save_registry(survivors, data_dir)
            return True
        # Unregistered path: best-effort directory removal, but ONLY inside the
        # configured worktree root (never an arbitrary path supplied by a caller).
        if want_path and Path(want_path).exists() and _is_within(Path(want_path), root):
            shutil.rmtree(want_path, ignore_errors=True)
            return True
    return False


def prune_orphans(
    *,
    worktree_root: Optional[Any] = None,
    data_dir: Optional[Any] = None,
    retention_days: Optional[int] = None,
) -> Dict[str, Any]:
    """Startup reconciliation: drop worktrees past retention or with a missing
    checkout, then reconcile git's own worktree metadata. Patch artifacts live in
    the task drive, independent of the worktree, so removal never loses results.
    """
    retention = retention_days if retention_days is not None else get_subagent_worktree_retention_days()
    cutoff = time.time() - max(0, retention) * 86400
    root = _resolve_root(worktree_root)
    removed: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    repos: set[str] = set()
    with _ops_lock(root):
        for entry in _load_registry(data_dir):
            repo_dir = str(entry.get("repo_dir") or "")
            wt_path = str(entry.get("path") or "")
            created = float(entry.get("created_at") or 0)
            if repo_dir:
                repos.add(repo_dir)
            path_exists = Path(wt_path).exists() if wt_path else False
            if created < cutoff or not path_exists:
                if repo_dir or wt_path:
                    _remove_paths(Path(repo_dir or "."), Path(wt_path), entry.get("branch") or "", allowed_root=root)
                removed.append(entry)
            else:
                kept.append(entry)
        _save_registry(kept, data_dir)
        for repo in repos:
            try:
                _git(Path(repo), "worktree", "prune", check=False)
            except Exception:
                pass
    return {"removed": len(removed), "kept": len(kept)}


def list_worktrees(data_dir: Optional[Any] = None) -> List[Dict[str, Any]]:
    """Return registered worktree records (for UI / inspection)."""
    return _load_registry(data_dir)
