"""Google Colab bootstrap helpers for source-mode Ouroboros."""

from __future__ import annotations

import getpass
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional

from ouroboros.config import SETTINGS_DEFAULTS
from ouroboros.utils import atomic_write_json

DEFAULT_COLAB_APP_ROOT = "/content/drive/MyDrive/Ouroboros"
DEFAULT_COLAB_REPO_DIR = "/content/ouroboros_repo"
DEFAULT_OFFICIAL_REPO_URL = "https://github.com/razzant/ouroboros.git"

_SECRET_KEYS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN")


def get_colab_secret(name: str, *, required: bool = True) -> str:
    """Return a Colab secret/env value, or ask via a hidden prompt.

    Optional secrets (``required=False``) never block on a prompt: if they are
    absent from Colab userdata and the environment, an empty string is returned.
    """
    value = ""
    try:
        from google.colab import userdata  # type: ignore
        value = str(userdata.get(name) or "").strip()
    except Exception:
        value = ""
    if not value:
        value = str(os.environ.get(name, "") or "").strip()
    if not value and required:
        value = getpass.getpass(f"{name}: ").strip()
    return value


def collect_colab_secrets() -> Dict[str, str]:
    """Collect runtime secrets without printing their values.

    The Telegram bot token is required (the bridge needs it). Any supported
    provider key works — OpenRouter, OpenAI, or Anthropic are collected
    optionally, and OpenRouter is prompted only if none is found, so an
    OpenAI-only or Anthropic-only Colab user is not forced to enter OpenRouter.
    The GitHub token is optional (it only enables personal self-modification
    persistence), so a quick prototype never blocks on a GitHub prompt.
    """
    # Providers the one-click Colab launch can auto-route models for via
    # apply_runtime_provider_defaults (OpenRouter is the default aggregator;
    # OpenAI/Anthropic/Cloud.ru have direct model defaults). OpenAI-compatible
    # endpoints have no universal model default and need explicit OUROBOROS_MODEL_*
    # config, so they are an advanced manual path, not part of the quick launch.
    provider_keys = (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    )
    out: Dict[str, str] = {}
    out["TELEGRAM_BOT_TOKEN"] = get_colab_secret("TELEGRAM_BOT_TOKEN")
    for key in provider_keys:
        out[key] = get_colab_secret(key, required=False)
    if not any(out.get(key) for key in provider_keys):
        # Ensure at least one runnable provider; OpenRouter is the default route.
        out["OPENROUTER_API_KEY"] = get_colab_secret("OPENROUTER_API_KEY")
    out["GITHUB_TOKEN"] = get_colab_secret("GITHUB_TOKEN", required=False)
    return out


def masked_secret_status(settings: Dict[str, Any]) -> Dict[str, bool]:
    """Expose configured/missing status only; never return secret values."""
    return {key: bool(str(settings.get(key, "") or "").strip()) for key in _SECRET_KEYS}


