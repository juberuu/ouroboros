"""
Ouroboros Launcher — Immutable process manager.

This file is bundled into the .app via PyInstaller. It never self-modifies.
All agent logic lives in REPO_DIR and is launched as a subprocess via the
embedded python-build-standalone interpreter.

Responsibilities:
  - PID lock (single instance)
  - Bootstrap REPO_DIR on first run
  - Start/restart agent subprocess (server.py)
  - Display pywebview window pointing at the agent's local HTTP server
  - Handle restart signals (agent exits with code 42)
"""

from __future__ import annotations

import logging
import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

from ouroboros.config import (
    AGENT_SERVER_PORT,
    DATA_DIR,
    PANIC_EXIT_CODE,
    PORT_FILE,
    REPO_DIR,
    RESTART_EXIT_CODE,
    SETTINGS_PATH,
    acquire_pid_lock,
    apply_settings_to_env as _apply_settings_to_env,
    load_settings,
    normalize_runtime_mode,
    read_version,
    release_pid_lock,
    save_settings,
)
from ouroboros.launcher_bootstrap import (
    BootstrapContext,
    bootstrap_repo as _bootstrap_repo,
    check_git as _check_git,
    install_deps as _install_deps_impl,
    sync_existing_repo_from_bundle as _sync_existing_repo_from_bundle_impl,
    verify_claude_runtime as _verify_claude_runtime,
)
from ouroboros.onboarding_wizard import build_onboarding_html, prepare_onboarding_settings
from ouroboros.platform_layer import (
    IS_MACOS,
    IS_WINDOWS,
    assign_pid_to_job,
    close_job,
    create_kill_on_close_job,
    embedded_python_candidates,
    force_kill_pid,
    git_install_hint,
    kill_process_on_port,
    merge_hidden_kwargs,
    resume_process,
    terminate_job,
)
from ouroboros.server_runtime import apply_runtime_provider_defaults, has_startup_ready_provider

MAX_CRASH_RESTARTS = 5
CRASH_WINDOW_SEC = 120
_CREATE_SUSPENDED = getattr(subprocess, "CREATE_SUSPENDED", 0x4) if IS_WINDOWS else 0
_CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_dir = DATA_DIR / "logs"
_log_dir.mkdir(parents=True, exist_ok=True)

_file_handler = RotatingFileHandler(
    _log_dir / "launcher.log",
    maxBytes=2 * 1024 * 1024,
    backupCount=2,
    encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
_handlers: list[logging.Handler] = [_file_handler]
if not getattr(sys, "frozen", False):
    _handlers.append(logging.StreamHandler())
logging.basicConfig(level=logging.INFO, format=_LOG_FORMAT, handlers=_handlers)
log = logging.getLogger("launcher")


APP_VERSION = read_version()


def _hidden_run(command, **kwargs):
    """subprocess.run() with platform-appropriate hidden-window flags."""
    return subprocess.run(command, **merge_hidden_kwargs(kwargs))


def _hidden_popen(command, **kwargs):
    """subprocess.Popen() with platform-appropriate hidden-window flags."""
    return subprocess.Popen(command, **merge_hidden_kwargs(kwargs))


# ---------------------------------------------------------------------------
# Embedded Python
# ---------------------------------------------------------------------------
def _find_embedded_python() -> str:
    """Locate the embedded python-build-standalone interpreter."""
    if getattr(sys, "frozen", False):
        base = pathlib.Path(sys._MEIPASS)
    else:
        base = pathlib.Path(__file__).parent
    for path in embedded_python_candidates(base):
        if path.exists():
            return str(path)
    return sys.executable


EMBEDDED_PYTHON = _find_embedded_python()


# ---------------------------------------------------------------------------
# Windows UI runtime
# ---------------------------------------------------------------------------
_windows_dll_dir_handles: list = []


def _show_windows_message(title: str, message: str) -> None:
    if not IS_WINDOWS:
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, message, title, 0x10)
    except Exception:
        pass


