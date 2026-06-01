# %% [markdown]
# # Ouroboros Colab Quickstart
#
# Runs full source-mode Ouroboros in Google Colab without the desktop UI and
# brings up the Telegram control bridge automatically.

# %%
import json
import os
import pathlib
import subprocess
import sys

try:
    from google.colab import drive  # type: ignore
except Exception as exc:  # pragma: no cover - only meaningful in Colab
    raise RuntimeError("This quickstart is intended for Google Colab.") from exc

drive.mount("/content/drive")

# Minimal bootstrap clone so `ouroboros.colab_bootstrap` becomes importable.
# Remote roles and fast-forward updates are handled by clone_or_update_repo below.
REPO_DIR = pathlib.Path("/content/ouroboros_repo")
if not (REPO_DIR / ".git").exists():
    subprocess.run(
        ["git", "clone", "--branch", "ouroboros", "https://github.com/razzant/ouroboros.git", str(REPO_DIR)],
        check=True,
    )

os.chdir(REPO_DIR)
sys.path.insert(0, str(REPO_DIR))

# %%
from ouroboros.colab_bootstrap import (
    build_colab_settings,
    clone_or_update_repo,
    collect_colab_secrets,
    configure_colab_personal_origin,
    ensure_telegram_bridge_live,
    export_colab_env,
    masked_secret_status,
    server_command,
    write_colab_settings,
)

# Canonical update: establish the `managed` remote role and fast-forward.
clone_or_update_repo(REPO_DIR)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-e", "."], check=True)

APP_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros")
DATA_DIR = APP_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

secrets = collect_colab_secrets()
# Preserve prior owner choices on Drive across ephemeral Colab sessions (a re-run
# of this cell must not wipe a pinned chat, tweaked models, or other prefs).
_existing_settings = {}
_settings_file = DATA_DIR / "settings.json"
if _settings_file.exists():
    try:
        _existing_settings = json.loads(_settings_file.read_text(encoding="utf-8"))
    except Exception:
        _existing_settings = {}
settings = build_colab_settings(
    secrets,
    total_budget=float(os.environ.get("TOTAL_BUDGET", "10")),
    runtime_mode=os.environ.get("OUROBOROS_RUNTIME_MODE", "advanced"),
    max_workers=int(os.environ.get("OUROBOROS_MAX_WORKERS", "1")),
    existing=_existing_settings,
)
# GitHub persistence is optional: a personal fork is configured only when a token
# is present, otherwise the prototype still runs (without remote self-persistence).
origin_result = configure_colab_personal_origin(REPO_DIR, DATA_DIR, settings)
settings_path = write_colab_settings(DATA_DIR, settings)
export_colab_env(REPO_DIR, DATA_DIR, settings_path)

print("Secrets configured:", masked_secret_status(settings))
print("Personal origin:", origin_result)
print("Settings:", settings_path)

# %%
server = subprocess.Popen(
    server_command(REPO_DIR),
    cwd=str(REPO_DIR),
    env=os.environ.copy(),
)
print("Ouroboros server PID:", server.pid)

# Install + review + grant + enable the Telegram bridge over the loopback gateway.
bridge_status = ensure_telegram_bridge_live(settings=settings)
print("Telegram bridge:", bridge_status)
if bridge_status.get("ok") and bridge_status.get("command_mode_ok"):
    print("Message your Telegram bot now. Your first owner slash command (e.g. /status) registers your chat and asks you to send it once more;")
    print("after that, owner commands like /status and /panic run immediately.")
elif bridge_status.get("ok"):
    print("Bridge installed and enabled, but full_access command mode was not applied:", bridge_status.get("warning"))
    print("Slash commands stay restricted until you set TELEGRAM_COMMAND_MODE=full_access in the bridge settings.")
else:
    print("Bridge not live yet:", bridge_status.get("error") or bridge_status)
