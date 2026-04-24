"""Managed git bootstrap helpers for the desktop launcher."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable


BUNDLE_REPO_NAME = "repo.bundle"
BUNDLE_MANIFEST_NAME = "repo_bundle_manifest.json"
MANAGED_REPO_META_NAME = "ouroboros-managed.json"
BOOTSTRAP_PIN_MARKER_NAME = "ouroboros-bootstrap-pending"
MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MANAGED_REMOTE_NAME = "managed"
DEFAULT_MANAGED_LOCAL_BRANCH = "ouroboros"
DEFAULT_MANAGED_LOCAL_STABLE_BRANCH = "ouroboros-stable"
DEFAULT_MANAGED_REMOTE_STABLE_BRANCH = "ouroboros-stable"


@dataclass(frozen=True)
class BootstrapContext:
    bundle_dir: pathlib.Path
    repo_dir: pathlib.Path
    data_dir: pathlib.Path
    settings_path: pathlib.Path
    embedded_python: str
    app_version: str
    hidden_run: Callable[..., Any]
    save_settings: Callable[[dict], None]
    log: Any


def check_git(is_windows: bool) -> bool:
    if shutil.which("git") is not None:
        return True
    if is_windows:
        for candidate in (
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Git", "cmd", "git.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Git", "cmd", "git.exe"),
        ):
            if os.path.isfile(candidate):
                git_dir = os.path.dirname(candidate)
                os.environ["PATH"] = git_dir + ";" + os.environ.get("PATH", "")
                return True
    return False


def _bundle_repo_path(context: BootstrapContext) -> pathlib.Path:
    return context.bundle_dir / BUNDLE_REPO_NAME


def _bundle_manifest_path(context: BootstrapContext) -> pathlib.Path:
    return context.bundle_dir / BUNDLE_MANIFEST_NAME


def _managed_meta_path(repo_dir: pathlib.Path) -> pathlib.Path:
    return repo_dir / ".git" / MANAGED_REPO_META_NAME


def _bootstrap_pin_marker_path(repo_dir: pathlib.Path) -> pathlib.Path:
    return repo_dir / ".git" / BOOTSTRAP_PIN_MARKER_NAME


def _read_json_file(path: pathlib.Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _normalize_bundle_manifest(raw: dict[str, Any], *, app_version: str) -> dict[str, Any]:
    manifest = dict(raw)
    return {
        "schema_version": int(manifest.get("schema_version") or MANIFEST_SCHEMA_VERSION),
        "bundle_file": str(manifest.get("bundle_file") or BUNDLE_REPO_NAME),
        "app_version": str(manifest.get("app_version") or app_version),
        "source_sha": str(manifest.get("source_sha") or ""),
        "release_tag": str(manifest.get("release_tag") or ""),
        "bundle_sha256": str(manifest.get("bundle_sha256") or ""),
        "source_branch": str(manifest.get("source_branch") or ""),
        "managed_remote_name": str(manifest.get("managed_remote_name") or DEFAULT_MANAGED_REMOTE_NAME),
        "managed_remote_url": str(manifest.get("managed_remote_url") or ""),
        "managed_remote_branch": str(manifest.get("managed_remote_branch") or manifest.get("source_branch") or ""),
        "managed_local_branch": str(manifest.get("managed_local_branch") or DEFAULT_MANAGED_LOCAL_BRANCH),
        "managed_local_stable_branch": str(
            manifest.get("managed_local_stable_branch") or DEFAULT_MANAGED_LOCAL_STABLE_BRANCH
        ),
        "managed_remote_stable_branch": str(
            manifest.get("managed_remote_stable_branch") or DEFAULT_MANAGED_REMOTE_STABLE_BRANCH
        ),
    }


def load_bundle_manifest(context: BootstrapContext) -> dict[str, Any]:
    manifest_path = _bundle_manifest_path(context)
    if not manifest_path.is_file():
        raise RuntimeError(
            f"Embedded managed repo manifest is missing: {manifest_path}. "
            "Rebuild the app bundle with scripts/build_repo_bundle.py."
        )
    manifest = _normalize_bundle_manifest(_read_json_file(manifest_path), app_version=context.app_version)
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise RuntimeError(
            f"Unsupported managed repo manifest schema {manifest['schema_version']} "
            f"(expected {MANIFEST_SCHEMA_VERSION})."
        )
    if not manifest["source_sha"]:
        raise RuntimeError("Managed repo manifest is missing source_sha.")
    if not manifest["bundle_sha256"]:
        raise RuntimeError("Managed repo manifest is missing bundle_sha256.")
    if not manifest["managed_remote_branch"]:
        raise RuntimeError("Managed repo manifest is missing managed_remote_branch.")
    if manifest["app_version"] != context.app_version:
        raise RuntimeError(
            f"Managed repo manifest app_version {manifest['app_version']!r} does not "
            f"match launcher app version {context.app_version!r}."
        )
    expected_tag = f"v{manifest['app_version']}"
    if manifest["release_tag"] and manifest["release_tag"] != expected_tag:
        raise RuntimeError(
            f"Managed repo manifest release_tag {manifest['release_tag']!r} does not "
            f"match app_version {manifest['app_version']!r}."
        )
    _assert_bundle_integrity(context, manifest)
    return manifest


def load_repo_manifest(repo_dir: pathlib.Path) -> dict[str, Any]:
    meta_path = _managed_meta_path(repo_dir)
    if not meta_path.is_file():
        return {}
    return _read_json_file(meta_path)


def _write_repo_manifest(repo_dir: pathlib.Path, manifest: dict[str, Any]) -> None:
    meta_path = _managed_meta_path(repo_dir)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def _mark_bootstrap_pin_pending(repo_dir: pathlib.Path) -> None:
    marker = _bootstrap_pin_marker_path(repo_dir)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("pending\n", encoding="utf-8")


def _repo_manifest_matches(repo_dir: pathlib.Path, bundle_manifest: dict[str, Any]) -> bool:
    installed = load_repo_manifest(repo_dir)
    if not installed:
        return False
    tracked_keys = (
        "schema_version",
        "app_version",
        "source_sha",
        "release_tag",
        "bundle_sha256",
        "managed_remote_name",
        "managed_remote_url",
        "managed_remote_branch",
        "managed_local_branch",
        "managed_local_stable_branch",
        "managed_remote_stable_branch",
    )
    return all(str(installed.get(key) or "") == str(bundle_manifest.get(key) or "") for key in tracked_keys)


def _run_git(context: BootstrapContext, args: list[str], *, cwd: pathlib.Path, check: bool = True) -> Any:
    return context.hidden_run(
        args,
        cwd=str(cwd),
        check=check,
        capture_output=True,
        text=True,
    )


def _remote_url(context: BootstrapContext, repo_dir: pathlib.Path, remote_name: str) -> str:
    result = _run_git(context, ["git", "remote", "get-url", remote_name], cwd=repo_dir, check=False)
    return str(getattr(result, "stdout", "") or "").strip()


def _sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_bundle_integrity(context: BootstrapContext, manifest: dict[str, Any]) -> None:
    bundle_path = context.bundle_dir / manifest["bundle_file"]
    if not bundle_path.is_file():
        raise RuntimeError(
            f"Embedded managed repo bundle is missing: {bundle_path}. "
            "Rebuild the app bundle with scripts/build_repo_bundle.py."
        )
    actual_sha = _sha256_file(bundle_path)
    expected_sha = str(manifest.get("bundle_sha256") or "").strip()
    if expected_sha and actual_sha != expected_sha:
        raise RuntimeError(
            f"Embedded managed repo bundle hash mismatch for {bundle_path}: "
            f"expected {expected_sha}, got {actual_sha}."
        )


def _archive_existing_repo(context: BootstrapContext, reason: str) -> pathlib.Path | None:
    if not context.repo_dir.exists():
        return None
    archive_root = context.data_dir / "archive" / "managed_repo"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_dir = archive_root / f"{int(time.time())}-{uuid.uuid4().hex[:8]}-{reason}"
    shutil.move(str(context.repo_dir), str(archive_dir))
    context.log.info("Archived existing repo to %s (%s)", archive_dir, reason)
    return archive_dir


def _remove_if_exists(path: pathlib.Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def _configure_managed_clone(context: BootstrapContext, repo_dir: pathlib.Path, manifest: dict[str, Any]) -> None:
    source_sha = str(manifest.get("source_sha") or "").strip()
    local_branch = manifest["managed_local_branch"]
    local_stable_branch = manifest["managed_local_stable_branch"]
    remote_name = manifest["managed_remote_name"]
    remote_url = manifest["managed_remote_url"]

    source_sha_check = _run_git(
        context,
        ["git", "rev-parse", "--verify", source_sha],
        cwd=repo_dir,
        check=False,
    )
    if getattr(source_sha_check, "returncode", 1) != 0:
        raise RuntimeError(
            f"Embedded managed repo bundle does not contain manifest source_sha {source_sha}."
        )
    _run_git(context, ["git", "checkout", "-B", local_branch, source_sha], cwd=repo_dir)
    head = _run_git(context, ["git", "rev-parse", "HEAD"], cwd=repo_dir)
    head_sha = str(getattr(head, "stdout", "") or "").strip()
    if head_sha != source_sha:
        raise RuntimeError(
            f"Managed repo bootstrap checked out {head_sha or '(unknown)'} but manifest "
            f"requires {source_sha}."
        )
    if local_stable_branch and local_stable_branch != local_branch:
        if head_sha:
            _run_git(context, ["git", "branch", "-f", local_stable_branch, head_sha], cwd=repo_dir)

    origin = _run_git(context, ["git", "remote"], cwd=repo_dir, check=False)
    existing_remotes = {
        line.strip() for line in str(getattr(origin, "stdout", "") or "").splitlines() if line.strip()
    }
    if "origin" in existing_remotes:
        _run_git(context, ["git", "remote", "remove", "origin"], cwd=repo_dir, check=False)
    if remote_name in existing_remotes:
        _run_git(context, ["git", "remote", "remove", remote_name], cwd=repo_dir, check=False)
    if remote_url:
        _run_git(context, ["git", "remote", "add", remote_name, remote_url], cwd=repo_dir)

    _run_git(context, ["git", "config", "user.name", "Ouroboros"], cwd=repo_dir, check=False)
    _run_git(context, ["git", "config", "user.email", "ouroboros@local.mac"], cwd=repo_dir, check=False)
    _write_repo_manifest(repo_dir, manifest)
    _mark_bootstrap_pin_pending(repo_dir)


def _ensure_managed_remote(context: BootstrapContext, repo_dir: pathlib.Path, manifest: dict[str, Any]) -> None:
    remote_name = manifest["managed_remote_name"]
    remote_url = manifest["managed_remote_url"]

    remotes = _run_git(context, ["git", "remote"], cwd=repo_dir, check=False)
    existing_remotes = {
        line.strip() for line in str(getattr(remotes, "stdout", "") or "").splitlines() if line.strip()
    }
    if remote_url:
        if remote_name in existing_remotes:
            _run_git(context, ["git", "remote", "set-url", remote_name, remote_url], cwd=repo_dir)
        else:
            _run_git(context, ["git", "remote", "add", remote_name, remote_url], cwd=repo_dir)

    _run_git(context, ["git", "config", "user.name", "Ouroboros"], cwd=repo_dir, check=False)
    _run_git(context, ["git", "config", "user.email", "ouroboros@local.mac"], cwd=repo_dir, check=False)
    _write_repo_manifest(repo_dir, manifest)


def _clone_repo_from_bundle(context: BootstrapContext, manifest: dict[str, Any]) -> pathlib.Path:
    bundle_path = context.bundle_dir / manifest["bundle_file"]
    if not bundle_path.is_file():
        raise RuntimeError(
            f"Embedded managed repo bundle is missing: {bundle_path}. "
            "Rebuild the app bundle with scripts/build_repo_bundle.py."
        )

    temp_repo = context.repo_dir.parent / f".repo-bootstrap-{uuid.uuid4().hex[:8]}"
    _remove_if_exists(temp_repo)
    try:
        _run_git(context, ["git", "clone", str(bundle_path), str(temp_repo)], cwd=context.bundle_dir)
        _configure_managed_clone(context, temp_repo, manifest)
        return temp_repo
    except Exception:
        _remove_if_exists(temp_repo)
        raise


def _install_managed_repo(context: BootstrapContext, manifest: dict[str, Any], *, reason: str) -> str:
    preserved_origin_url = _remote_url(context, context.repo_dir, "origin") if (context.repo_dir / ".git").exists() else ""
    archived_repo = _archive_existing_repo(context, reason)
    temp_repo = _clone_repo_from_bundle(context, manifest)
    try:
        shutil.move(str(temp_repo), str(context.repo_dir))
        if preserved_origin_url:
            _run_git(context, ["git", "remote", "add", "origin", preserved_origin_url], cwd=context.repo_dir, check=False)
    except Exception:
        _remove_if_exists(temp_repo)
        if archived_repo is not None and not context.repo_dir.exists():
            shutil.move(str(archived_repo), str(context.repo_dir))
        raise
    return "replaced" if archived_repo is not None else "created"


def ensure_managed_repo(context: BootstrapContext) -> str:
    """Ensure REPO_DIR is a managed git clone backed by the embedded bundle."""
    manifest = load_bundle_manifest(context)
    if not context.repo_dir.exists():
        return _install_managed_repo(context, manifest, reason="missing")
    if not (context.repo_dir / ".git").exists():
        return _install_managed_repo(context, manifest, reason="legacy-no-git")
    if not _repo_manifest_matches(context.repo_dir, manifest):
        return _install_managed_repo(context, manifest, reason="bundle-upgrade")

    _ensure_managed_remote(context, context.repo_dir, manifest)
    return "unchanged"


def sync_existing_repo_from_bundle(context: BootstrapContext) -> None:
    """Reconcile the managed repo against the embedded bundle metadata."""
    outcome = ensure_managed_repo(context)
    context.log.info("Managed repo sync outcome: %s", outcome)


def _migrate_old_settings(context: BootstrapContext) -> None:
    """Migrate old env-only installs into settings.json on first modern boot."""
    if context.settings_path.exists():
        return

    migrated = {}
    env_keys = [
        "OPENROUTER_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY", "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "OUROBOROS_NETWORK_PASSWORD", "OUROBOROS_FILE_BROWSER_DEFAULT",
        "OUROBOROS_MODEL", "OUROBOROS_MODEL_CODE", "OUROBOROS_MODEL_LIGHT",
        "OUROBOROS_MODEL_FALLBACK", "TOTAL_BUDGET", "OUROBOROS_MAX_WORKERS",
        "OUROBOROS_SOFT_TIMEOUT_SEC", "OUROBOROS_HARD_TIMEOUT_SEC",
        "GITHUB_TOKEN", "GITHUB_REPO",
    ]
    for key in env_keys:
        val = os.environ.get(key, "")
        if val:
            migrated[key] = val
    if not migrated:
        return
    try:
        context.save_settings(migrated)
        context.log.info("Migrated %d env settings into %s", len(migrated), context.settings_path)
    except Exception as exc:
        context.log.warning("Failed to migrate old settings: %s", exc)


def install_deps(context: BootstrapContext) -> None:
    """Install/update Python deps inside the embedded interpreter."""
    try:
        requirements = context.repo_dir / "requirements.txt"
        if requirements.exists():
            context.hidden_run(
                [context.embedded_python, "-m", "pip", "install", "-r", str(requirements)],
                timeout=240,
                capture_output=True,
            )
    except Exception as exc:
        context.log.warning("Dependency install/update failed: %s", exc)


_CLAUDE_SDK_BASELINE = "claude-agent-sdk>=0.1.60"
_CLAUDE_SDK_MIN_VERSION = "0.1.60"


def _version_tuple(v: str) -> tuple:
    """Parse a PEP 440-ish version string into a comparable tuple.

    Strips any post/pre/dev suffix after the first non-numeric component.
    ``"0.1.60" -> (0, 1, 60)``, ``"0.1.60.post1" -> (0, 1, 60)``.
    Returns ``(0,)`` on parse failure (treat as "very old, needs upgrade").
    """
    if not v:
        return (0,)
    parts: list[int] = []
    for p in v.split("."):
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def verify_claude_runtime(context: BootstrapContext) -> bool:
    """Ensure the Claude runtime baseline is present in the app-managed interpreter.

    Checks that ``claude-agent-sdk`` is importable, its installed version meets
    ``_CLAUDE_SDK_MIN_VERSION``, and its bundled CLI binary exists. If any
    check fails, attempts a repair install. Returns True on success.

    Version check prevents a silent gap where an older installed SDK
    (e.g. 0.1.50 on an upgraded install) still imports and has the CLI
    binary present, but pre-dates Opus 4.7 adaptive thinking support.
    """
    import sys as _sys
    cli_name = "claude.exe" if _sys.platform == "win32" else "claude"
    try:
        result = context.hidden_run(
            [context.embedded_python, "-c",
             "import claude_agent_sdk; "
             "import importlib.metadata as _m; "
             "from pathlib import Path; "
             f"cli = Path(claude_agent_sdk.__file__).parent / '_bundled' / '{cli_name}'; "
             "ver = _m.version('claude-agent-sdk'); "
             "print('ok|' + ver if cli.exists() else 'no_cli|' + ver)"],
            capture_output=True, text=True, timeout=30,
        )
        stdout = (result.stdout or "").strip()
        if result.returncode == 0 and stdout.startswith("ok|"):
            installed = stdout.split("|", 1)[1]
            if _version_tuple(installed) >= _version_tuple(_CLAUDE_SDK_MIN_VERSION):
                context.log.info(
                    "Claude runtime verified: SDK %s >= %s, bundled CLI present.",
                    installed, _CLAUDE_SDK_MIN_VERSION,
                )
                return True
            context.log.warning(
                "Claude runtime SDK %s is below baseline %s — repairing.",
                installed, _CLAUDE_SDK_MIN_VERSION,
            )
        else:
            context.log.warning("Claude runtime check: %s (exit %d)", stdout, result.returncode)
    except Exception as exc:
        context.log.warning("Claude runtime probe failed: %s", exc)

    context.log.info("Repairing Claude runtime baseline...")
    try:
        repair = context.hidden_run(
            [context.embedded_python, "-m", "pip", "install", "--upgrade", _CLAUDE_SDK_BASELINE],
            timeout=120,
            capture_output=True,
        )
        if repair.returncode != 0:
            context.log.warning("Claude runtime repair pip returned exit %d", repair.returncode)
            return False
        context.log.info("Claude runtime repair install complete.")
        return True
    except Exception as exc:
        context.log.warning("Claude runtime repair failed: %s", exc)
        return False


def bootstrap_repo(context: BootstrapContext) -> None:
    """Ensure the launcher-managed git repo exists and matches the embedded bundle."""
    context.data_dir.mkdir(parents=True, exist_ok=True)
    outcome = ensure_managed_repo(context)
    context.log.info("Bootstrapping managed repository to %s (outcome=%s)", context.repo_dir, outcome)

    try:
        memory_dir = context.data_dir / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)
        world_path = memory_dir / "WORLD.md"
        if not world_path.exists():
            env = os.environ.copy()
            env["PYTHONPATH"] = str(context.repo_dir)
            context.hidden_run(
                [
                    context.embedded_python,
                    "-c",
                    f"import sys; sys.path.insert(0, '{context.repo_dir}'); "
                    f"from ouroboros.world_profiler import generate_world_profile; "
                    f"generate_world_profile('{world_path}')",
                ],
                env=env,
                timeout=30,
                capture_output=True,
            )
    except Exception as exc:
        context.log.warning("World profile generation failed: %s", exc)

    _migrate_old_settings(context)
    if outcome != "unchanged":
        install_deps(context)
    verify_claude_runtime(context)
    context.log.info("Bootstrap complete.")
