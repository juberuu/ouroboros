"""Minimal Colab boot shim.

Paste this file contents into the only immutable Colab cell.
The shim stays tiny and only starts the runtime launcher from repository.
"""

import os
import pathlib
import subprocess
import sys
from typing import Optional

from google.colab import userdata  # type: ignore


def get_secret(name: str, required: bool = False) -> Optional[str]:
    v = None
    try:
        v = userdata.get(name)
    except Exception:
        v = None
    if v is None or str(v).strip() == "":
        v = os.environ.get(name)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name}"
    return v


GITHUB_TOKEN = str(get_secret("GITHUB_TOKEN", required=True))
GITHUB_USER = str(os.environ.get("GITHUB_USER", "razzant"))
GITHUB_REPO = str(os.environ.get("GITHUB_REPO", "ouroboros"))
BOOT_BRANCH = str(os.environ.get("OUROBOROS_BOOT_BRANCH", "ouroboros"))

REPO_DIR = pathlib.Path("/content/ouroboros_repo").resolve()
REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"

if not (REPO_DIR / ".git").exists():
    subprocess.run(["rm", "-rf", str(REPO_DIR)], check=False)
    subprocess.run(["git", "clone", REMOTE_URL, str(REPO_DIR)], check=True)
else:
    subprocess.run(["git", "remote", "set-url", "origin", REMOTE_URL], cwd=str(REPO_DIR), check=True)

subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)
subprocess.run(["git", "checkout", BOOT_BRANCH], cwd=str(REPO_DIR), check=True)
subprocess.run(["git", "reset", "--hard", f"origin/{BOOT_BRANCH}"], cwd=str(REPO_DIR), check=True)

launcher_path = REPO_DIR / "colab_launcher.py"
assert launcher_path.exists(), f"Missing launcher: {launcher_path}"
subprocess.run([sys.executable, str(launcher_path)], cwd=str(REPO_DIR), check=True)