def _prepare_windows_webview_runtime() -> tuple[bool, str]:
    """Prepare pythonnet/pywebview runtime before importing webview on Windows."""
    if not IS_WINDOWS:
        return True, ""

    base_dir = pathlib.Path(getattr(sys, "_MEIPASS", pathlib.Path(sys.executable).parent))
    exe_dir = pathlib.Path(sys.executable).parent
    runtime_dir = base_dir / "pythonnet" / "runtime"
    webview_lib_dir = base_dir / "webview" / "lib"
    py_dll_name = f"python{sys.version_info[0]}{sys.version_info[1]}.dll"

    def _unblock_file(path: pathlib.Path) -> None:
        try:
            os.remove(f"{path}:Zone.Identifier")
        except OSError:
            pass

    def _unblock_tree(root: pathlib.Path) -> None:
        if not root.is_dir():
            return
        for child in root.rglob("*"):
            if child.is_file() and child.suffix.lower() in {".dll", ".exe", ".pyd"}:
                _unblock_file(child)

    py_dll_candidates = [
        base_dir / py_dll_name,
        exe_dir / py_dll_name,
    ]
    for root, _dirs, files in os.walk(base_dir):
        if py_dll_name in files:
            py_dll_candidates.append(pathlib.Path(root) / py_dll_name)
            if len(py_dll_candidates) >= 6:
                break

    py_dll_path = next((path for path in py_dll_candidates if path.is_file()), None)
    runtime_dll_path = runtime_dir / "Python.Runtime.dll"
    if not runtime_dll_path.is_file():
        for root, _dirs, files in os.walk(base_dir):
            if "Python.Runtime.dll" in files:
                runtime_dll_path = pathlib.Path(root) / "Python.Runtime.dll"
                break

    if py_dll_path is None:
        return False, f"Bundled {py_dll_name} was not found."
    if not runtime_dll_path.is_file():
        return False, "Bundled Python.Runtime.dll was not found."

    _unblock_file(py_dll_path)
    _unblock_file(runtime_dll_path)
    _unblock_tree(runtime_dll_path.parent)
    _unblock_tree(webview_lib_dir)

    os.environ["PYTHONNET_RUNTIME"] = "netfx"
    os.environ["PYTHONNET_PYDLL"] = str(py_dll_path)

    search_dirs = []
    for candidate in (
        base_dir,
        exe_dir,
        runtime_dir,
        runtime_dll_path.parent,
        py_dll_path.parent,
        webview_lib_dir,
    ):
        candidate_str = str(candidate)
        if candidate.is_dir() and candidate_str not in search_dirs:
            search_dirs.append(candidate_str)

    current_path_parts = os.environ.get("PATH", "").split(os.pathsep) if os.environ.get("PATH") else []
    os.environ["PATH"] = os.pathsep.join(search_dirs + [part for part in current_path_parts if part and part not in search_dirs])

    if hasattr(os, "add_dll_directory"):
        global _windows_dll_dir_handles
        for candidate in search_dirs:
            try:
                _windows_dll_dir_handles.append(os.add_dll_directory(candidate))
            except (FileNotFoundError, OSError):
                pass

    try:
        from clr_loader import get_netfx
        from pythonnet import set_runtime

        set_runtime(get_netfx())
    except Exception as exc:
        return False, f"Windows .NET runtime init failed: {exc}"

    return True, ""


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def _bundle_dir() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys._MEIPASS)
    return pathlib.Path(__file__).parent


def _bootstrap_context() -> BootstrapContext:
    return BootstrapContext(
        bundle_dir=_bundle_dir(),
        repo_dir=REPO_DIR,
        data_dir=DATA_DIR,
        settings_path=SETTINGS_PATH,
        embedded_python=EMBEDDED_PYTHON,
        app_version=APP_VERSION,
        hidden_run=_hidden_run,
        # Launcher-driven save: owner-process action, allow_elevation=True
        # so first-launch env migration can set any runtime_mode.
        save_settings=lambda settings: save_settings(settings, allow_elevation=True),
        log=log,
    )


