"""Shared utility for pushing files to razzant/ouroboros-webapp via git."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)


def push_to_webapp(
    files: Dict[str, str],
    commit_msg: str,
    post_clone_hook: Optional[Callable[[Path], None]] = None,
) -> str:
    """Clone ouroboros-webapp, write files, commit and push.

    Args:
        files: {filename: content} dict -- files to write to the webapp repo root.
        commit_msg: Git commit message.
        post_clone_hook: Optional callback receiving the cloned webapp dir path.
            Called after clone but before commit. Can read/modify any files in the
            repo (e.g., patch app.html based on existing content).

    Returns:
        Status string describing what happened.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        webapp_dir = Path(tmpdir) / "webapp"

        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            repo_url = f"https://{token}@github.com/razzant/ouroboros-webapp.git"
        else:
            repo_url = "https://github.com/razzant/ouroboros-webapp.git"

        r = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, str(webapp_dir)],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            return f"Clone failed: {r.stderr[:400]}"

        subprocess.run(["git", "config", "user.name", "Ouroboros"], cwd=webapp_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "ouroboros@joi.ai"], cwd=webapp_dir, capture_output=True)

        for filename, content in files.items():
            (webapp_dir / filename).write_text(content, encoding="utf-8")

        if post_clone_hook is not None:
            try:
                post_clone_hook(webapp_dir)
            except Exception as e:
                log.warning("post_clone_hook failed: %s", e, exc_info=True)

        # Stage all changes (explicit files + any hook modifications)
        subprocess.run(["git", "add", "."], cwd=webapp_dir, capture_output=True)

        commit = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=webapp_dir, capture_output=True, text=True,
        )
        if "nothing to commit" in (commit.stdout + commit.stderr):
            return f"No changes (up to date)"

        push = subprocess.run(
            ["git", "push", "origin", "HEAD:main"],
            cwd=webapp_dir, capture_output=True, text=True, timeout=60,
        )
        if push.returncode != 0:
            return f"Push failed: {push.stderr[:400]}"

        return f"Pushed ({commit_msg})"