def build_colab_settings(secrets: Dict[str, str], *, github_repo: str = "", total_budget: float = 10.0, runtime_mode: str = "advanced", max_workers: int = 1, models: Dict[str, str] | None = None, network_password: str = "", existing: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build a Drive-persisted settings payload for Colab.

    On a re-run, ``existing`` (the current Drive ``settings.json``) is merged over
    the defaults FIRST, so prior owner choices that the Colab launch does not set
    explicitly — a pinned ``TELEGRAM_CHAT_ID``, tweaked model slots, an auto-grant
    preference — survive instead of being reset to defaults. The launch knobs
    below (secrets, budget, runtime mode, workers, host, repo) then win.
    """
    settings = dict(SETTINGS_DEFAULTS)
    if existing:
        # Apply the same v6.39 slot rename-alias migration load_settings uses, BEFORE
        # merging over the defaults — exactly as load_settings migrates the raw loaded file
        # before layering SETTINGS_DEFAULTS. (Migrating after the merge would see the new
        # key already present as its empty default and skip the copy.) This keeps a Drive
        # settings.json with legacy OUROBOROS_MODEL_CODE / USE_LOCAL_CODE /
        # OUROBOROS_MODEL_FALLBACK customizations alive on a re-run.
        from ouroboros.config import migrate_legacy_slot_keys
        migrated = migrate_legacy_slot_keys(dict(existing))
        settings.update({k: v for k, v in migrated.items() if not str(k).startswith("_")})
    for key, value in secrets.items():
        # Only overwrite when the freshly collected secret is non-empty, so a
        # re-run that omits an optional provider/GitHub key (collect_colab_secrets
        # returns "") does NOT wipe a credential already persisted on Drive.
        if (key in settings or key == "TELEGRAM_BOT_TOKEN") and str(value or "").strip():
            settings[key] = str(value)
    if github_repo:
        settings["GITHUB_REPO"] = github_repo
    settings["TOTAL_BUDGET"] = float(total_budget)
    settings["OUROBOROS_RUNTIME_MODE"] = runtime_mode
    settings["OUROBOROS_MAX_WORKERS"] = int(max_workers)
    settings["OUROBOROS_SERVER_HOST"] = "127.0.0.1"
    if network_password:
        settings["OUROBOROS_NETWORK_PASSWORD"] = network_password
    for key, value in (models or {}).items():
        if key in {"OUROBOROS_MODEL", "OUROBOROS_MODEL_HEAVY", "OUROBOROS_MODEL_LIGHT", "OUROBOROS_MODEL_VISION", "OUROBOROS_MODEL_CONSCIOUSNESS", "OUROBOROS_MODEL_FALLBACKS"} and value:
            settings[key] = str(value)
    # Route model slots to the configured provider (same SSOT the desktop
    # onboarding wizard uses): an OpenAI-only / Anthropic-only / Cloud.ru-only
    # Colab config gets correct provider model defaults instead of OpenRouter-style
    # ones. Explicitly supplied model overrides above are preserved.
    from ouroboros.server_runtime import apply_runtime_provider_defaults
    settings, _changed, _changed_keys = apply_runtime_provider_defaults(settings)
    return settings


def write_colab_settings(data_dir: pathlib.Path, settings: Dict[str, Any]) -> pathlib.Path:
    path = pathlib.Path(data_dir) / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, dict(settings), trailing_newline=True)
    return path


def export_colab_env(repo_dir: pathlib.Path, data_dir: pathlib.Path, settings_path: pathlib.Path) -> Dict[str, str]:
    # Colab forks workers from the long-lived, multi-threaded supervisor; a forked
    # child can deadlock on the CPython import lock the first time it lazily imports
    # the OpenAI client (llm._make_no_proxy_client). spawn is the safe start method
    # for fork-from-threads, so default to it here and respect an explicit override.
    start_method = (os.environ.get("OUROBOROS_WORKER_START_METHOD") or "spawn").strip().lower()
    env = {"OUROBOROS_APP_ROOT": str(pathlib.Path(data_dir).parent), "OUROBOROS_REPO_DIR": str(repo_dir), "OUROBOROS_DATA_DIR": str(data_dir), "OUROBOROS_SETTINGS_PATH": str(settings_path), "OUROBOROS_WORKER_START_METHOD": start_method, "PYTHONUNBUFFERED": "1", "PYTHONPATH": str(repo_dir)}
    os.environ.update(env)
    return env


def _ff_to_origin_if_ahead(repo_dir: pathlib.Path, branch: str) -> bool:
    """Fast-forward the local branch to origin/<branch> if origin is ahead.

    Restores self-modification commits pushed to the personal origin in an
    earlier (ephemeral) Colab session. Fast-forward only, so it can never lose
    or rewrite local state — a diverged/behind origin leaves the checkout at the
    official HEAD that ``clone_or_update_repo`` already established.
    """
    cwd = str(repo_dir)
    try:
        if subprocess.run(["git", "fetch", "origin", branch], cwd=cwd, capture_output=True).returncode != 0:
            return False
        rc = subprocess.run(["git", "merge", "--ff-only", f"origin/{branch}"], cwd=cwd, capture_output=True).returncode
        return rc == 0
    except Exception:
        return False


def configure_colab_personal_origin(repo_dir: pathlib.Path, data_dir: pathlib.Path, settings: Dict[str, Any], *, branch: str = "ouroboros") -> Dict[str, Any]:
    """Configure the personal origin remote, creating a fork when needed, and
    best-effort restore prior self-modification commits pushed to that origin."""
    token = str(settings.get("GITHUB_TOKEN") or "").strip()
    if not token:
        return {"ok": False, "error": "GITHUB_TOKEN is not configured"}
    from supervisor import git_ops
    git_ops.init(pathlib.Path(repo_dir), pathlib.Path(data_dir), remote_url="")
    ok, message, resolved = git_ops.configure_personal_remote(str(settings.get("GITHUB_REPO") or ""), token, auto_fork=True, confirm_replace_origin=False)
    restored = False
    if ok and resolved:
        settings["GITHUB_REPO"] = resolved
        restored = _ff_to_origin_if_ahead(repo_dir, branch)
    return {"ok": ok, "message": message, "repo": resolved, "restored_from_origin": restored}


def clone_or_update_repo(repo_dir: pathlib.Path, source_url: str = DEFAULT_OFFICIAL_REPO_URL, branch: str = "ouroboros") -> pathlib.Path:
    repo_dir = pathlib.Path(repo_dir)
    if not (repo_dir / ".git").exists():
        if repo_dir.exists():
            raise RuntimeError(f"{repo_dir} exists but is not a git repository")
        subprocess.run(["git", "clone", "--branch", branch, source_url, str(repo_dir)], check=True)
    remotes = subprocess.run(["git", "remote"], cwd=str(repo_dir), capture_output=True, text=True, check=False).stdout.split()
    if "managed" in remotes:
        subprocess.run(["git", "remote", "set-url", "managed", source_url], cwd=str(repo_dir), check=False)
    else:
        from ouroboros.repo_remotes import normalize_repo_slug
        origin_url = ""
        if "origin" in remotes:
            origin_url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=str(repo_dir), capture_output=True, text=True, check=False).stdout.strip()
        # Only reclassify a clone-default origin still pointing at the official
        # upstream as `managed`; a personal `origin` is the persistence target and
        # must be preserved (role-based remotes: managed=official, origin=personal).
        if "origin" in remotes and normalize_repo_slug(origin_url) == normalize_repo_slug(source_url):
            subprocess.run(["git", "remote", "rename", "origin", "managed"], cwd=str(repo_dir), check=False)
            subprocess.run(["git", "remote", "set-url", "managed", source_url], cwd=str(repo_dir), check=False)
        else:
            subprocess.run(["git", "remote", "add", "managed", source_url], cwd=str(repo_dir), check=False)
    subprocess.run(["git", "fetch", "managed"], cwd=str(repo_dir), check=False)
    remote_ref = f"managed/{branch}"
    has_remote_ref = subprocess.run(["git", "rev-parse", "--verify", remote_ref], cwd=str(repo_dir), capture_output=True, check=False).returncode == 0
    if has_remote_ref:
        has_local_branch = subprocess.run(["git", "rev-parse", "--verify", branch], cwd=str(repo_dir), capture_output=True, check=False).returncode == 0
        if has_local_branch:
            subprocess.run(["git", "checkout", branch], cwd=str(repo_dir), check=True)
            subprocess.run(["git", "merge", "--ff-only", remote_ref], cwd=str(repo_dir), check=True)
        else:
            subprocess.run(["git", "checkout", "-B", branch, remote_ref], cwd=str(repo_dir), check=True)
    return repo_dir


def _gateway_request(host: str, port: int) -> Callable[..., tuple]:
    base = f"http://{host}:{port}"

    def _call(method: str, path: str, body: Optional[Dict[str, Any]] = None, timeout: float = 60.0) -> tuple:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw) if raw.strip() else {}
            except Exception:
                payload = {"error": raw[:300]}
            return exc.code, payload

    return _call


def ensure_telegram_bridge_live(
    host: str = "127.0.0.1",
    port: int = 8765,
    *,
    settings: Optional[Dict[str, Any]] = None,
    slug: str = "telegram-bridge",
    command_mode: str = "full_access",
    timeout: float = 180.0,
    request: Optional[Callable[..., tuple]] = None,
) -> Dict[str, Any]:
    """Install, review, grant, enable, and configure the Telegram bridge over loopback.

    Headless Colab has no UI to manage skills, so this brings the bridge fully
    live after the server is started, including setting the bridge command mode
    (default ``full_access``) so owner slash commands actually work — otherwise
    the bridge installs in ``strict`` mode and blocks every slash command. It is
    best-effort: it returns a status dict and never raises, so a failure here
    cannot crash the notebook.
    """
    settings = settings or {}
    call = request or _gateway_request(host, port)
    status: Dict[str, Any] = {"ok": False, "slug": slug, "steps": []}

    # 1. Wait until the server accepts loopback requests (the gateway client has
    #    no built-in retry, and the notebook starts the server as a subprocess).
    deadline = time.time() + timeout
    ready = False
    while time.time() < deadline:
        try:
            code, _ = call("GET", "/api/health")
            if code == 200:
                ready = True
                break
        except Exception:
            pass
        time.sleep(1.0)
    if not ready:
        status["error"] = "server did not become ready"
        return status
    status["steps"].append("ready")

    if not str(settings.get("TELEGRAM_BOT_TOKEN") or "").strip():
        status["warning"] = "TELEGRAM_BOT_TOKEN is empty; bridge will install but cannot poll Telegram"

    quoted = urllib.parse.quote(slug)

    # Auto-grant is NOT forced here: it is governed by the persisted
    # OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS setting (default-on, merged from any
    # existing Drive settings.json), so an owner who deliberately disabled it is
    # respected. With the default on, the install-time review grants the bridge's
    # keys; if an owner turned it off, the enable step below surfaces the missing
    # grants instead of silently overriding the owner's policy.

    # 2. Install from the live catalog. This synchronously runs tri-model skill
    #    review, which can take minutes, so use a review-scale timeout rather
    #    than the default 60s (otherwise a slow review looks like an install
    #    failure even though the server is still working).
    try:
        _code, payload = call("POST", "/api/marketplace/ouroboroshub/install", {"slug": slug}, timeout=1800.0)
    except Exception as exc:
        status["error"] = f"install request failed: {exc}"
        return status
    err = str((payload or {}).get("error") or "") if isinstance(payload, dict) else ""
    already = "already installed" in err.lower()
    if err and not already:
        status["error"] = f"install failed: {err}"
        return status
    status["steps"].append("already_installed" if already else "installed")

    # 3b. The already-installed path does NOT re-run install-time review/grants,
    #     so a Drive-persisted bridge whose review/grants state is missing or
    #     stale (e.g. an interrupted earlier session) would fail to enable below.
    #     Re-run review (auto-grant is on) to guarantee the enable precondition.
    if already:
        try:
            _code, payload = call("POST", f"/api/skills/{quoted}/review", timeout=1800.0)
        except Exception as exc:
            status["error"] = f"re-review request failed: {exc}"
            return status
        rerr = str((payload or {}).get("error") or "") if isinstance(payload, dict) else ""
        if rerr:
            status["error"] = f"re-review failed: {rerr}"
            return status
        status["steps"].append("reviewed")

    # 4. Enable (gateway enforces fresh executable review + all grants).
    try:
        _code, payload = call("POST", f"/api/skills/{quoted}/toggle", {"enabled": True})
    except Exception as exc:
        status["error"] = f"enable request failed: {exc}"
        return status
    err = str((payload or {}).get("error") or "") if isinstance(payload, dict) else ""
    if err:
        status["error"] = f"enable failed: {err}"
        return status
    status["steps"].append("enabled")

    # 5. Set the bridge command mode so owner slash commands are allowed. Without
    #    this the bridge defaults to `strict` and rejects every slash command.
    #    The skill's settings/save route is mounted once the extension is enabled;
    #    settings.json is empty at this point (no chat pinned yet), so this is safe.
    if command_mode:
        try:
            code, payload = call("POST", f"/api/extensions/{quoted}/settings/save", {"TELEGRAM_COMMAND_MODE": command_mode})
        except Exception as exc:
            status["command_mode_ok"] = False
            status["warning"] = f"bridge enabled but command mode not applied (slash commands stay restricted): {exc}"
        else:
            cm_err = str((payload or {}).get("error") or "") if isinstance(payload, dict) else ""
            if (isinstance(code, int) and code >= 400) or cm_err:
                status["command_mode_ok"] = False
                status["warning"] = f"bridge enabled but command mode not applied (slash commands stay restricted): {cm_err or f'HTTP {code}'}"
            else:
                status["steps"].append(f"command_mode:{command_mode}")
                status["command_mode_ok"] = True

    status["ok"] = True
    return status


def server_command(repo_dir: pathlib.Path, *, host: str = "127.0.0.1", port: int = 8765) -> list[str]:
    return [sys.executable, "-m", "ouroboros.cli", "server", "--host", host, "--port", str(port), "--no-ui"]