def check_git() -> bool:
    return _check_git(IS_WINDOWS)


def bootstrap_repo() -> None:
    _bootstrap_repo(_bootstrap_context())


def _sync_existing_repo_from_bundle() -> None:
    _sync_existing_repo_from_bundle_impl(_bootstrap_context())


def _install_deps() -> None:
    _install_deps_impl(_bootstrap_context())


# ---------------------------------------------------------------------------
# Agent process management
# ---------------------------------------------------------------------------
_agent_proc: Optional[subprocess.Popen] = None
_agent_job: Optional[object] = None
_agent_lock = threading.Lock()
_shutdown_event = threading.Event()
_webview_window = None


def start_agent(port: int = AGENT_SERVER_PORT) -> subprocess.Popen:
    """Start the agent server.py as a subprocess."""
    global _agent_proc, _agent_job

    settings = _load_settings()
    _apply_settings_to_env(settings)
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_DIR)
    saved_host = str(settings.get("OUROBOROS_SERVER_HOST") or "").strip()
    if saved_host:
        env["OUROBOROS_SERVER_HOST"] = saved_host
    env["OUROBOROS_SERVER_PORT"] = str(port)
    env["OUROBOROS_DATA_DIR"] = str(DATA_DIR)
    env["OUROBOROS_REPO_DIR"] = str(REPO_DIR)
    env["OUROBOROS_APP_VERSION"] = str(APP_VERSION)
    env["OUROBOROS_MANAGED_BY_LAUNCHER"] = "1"

    server_py = REPO_DIR / "server.py"
    log.info("Starting agent: %s %s (port=%d)", EMBEDDED_PYTHON, server_py, port)

    popen_kwargs: dict = {
        "cwd": str(REPO_DIR),
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
    }
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = (
            popen_kwargs.get("creationflags", 0)
            | _CREATE_NEW_PROCESS_GROUP
            | _CREATE_SUSPENDED
        )

    proc = _hidden_popen([EMBEDDED_PYTHON, str(server_py)], **popen_kwargs)
    _agent_proc = proc

    if IS_WINDOWS:
        job = create_kill_on_close_job()
        if job is None:
            log.error(
                "Failed to create Windows Job Object; refusing to run without process-tree ownership."
            )
            proc.kill()
            return proc
        if not assign_pid_to_job(job, proc.pid):
            log.error(
                "Failed to assign agent pid %d to Windows Job Object; refusing to run without process-tree ownership.",
                proc.pid,
            )
            close_job(job)
            proc.kill()
            return proc
        _agent_job = job
        if not resume_process(proc.pid):
            log.error("Failed to resume agent process %d — killing", proc.pid)
            with _agent_lock:
                if _agent_job is job:
                    _agent_job = None
            terminate_job(job)
            close_job(job)
            return proc
        log.info("Agent pid %d assigned to Windows Job Object", proc.pid)

    def _stream_output() -> None:
        log_path = DATA_DIR / "logs" / "agent_stdout.log"
        try:
            with open(log_path, "a", encoding="utf-8") as handle:
                for line in iter(proc.stdout.readline, b""):
                    decoded = line.decode("utf-8", errors="replace")
                    handle.write(decoded)
                    handle.flush()
        except Exception:
            pass

    threading.Thread(target=_stream_output, daemon=True).start()
    return proc


def stop_agent() -> None:
    """Gracefully stop the agent process."""
    global _agent_proc, _agent_job
    with _agent_lock:
        if _agent_proc is None:
            return
        proc = _agent_proc
        job = _agent_job
        _agent_proc = None
        _agent_job = None

    log.info("Stopping agent (pid=%s)...", proc.pid)
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if IS_WINDOWS and job is not None:
            terminate_job(job)
        else:
            proc.kill()
        proc.wait(timeout=5)
    except Exception:
        pass

    if IS_WINDOWS and job is not None:
        close_job(job)


