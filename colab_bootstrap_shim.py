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
from google.colab import drive  # type: ignore


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


def export_secret_to_env(name: str, required: bool = False) -> Optional[str]:
    val = get_secret(name, required=required)
    if val is not None and str(val).strip() != "":
        os.environ[name] = str(val)
    return val


# Export required runtime secrets so subprocess launcher can always read env fallback.
for _name in ("OPENROUTER_API_KEY", "TELEGRAM_BOT_TOKEN", "TOTAL_BUDGET", "GITHUB_TOKEN"):
    export_secret_to_env(_name, required=True)

# Optional secrets (keep empty if missing).
for _name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
    export_secret_to_env(_name, required=False)

GITHUB_TOKEN = str(os.environ["GITHUB_TOKEN"])
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

# Mount Drive in notebook process first (interactive auth works here).
if not pathlib.Path("/content/drive/MyDrive").exists():
    drive.mount("/content/drive")

launcher_path = REPO_DIR / "colab_launcher.py"
assert launcher_path.exists(), f"Missing launcher: {launcher_path}"
subprocess.run([sys.executable, str(launcher_path)], cwd=str(REPO_DIR), check=True)