def _read_port_file() -> int:
    """Read the active port from PORT_FILE (written by server.py)."""
    try:
        if PORT_FILE.exists():
            return int(PORT_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pass
    return AGENT_SERVER_PORT


def _kill_stale_on_port(port: int) -> None:
    """Kill any process listening on the given port (cleanup from previous runs)."""
    if IS_WINDOWS:
        kill_process_on_port(port)
        return
    try:
        result = subprocess.run(
            ["lsof", "-ti", f"tcp:{port}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        pids = result.stdout.strip().split()
        for pid_str in pids:
            try:
                pid = int(pid_str)
                if pid != os.getpid():
                    force_kill_pid(pid)
            except (TypeError, ValueError, ProcessLookupError, PermissionError, OSError):
                pass
    except Exception:
        kill_process_on_port(port)


def _wait_for_server(port: int, timeout: float = 30.0) -> bool:
    """Wait for the agent HTTP server to become responsive."""
    import urllib.request

    url = f"http://127.0.0.1:{port}/api/health"
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _poll_port_file(timeout: float = 30.0) -> int:
    """Poll port file until it's freshly written (mtime within last 10s)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if PORT_FILE.exists():
                age = time.time() - PORT_FILE.stat().st_mtime
                if age < 10:
                    return int(PORT_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pass
        time.sleep(0.5)
    return _read_port_file()


def agent_lifecycle_loop(port: int = AGENT_SERVER_PORT) -> None:
    """Main loop: start agent, monitor, restart on exit code 42 or crash."""
    global _agent_proc, _agent_job
    crash_times: list[float] = []

    _kill_stale_on_port(port)

    while not _shutdown_event.is_set():
        try:
            PORT_FILE.unlink(missing_ok=True)
        except OSError:
            pass

        proc = start_agent(port)

        actual_port = _poll_port_file(timeout=30)
        if not _wait_for_server(actual_port, timeout=45):
            log.warning("Agent server did not become responsive within 45s (port %d)", actual_port)

        proc.wait()
        exit_code = proc.returncode
        log.info("Agent exited with code %d", exit_code)

        with _agent_lock:
            _agent_proc = None
            if IS_WINDOWS and _agent_job is not None:
                close_job(_agent_job)
                _agent_job = None

        if _shutdown_event.is_set():
            break

        if exit_code == PANIC_EXIT_CODE:
            log.info("Panic stop (exit code %d) — shutting down completely.", PANIC_EXIT_CODE)
            _shutdown_event.set()
            _kill_stale_on_port(port)
            import multiprocessing as _mp

            for child in _mp.active_children():
                try:
                    force_kill_pid(child.pid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
            if _webview_window:
                try:
                    _webview_window.destroy()
                except Exception:
                    pass
            break

        time.sleep(2)

        if exit_code == RESTART_EXIT_CODE:
            log.info("Agent requested restart (exit code 42). Restarting...")
            _sync_existing_repo_from_bundle()
            _install_deps()
            _kill_stale_on_port(port)
            continue

        now = time.time()
        crash_times.append(now)
        crash_times[:] = [stamp for stamp in crash_times if (now - stamp) < CRASH_WINDOW_SEC]
        if len(crash_times) >= MAX_CRASH_RESTARTS:
            log.error("Agent crashed %d times in %ds. Stopping.", MAX_CRASH_RESTARTS, CRASH_WINDOW_SEC)
            break

        log.info("Agent crashed. Restarting in 3s...")
        _kill_stale_on_port(port)
        time.sleep(3)


# ---------------------------------------------------------------------------
# Settings and onboarding
# ---------------------------------------------------------------------------
def _load_settings() -> dict:
    return load_settings()


def _save_settings(settings: dict) -> None:
    # Launcher is the owner-process boundary: first-run wizard, env-var
    # migration, and provider-default seeds all flow through here.
    # ``allow_elevation=True`` lets the owner pick any ``OUROBOROS_RUNTIME_MODE``
    # at first launch; the agent-callable path (``api_settings_post``,
    # ``_set_tool_timeout``) keeps the default ``False``.
    save_settings(settings, allow_elevation=True)


def _request_runtime_mode_change(mode: str, confirm_fn) -> dict:
    new_mode = normalize_runtime_mode(mode)
    settings = _load_settings()
    old_mode = normalize_runtime_mode(settings.get("OUROBOROS_RUNTIME_MODE"))
    if new_mode == old_mode:
        return {"ok": True, "runtime_mode": new_mode, "restart_required": False}
    message = (
        f"Change Ouroboros runtime mode from {old_mode} to {new_mode}?\n\n"
        "This is an owner-only operation. The new mode is saved by the "
        "desktop launcher and takes effect after restart."
    )
    if not confirm_fn("Confirm Runtime Mode Change", message):
        return {"ok": False, "error": "Runtime mode change cancelled."}
    settings["OUROBOROS_RUNTIME_MODE"] = new_mode
    _save_settings(settings)
    return {"ok": True, "runtime_mode": new_mode, "restart_required": True}


def _request_skill_key_grant(skill: str, keys: list, confirm_fn) -> dict:
    from ouroboros.skill_loader import (
        find_skill,
        requested_core_setting_keys,
        save_skill_grants,
    )

    skill_name = str(skill or "").strip()
    requested = [str(k or "").strip().upper() for k in (keys or []) if str(k or "").strip()]
    loaded = find_skill(
        DATA_DIR,
        skill_name,
        repo_path=str(_load_settings().get("OUROBOROS_SKILLS_REPO_PATH") or ""),
    )
    if loaded is None:
        return {"ok": False, "error": f"Skill {skill_name!r} not found"}
    if not (loaded.manifest.is_script() or loaded.manifest.is_extension()):
        return {"ok": False, "error": "Core-key grants are supported for script and extension skills."}
    if loaded.review.status != "pass" or loaded.review.is_stale_for(loaded.content_hash):
        return {"ok": False, "error": "Key grants require a fresh PASS review."}
    allowed = requested_core_setting_keys(list(loaded.manifest.env_from_settings or []))
    if not requested or any(key not in allowed for key in requested):
        return {"ok": False, "error": f"Grant keys must be requested by the current manifest: {allowed}"}
    message = (
        f"Grant skill {loaded.name!r} access to these settings keys?\n\n"
        + "\n".join(requested)
        + "\n\nOnly grant keys to reviewed skills you trust."
    )
    if not confirm_fn("Confirm Skill Key Grant", message):
        return {"ok": False, "error": "Skill key grant cancelled."}
    save_skill_grants(
        DATA_DIR,
        loaded.name,
        requested,
        content_hash=loaded.content_hash,
        requested_keys=allowed,
    )
    # v5.2.2 dual-track grants: extensions need a runtime reconcile so
    # the just-granted core key reaches ``PluginAPIImpl.get_settings``
    # without forcing the operator to toggle disable/enable. Scripts
    # pick up the grant on the next ``skill_exec`` call automatically
    # via ``_scrub_env`` so they do not need this reload.
    #
    # Cross-process boundary: launcher.py and server.py are independent
    # OS processes. The launcher cannot mutate the server's in-process
    # ``extension_loader._extensions`` / ``_load_failures`` dicts; an
    # in-launcher ``reconcile_extension`` call would only mutate dead
    # state and additionally execute the plugin's ``register(api)``
    # inside the immutable launcher process, which violates the
    # launcher contract documented at the top of this file. We POST to
    # the agent server's loopback ``/api/skills/<skill>/reconcile``
    # endpoint instead, which clears the server's cached load failure
    # and re-runs ``load_extension`` in the right address space.
    extension_action = None
    extension_reason = None
    extension_load_error = None
    if loaded.manifest.is_extension():
        import json as _json
        import urllib.parse as _urlparse
        import urllib.request as _urlreq

        try:
            actual_port = _read_port_file() or AGENT_SERVER_PORT
            req = _urlreq.Request(
                f"http://127.0.0.1:{actual_port}/api/skills/"
                f"{_urlparse.quote(loaded.name)}/reconcile",
                method="POST",
                data=b"{}",
                headers={"Content-Type": "application/json"},
            )
            with _urlreq.urlopen(req, timeout=10) as resp:
                payload = _json.loads(resp.read().decode("utf-8") or "{}")
            extension_action = payload.get("extension_action")
            extension_reason = payload.get("extension_reason")
            extension_load_error = payload.get("load_error")
        except Exception as exc:
            log.warning(
                "Skill grant saved but server-side reconcile failed for %s: %s",
                loaded.name, exc, exc_info=True,
            )
            extension_reason = "reconcile_call_failed"
    return {
        "ok": True,
        "skill": loaded.name,
        "granted_keys": requested,
        "extension_action": extension_action,
        "extension_reason": extension_reason,
        "load_error": extension_load_error,
    }


def _claude_code_status_payload(settings: dict | None = None) -> dict:
    current_settings = settings or _load_settings()
    _apply_settings_to_env(current_settings)

    from ouroboros.platform_layer import resolve_claude_runtime

    rt = resolve_claude_runtime()
    stderr_tail = ""
    try:
        from ouroboros.gateways.claude_code import get_last_stderr as _get_last_stderr

        stderr_tail = _get_last_stderr(max_chars=2000)
    except Exception:
        pass

    message_map = {
        "ready": f"Claude runtime ready (SDK {rt.sdk_version}, CLI {rt.cli_version})",
        "no_api_key": (
            f"Claude runtime available (SDK {rt.sdk_version}) but ANTHROPIC_API_KEY is not set. Add it in Settings."
        ),
        "error": f"Claude runtime error: {rt.error}",
        "degraded": (
            f"Claude runtime degraded (SDK {rt.sdk_version}, CLI {'found' if rt.cli_path else 'missing'}). Try Repair."
        ),
        "missing": "Claude runtime not available. Use Repair in Settings or reinstall the app.",
    }

    return {
        "status": rt.status_label(),
        "installed": bool(rt.sdk_version),
        "ready": rt.ready,
        "busy": False,
        "version": rt.sdk_version,
        "cli_version": rt.cli_version,
        "cli_path": rt.cli_path,
        "interpreter_path": rt.interpreter_path,
        "app_managed": rt.app_managed,
        "legacy_detected": rt.legacy_detected,
        "legacy_sdk_version": rt.legacy_sdk_version,
        "api_key_set": rt.api_key_set,
        "message": message_map.get(rt.status_label(), f"Claude runtime: {rt.status_label()}"),
        "error": rt.error,
        "stderr_tail": stderr_tail,
    }


def _run_first_run_wizard() -> bool:
    """Show setup wizard if no runtime provider or local model is configured."""
    settings, provider_defaults_changed, _provider_default_keys = apply_runtime_provider_defaults(_load_settings())
    if provider_defaults_changed:
        _save_settings(settings)
    _apply_settings_to_env(settings)
    if has_startup_ready_provider(settings):
        return True

    import webview

    _wizard_done = {"ok": False}

    class WizardApi:
        def save_wizard(self, data: dict) -> str:
            prepared_settings, error = prepare_onboarding_settings(data, settings)
            if error:
                return error
            settings.update(prepared_settings)
            settings.update(apply_runtime_provider_defaults(settings)[0])
            try:
                _save_settings(settings)
                _apply_settings_to_env(settings)
                _wizard_done["ok"] = True
                for window in webview.windows:
                    window.destroy()
                return "ok"
            except Exception as exc:
                return f"Failed to save: {exc}"

        def claude_code_status(self) -> dict:
            return _claude_code_status_payload(settings)

        def install_claude_code(self) -> dict:
            _apply_settings_to_env(settings)
            repaired = _verify_claude_runtime(_bootstrap_context())
            payload = _claude_code_status_payload(settings)
            payload["repaired"] = repaired
            if not repaired:
                payload["status"] = "error"
                payload["ready"] = False
                payload["busy"] = False
                payload["message"] = "Claude runtime repair failed."
                if not payload.get("error"):
                    payload["error"] = "Failed to install/update claude-agent-sdk in the embedded runtime."
            return payload

    webview.create_window(
        "Ouroboros — Setup",
        html=build_onboarding_html(settings, host_mode="desktop"),
        js_api=WizardApi(),
        width=980,
        height=780,
        min_size=(840, 640),
    )
    webview.start()
    return _wizard_done["ok"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    if IS_WINDOWS:
        ok, reason = _prepare_windows_webview_runtime()
        if not ok:
            log.error("Windows UI runtime initialization failed: %s", reason)
            _show_windows_message(
                "Ouroboros — Startup Failed",
                "Windows UI runtime initialization failed.\n\n"
                f"{reason}\n\n"
                "Check launcher.log for details.",
            )
            return

    import webview

    if not acquire_pid_lock():
        log.error("Another instance already running.")
        webview.create_window(
            "Ouroboros",
            html="<html><body style='background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
            "<div style='text-align:center'><h2>Ouroboros is already running</h2><p>Only one instance can run at a time.</p></div></body></html>",
            width=420,
            height=200,
        )
        webview.start()
        return

    import atexit

    atexit.register(release_pid_lock)

    if not check_git():
        log.warning("Git not found.")
        _hint = git_install_hint()
        _install_status = (
            "Installing... A system dialog may appear."
            if IS_MACOS
            else "Installing... Please wait."
        )

        def _git_page(window):
            window.evaluate_js(
                """
                document.getElementById('install-btn').onclick = function() {
                    document.getElementById('status').textContent = '__INSTALL_STATUS__';
                    window.pywebview.api.install_git();
                };
                """.replace("__INSTALL_STATUS__", _install_status)
            )

        class GitApi:
            def install_git(self):
                if IS_MACOS:
                    subprocess.Popen(["xcode-select", "--install"])
                elif IS_WINDOWS:
                    _hidden_popen(
                        ["winget", "install", "Git.Git", "--source", "winget", "--accept-source-agreements"]
                    )
                else:
                    for cmd in (
                        ["sudo", "apt", "install", "-y", "git"],
                        ["sudo", "dnf", "install", "-y", "git"],
                    ):
                        try:
                            _hidden_popen(cmd)
                            break
                        except FileNotFoundError:
                            continue
                for _ in range(300):
                    time.sleep(3)
                    if shutil.which("git"):
                        return "installed"
                return "timeout"

        git_window = webview.create_window(
            "Ouroboros — Setup Required",
            html=(
                """<html><body style="background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center">
                <h2>Git is required</h2>
                <p>Ouroboros needs Git to manage its local repository.</p>
                <button id="install-btn" style="padding:10px 24px;border-radius:8px;border:none;background:#0ea5e9;color:white;cursor:pointer;font-size:14px">
                    Install Git (Xcode CLI Tools)
                </button>
                <p id="status" style="color:#fbbf24;margin-top:12px"></p>
            </div></body></html>"""
                if IS_MACOS
                else f"""<html><body style="background:#1a1a2e;color:white;font-family:system-ui;display:flex;align-items:center;justify-content:center;height:100vh;margin:0">
            <div style="text-align:center">
                <h2>Git is required</h2>
                <p>Ouroboros needs Git to manage its local repository.</p>
                <p style="color:#94a3b8;font-size:13px;margin-top:8px">{_hint}</p>
                <button id="install-btn" style="padding:10px 24px;border-radius:8px;border:none;background:#0ea5e9;color:white;cursor:pointer;font-size:14px;margin-top:12px">
                    Install Git
                </button>
                <p id="status" style="color:#fbbf24;margin-top:12px"></p>
            </div></body></html>"""
            ),
            js_api=GitApi(),
            width=520,
            height=300,
        )
        webview.start(func=_git_page, args=[git_window])
        if not check_git():
            sys.exit(1)

    bootstrap_repo()

    if not _run_first_run_wizard():
        log.info("Wizard was closed without saving. Launching anyway (Settings page available).")

    global _webview_window
    port = AGENT_SERVER_PORT

    lifecycle_thread = threading.Thread(target=agent_lifecycle_loop, args=(port,), daemon=True)
    lifecycle_thread.start()

    server_ready = _wait_for_server(port, timeout=15)
    actual_port = _read_port_file()
    if actual_port != port:
        server_ready = _wait_for_server(actual_port, timeout=45)
    else:
        server_ready = server_ready or _wait_for_server(port, timeout=45)

    if not server_ready:
        log.error("Agent failed to become healthy on port %d; aborting UI startup.", actual_port)
        _shutdown_event.set()
        stop_agent()
        lifecycle_thread.join(timeout=5)
        webview.create_window(
            "Ouroboros — Startup Failed",
            html=(
                "<html><body style='background:#1a1a2e;color:white;font-family:system-ui;"
                "display:flex;align-items:center;justify-content:center;height:100vh;margin:0'>"
                "<div style='text-align:center;max-width:460px;padding:24px'>"
                "<h2>Ouroboros failed to start</h2>"
                "<p>The local agent server did not become ready.</p>"
                "<p style='color:#94a3b8;font-size:13px;margin-top:10px'>"
                "Check launcher.log and agent_stdout.log in the Ouroboros data directory "
                "for details.</p>"
                "</div></body></html>"
            ),
            width=520,
            height=260,
        )
        webview.start()
        return

    class MainApi:
        def request_runtime_mode_change(self, mode: str) -> dict:
            try:
                return _request_runtime_mode_change(
                    mode,
                    lambda title, message: bool(
                        _webview_window and _webview_window.create_confirmation_dialog(title, message)
                    ),
                )
            except Exception as exc:
                log.warning("Runtime mode native confirmation failed: %s", exc, exc_info=True)
                return {"ok": False, "error": f"Native confirmation failed: {exc}"}

        def request_skill_key_grant(self, skill: str, keys: list) -> dict:
            try:
                return _request_skill_key_grant(
                    skill,
                    keys,
                    lambda title, message: bool(
                        _webview_window and _webview_window.create_confirmation_dialog(title, message)
                    ),
                )
            except Exception as exc:
                log.warning("Skill grant native confirmation failed: %s", exc, exc_info=True)
                return {"ok": False, "error": f"Native confirmation failed: {exc}"}

    url = f"http://127.0.0.1:{actual_port}"
    window = webview.create_window(
        f"Ouroboros v{APP_VERSION}",
        url=url,
        js_api=MainApi(),
        width=1100,
        height=750,
        min_size=(800, 500),
        background_color="#0d0b0f",
        text_select=True,
    )

    def _kill_orphaned_children() -> None:
        """Final safety net: kill any processes still on the server port."""
        _kill_stale_on_port(port)
        _kill_stale_on_port(8766)
        for child in __import__("multiprocessing").active_children():
            try:
                force_kill_pid(child.pid)
                log.info("Killed orphaned child pid=%d", child.pid)
            except (ProcessLookupError, PermissionError, OSError):
                pass

    def _on_closing() -> None:
        log.info("Window closing — graceful shutdown.")
        _shutdown_event.set()
        stop_agent()
        _kill_orphaned_children()
        release_pid_lock()
        os._exit(0)

    window.events.closing += _on_closing
    _webview_window = window

    webview.start(debug=False)


if __name__ == "__main__":
    from multiprocessing import freeze_support

    freeze_support()

    if sys.platform == "darwin":
        try:
            shell_path = subprocess.check_output(
                ["/bin/bash", "-l", "-c", "echo $PATH"],
                text=True,
                timeout=5,
            ).strip()
            if shell_path:
                os.environ["PATH"] = shell_path
        except Exception:
            pass

    main()
