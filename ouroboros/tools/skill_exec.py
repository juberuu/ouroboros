"""Skill execution substrate + skill-lifecycle tools (Phase 3).

Exposes four tools to the agent:

- ``list_skills``   — catalogue view (no filesystem side effects).
- ``review_skill``  — run tri-model review against a single skill.
- ``toggle_skill``  — flip the durable ``enabled.json`` bit for a skill.
- ``skill_exec``    — execute a script from a reviewed + enabled skill.

Design rules (per the Phase 3 plan):

- ``skill_exec`` is a **separate substrate**, not a ``run_shell`` reuse.
  It never spawns a user-supplied string command; callers pick a script
  name declared by the skill manifest, and the runtime resolves that name
  to the exact on-disk file inside the skill directory.
- Only skills that are enabled, whose review status is ``pass``, and
  whose review is NOT stale against the current content hash can execute.
  ``type: extension`` skills are deferred until Phase 4.
- The subprocess runs with ``cwd=skill_dir``, a scrubbed environment, a
  timeout (from the manifest, hard-capped at 300s), and bounded stdout /
  stderr so a misbehaving skill cannot flood the runtime logs.
- The runtime allowlist is ``python``/``python3``/``bash``/``node``;
  anything else is rejected up-front.
- Runtime-mode gate: ``light`` blocks execution entirely (the whole
  point of light mode is "no agent-initiated side effects beyond reading");
  ``advanced`` and ``pro`` both allow reviewed skills to execute.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.config import get_runtime_mode, get_skills_repo_path, load_settings
from ouroboros.skill_loader import (
    VALID_REVIEW_STATUSES,
    compute_content_hash,
    discover_skills,
    find_skill,
    save_enabled,
    summarize_skills,
)
from ouroboros.skill_review import review_skill as _review_skill_impl
from ouroboros.tools.registry import ToolContext, ToolEntry

# Reuse the panic-integrated tracked-subprocess runner so skills spawned
# by ``skill_exec`` participate in the same process-group tracking as
# ``run_shell``/``claude_code_edit``. Without this, a long-running skill
# would not be killed by ``/panic`` → Emergency Stop Invariant violation.
from ouroboros.tools.shell import (
    _active_subprocesses,
    _subprocess_lock,
    _kill_process_group,
)
from subprocess import Popen
from ouroboros.platform_layer import merge_hidden_kwargs, subprocess_new_group_kwargs

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Execution policy
# ---------------------------------------------------------------------------

# Hard ceiling regardless of the skill manifest's timeout_sec. Anything
# longer than this is bundled into a background task by the runtime loop
# — ``skill_exec`` is for bounded, synchronous helper calls, not for
# long-running worker tasks.
_HARD_TIMEOUT_CEILING_SEC = 300
_DEFAULT_TIMEOUT_SEC = 60
_MAX_STDOUT_BYTES = 64 * 1024
_MAX_STDERR_BYTES = 32 * 1024

_ALLOWED_RUNTIMES = {
    # Cross-platform compatibility: ``python3`` is the canonical declared
    # runtime but Windows and some minimal Linux installs only ship
    # ``python.exe`` / ``python``. Fall back to ``python`` so a skill
    # declaring ``runtime: python3`` works on every supported OS.
    "python": ("python", "python3"),
    "python3": ("python3", "python"),
    "bash": ("bash",),
    "node": ("node",),
}

# Environment keys that are always passed through to a skill subprocess
# regardless of ``env_from_settings``. These are OS-level, not application
# state, and removing them would break basic ``python`` / ``node`` / ``bash``
# invocations on many systems.
_ALWAYS_FORWARDED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "SYSTEMROOT",  # Windows
        "TMPDIR",
        "TMP",
        "TEMP",
    }
)

# Hard denylist of settings keys we refuse to forward via
# ``env_from_settings`` regardless of what the manifest requests. The
# first layer of defence is the tri-model skill review (``env_allowlist``
# checklist item), but that depends on reviewer perfection. Keeping a
# runtime denylist here means a missed review cannot leak production
# credentials / tokens / the network-gate password to an executing
# skill. Skills that genuinely need an API key should talk to the
# ``main`` Ouroboros process rather than receive it as a subprocess
# envvar.
_FORBIDDEN_ENV_FORWARD_KEYS = frozenset(
    {
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "GITHUB_TOKEN",
        "OUROBOROS_NETWORK_PASSWORD",
    }
)


def _resolve_runtime_binary(runtime: str) -> Optional[str]:
    """Return the absolute path to the binary implementing the runtime.

    Uses ``shutil.which`` first (the common case on developer machines
    where ``python3`` / ``node`` / ``bash`` live on PATH). For
    packaged / frozen builds (``_FROZEN_TOOL_MODULES`` now includes
    ``skill_exec`` so the tool ships inside the app bundle too), we
    additionally fall back to ``sys.executable`` for ``python`` /
    ``python3`` requests so skills declaring the default Python
    runtime still work even when the bundled ``python-standalone``
    interpreter is not on PATH.
    """
    import sys
    candidates = _ALLOWED_RUNTIMES.get(runtime or "", ())
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    # Packaged-build fallback: the bundled Python interpreter is always
    # available via ``sys.executable`` even when not on PATH. Mirrors
    # ``claude_code_edit`` / ``ouroboros/platform_layer.py`` which use
    # the same trick for the app-managed Claude runtime.
    if runtime in ("python", "python3") and sys.executable:
        resolved = pathlib.Path(sys.executable)
        if resolved.is_file():
            return str(resolved)
    return None


def _scrub_env(
    manifest_env_keys: List[str],
    skill_state_dir_path: pathlib.Path,
    skill_name: str,
) -> Dict[str, str]:
    """Build a minimal env for the subprocess.

    Starts empty, adds always-forwarded OS keys, then copies user-approved
    settings keys listed in the manifest's ``env_from_settings`` (loaded
    live from settings.json so key-rotation propagates without a restart).
    Also exposes the per-skill state directory under
    ``OUROBOROS_SKILL_STATE_DIR`` so scripts have a documented writable
    location.
    """
    env: Dict[str, str] = {}
    for key in _ALWAYS_FORWARDED_ENV:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    if manifest_env_keys:
        settings = load_settings()
        allow = {str(k).strip() for k in manifest_env_keys if str(k).strip()}
        for key in allow:
            if key in _FORBIDDEN_ENV_FORWARD_KEYS:
                # Runtime denylist — never forward known credentials /
                # tokens / the network-gate password, regardless of
                # what the manifest asked for or what review decided.
                log.warning(
                    "Skill %s asked env_from_settings for %s; refusing by runtime denylist.",
                    skill_name, key,
                )
                continue
            val = settings.get(key)
            if val is None or val == "":
                continue
            env[key] = str(val)
    env["OUROBOROS_SKILL_NAME"] = skill_name
    env["OUROBOROS_SKILL_STATE_DIR"] = str(skill_state_dir_path)
    return env


def _drain_pipe_with_cap(pipe, cap: int, buf: bytearray, overflow_flag: Dict[str, bool], label: str) -> None:
    """Read from ``pipe`` into ``buf`` up to ``cap`` bytes.

    Stops reading (and flips ``overflow_flag[label]``) the moment the
    buffer exceeds the cap so a pathological skill that writes
    gigabytes to stdout cannot exhaust runtime memory. The caller is
    expected to terminate the subprocess once either overflow flag
    fires (skill_exec does exactly that via ``_kill_process_group``).
    """
    try:
        while True:
            chunk = pipe.read(4096)
            if not chunk:
                return
            remaining = cap - len(buf)
            if remaining <= 0:
                overflow_flag[label] = True
                return
            if len(chunk) > remaining:
                buf.extend(chunk[:remaining])
                overflow_flag[label] = True
                return
            buf.extend(chunk)
    except (OSError, ValueError):
        # Pipe closed mid-read — normal during kill.
        return


def _run_skill_subprocess(
    cmd: List[str],
    *,
    cwd: str,
    env: Dict[str, str],
    timeout_sec: int,
    stdout_cap: int,
    stderr_cap: int,
) -> Tuple[int, bytes, bytes, bool]:
    """Spawn a skill subprocess with byte-capped stdout/stderr streaming.

    Returns ``(returncode, stdout_bytes, stderr_bytes, overflowed)``.
    ``overflowed`` is True when either stream's cap was hit — in that
    case the process tree was killed; ``returncode`` is whatever the
    OS returned (often a negative signal number on SIGKILL / SIGTERM).

    Raises ``subprocess.TimeoutExpired`` on wall-clock timeout (with
    the partial stdout/stderr available via ``exc.stdout``/``exc.stderr``).
    Raises ``FileNotFoundError`` when the runtime binary disappears
    between resolution and spawn, matching ``subprocess.run`` semantics.
    """
    popen_kwargs: Dict[str, Any] = {
        "cwd": cwd,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
    }
    popen_kwargs.update(subprocess_new_group_kwargs())
    # Suppress the ugly per-skill console window on Windows. ``merge_hidden_kwargs``
    # is a no-op on Unix and bitwise-ORs ``creationflags`` on Windows so the
    # process-group flag from ``subprocess_new_group_kwargs`` is preserved.
    popen_kwargs = merge_hidden_kwargs(popen_kwargs)
    proc = Popen(cmd, **popen_kwargs)  # noqa: S603 — cmd is a vetted list, not shell
    with _subprocess_lock:
        _active_subprocesses.add(proc)

    stdout_buf = bytearray()
    stderr_buf = bytearray()
    overflow_flag = {"stdout": False, "stderr": False}

    stdout_thread = threading.Thread(
        target=_drain_pipe_with_cap,
        args=(proc.stdout, stdout_cap, stdout_buf, overflow_flag, "stdout"),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_pipe_with_cap,
        args=(proc.stderr, stderr_cap, stderr_buf, overflow_flag, "stderr"),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    deadline = time.monotonic() + max(1, int(timeout_sec))
    overflowed = False
    timed_out = False
    try:
        while True:
            # Overflow? Kill the tree immediately so a noisy/malicious
            # skill cannot keep filling dropped-on-the-floor pipes.
            if overflow_flag["stdout"] or overflow_flag["stderr"]:
                overflowed = True
                _kill_process_group(proc)
                break
            if proc.poll() is not None:
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _kill_process_group(proc)
                break
            time.sleep(0.05)
        # Wait briefly for pipe drain / reaper; don't block forever
        # even if something went sideways.
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_process_group(proc)
            proc.wait(timeout=2)
        stdout_thread.join(timeout=2)
        stderr_thread.join(timeout=2)
    finally:
        with _subprocess_lock:
            _active_subprocesses.discard(proc)
        try:
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()
        except OSError:
            pass

    if timed_out:
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=timeout_sec,
            output=bytes(stdout_buf),
            stderr=bytes(stderr_buf),
        )
    return proc.returncode or 0, bytes(stdout_buf), bytes(stderr_buf), overflowed


def _bound_timeout(requested_sec: Any) -> int:
    try:
        timeout = int(requested_sec)
    except (TypeError, ValueError):
        timeout = _DEFAULT_TIMEOUT_SEC
    if timeout <= 0:
        timeout = _DEFAULT_TIMEOUT_SEC
    return min(timeout, _HARD_TIMEOUT_CEILING_SEC)


def _cap(data: bytes, limit: int, label: str) -> str:
    text = data.decode("utf-8", errors="replace")
    if len(data) <= limit:
        return text
    return (
        text[:limit]
        + f"\n\n⚠️ OMISSION NOTE: skill_exec truncated {label} at "
        f"{limit} bytes (total {len(data)})."
    )


def _resolve_script_path(
    skill_dir: pathlib.Path,
    script_rel: str,
    *,
    reviewed_paths: Optional[List[pathlib.Path]] = None,
) -> Optional[pathlib.Path]:
    """Resolve ``script_rel`` against ``skill_dir``, blocking path escape.

    When ``reviewed_paths`` is supplied, the resolved script must also be
    a member of that set. This is the "executable surface == reviewed
    surface" invariant: the content hash + the review pack cover the
    manifest + manifest-declared ``entry`` + ``scripts/`` + ``assets/``,
    so ``skill_exec`` must refuse to execute anything outside those
    reviewed files (e.g. a stray ``skill_dir/helper.py`` the user dropped
    post-review). Without the match the PASS verdict would cover code
    that never went through tri-model review.

    Returns ``None`` on any failure (escape, missing file, or not in the
    reviewed set).
    """
    rel = (script_rel or "").strip()
    if not rel or rel.startswith("/") or rel.startswith("~"):
        return None
    if ".." in pathlib.PurePosixPath(rel).parts:
        return None
    candidate = (skill_dir / rel).resolve()
    try:
        candidate.relative_to(skill_dir.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    if reviewed_paths is not None:
        reviewed = {p.resolve() for p in reviewed_paths}
        if candidate not in reviewed:
            return None
    return candidate


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


def _skill_tool_preflight(
    ctx: ToolContext,
) -> Optional[str]:
    """Return an error string when the skill surface is unavailable.

    The skill tools are only meaningful when ``OUROBOROS_SKILLS_REPO_PATH``
    is configured. We surface a gentle message rather than crashing so
    the agent knows why the surface is empty.
    """
    repo_path = get_skills_repo_path()
    if not repo_path:
        return (
            "⚠️ SKILLS_UNAVAILABLE: OUROBOROS_SKILLS_REPO_PATH is not "
            "configured. Point it at a local checkout in Settings → "
            "Behavior → External Skills Repo first."
        )
    return None


def _handle_list_skills(ctx: ToolContext, **_kwargs: Any) -> str:
    err = _skill_tool_preflight(ctx)
    if err:
        return err
    drive_root = pathlib.Path(ctx.drive_root)
    summary = summarize_skills(drive_root)
    return json.dumps(summary, ensure_ascii=False, indent=2)


def _handle_review_skill(ctx: ToolContext, skill: str = "", **_kwargs: Any) -> str:
    err = _skill_tool_preflight(ctx)
    if err:
        return err
    skill_name = str(skill or "").strip()
    if not skill_name:
        return "⚠️ SKILL_REVIEW_ERROR: 'skill' argument is required."
    outcome = _review_skill_impl(ctx, skill_name)
    payload = {
        "skill": outcome.skill_name,
        "status": outcome.status,
        "content_hash": outcome.content_hash,
        "reviewer_models": outcome.reviewer_models,
        "findings": outcome.findings,
        "error": outcome.error,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _handle_skill_exec(
    ctx: ToolContext,
    skill: str = "",
    script: str = "",
    args: Optional[List[str]] = None,
    **_kwargs: Any,
) -> str:
    err = _skill_tool_preflight(ctx)
    if err:
        return err

    runtime_mode = get_runtime_mode()
    if runtime_mode == "light":
        return (
            "⚠️ SKILL_EXEC_BLOCKED: runtime_mode=light disables skill "
            "execution. Switch to 'advanced' or 'pro' in Settings → "
            "Behavior → Runtime Mode to allow reviewed skills to run."
        )

    skill_name = str(skill or "").strip()
    script_rel = str(script or "").strip()
    if not skill_name or not script_rel:
        return "⚠️ SKILL_EXEC_ERROR: both 'skill' and 'script' are required."

    drive_root = pathlib.Path(ctx.drive_root)
    loaded = find_skill(drive_root, skill_name)
    if loaded is None:
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} not found in "
            "OUROBOROS_SKILLS_REPO_PATH."
        )
    if loaded.load_error:
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} manifest is broken "
            f"({loaded.load_error}). Fix the skill package and re-review."
        )
    if loaded.manifest.is_extension():
        return (
            f"⚠️ SKILL_EXEC_DEFERRED: skill {skill_name!r} is an extension "
            "(type=extension). Extension execution lands in Phase 4."
        )
    # Phase 3 ``skill_exec`` only executes ``type: script`` skills.
    # ``instruction`` skills are catalogued + reviewable but have no
    # executable payload by design (their manifest declares no scripts).
    # Refusing here keeps the executable surface == ``manifest.scripts``
    # and prevents the reviewer-executor mismatch the scope reviewer
    # flagged in Phase 3 round 4.
    if not loaded.manifest.is_script():
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} has type "
            f"{loaded.manifest.type!r}. Only 'script' skills can execute "
            "via skill_exec in Phase 3."
        )
    if not loaded.enabled:
        return (
            f"⚠️ SKILL_EXEC_BLOCKED: skill {skill_name!r} is disabled. "
            "Enable it after review in the Skills UI (Phase 5) or via "
            "the dedicated enable tool."
        )
    current_hash = compute_content_hash(
        loaded.skill_dir,
        manifest_entry=loaded.manifest.entry,
        manifest_scripts=loaded.manifest.scripts,
    )
    if loaded.review.is_stale_for(current_hash):
        return (
            f"⚠️ SKILL_EXEC_BLOCKED: skill {skill_name!r} was edited since "
            f"the last review. Re-run review_skill(skill={skill_name!r}) "
            "before executing."
        )
    if loaded.review.status != "pass":
        return (
            f"⚠️ SKILL_EXEC_BLOCKED: skill {skill_name!r} review status is "
            f"'{loaded.review.status}', not 'pass'. Run review_skill and "
            "resolve findings before executing."
        )

    runtime = (loaded.manifest.runtime or "").strip().lower()
    runtime_binary = _resolve_runtime_binary(runtime)
    if runtime_binary is None:
        return (
            f"⚠️ SKILL_EXEC_ERROR: skill {skill_name!r} declared runtime "
            f"{runtime!r} is not in the allowlist {sorted(set(_ALLOWED_RUNTIMES))} "
            "or the matching binary is not on PATH."
        )

    # Keep the executable surface identical to the manifest-declared
    # ``scripts`` list — NOT the full reviewed file set. SKILL.md body and
    # assets/* are part of the reviewed content hash (so editing them
    # correctly invalidates the PASS verdict), but they are not executable
    # payload and must not be invokable via ``skill_exec``. Resolve each
    # declared script ``name`` against the skill directory once, up-front.
    # Manifest authors may write either a bare filename (``fetch.py``,
    # expected under ``scripts/``) or an explicit relative path
    # (``scripts/fetch.py``); both forms are accepted here.
    # Canonicalise a manifest ``scripts[].name`` to exactly one resolved
    # filesystem path. A bare name (``fetch.py``) always means
    # ``scripts/fetch.py`` — never a top-level shadow file of the same
    # name — so execution cannot depend on an accidentally-present
    # top-level ``hello.py`` sitting next to the real ``scripts/hello.py``.
    # Explicit paths (``name: bin/run.sh``) resolve verbatim. If BOTH
    # forms would resolve for a given declared name (e.g. ``hello.py``
    # exists both at top level and under ``scripts/``), we pick the
    # ``scripts/`` form and keep a note that the top-level file is
    # reviewed content but NOT executable.
    def _canonical_declared_path(declared_name: str) -> Optional[pathlib.Path]:
        name = declared_name.strip()
        if not name:
            return None
        if "/" in name or name.startswith("."):
            return _resolve_script_path(loaded.skill_dir, name)
        # Bare name — mandate the ``scripts/`` prefix.
        return _resolve_script_path(loaded.skill_dir, f"scripts/{name}")

    declared_scripts: List[pathlib.Path] = []
    declared_by_name: Dict[str, pathlib.Path] = {}
    for entry in loaded.manifest.scripts or []:
        if not isinstance(entry, dict):
            continue
        declared_name = str(entry.get("name") or "").strip()
        if not declared_name:
            continue
        canonical = _canonical_declared_path(declared_name)
        if canonical is None:
            continue
        if canonical not in declared_scripts:
            declared_scripts.append(canonical)
        declared_by_name[declared_name] = canonical
        # Also index by the explicit ``scripts/<name>`` spelling so a
        # caller that passes ``script="scripts/hello.py"`` matches the
        # same canonical target.
        if "/" not in declared_name:
            declared_by_name[f"scripts/{declared_name}"] = canonical

    # Look up the caller's script argument in the declared-name index
    # first, then fall back to the path-based check for callers that
    # pass an explicit relative path that happens to coincide with a
    # declared script path.
    script_path: Optional[pathlib.Path] = declared_by_name.get(script_rel.strip())
    if script_path is None:
        script_path = _resolve_script_path(
            loaded.skill_dir, script_rel, reviewed_paths=declared_scripts
        )
    if script_path is None:
        return (
            f"⚠️ SKILL_EXEC_ERROR: script {script_rel!r} is not a declared "
            "script for this skill. Only names listed under the manifest's "
            "``scripts:`` array can execute via skill_exec (assets/* and "
            "SKILL.md body are reviewed content but not executable payload). "
            "Add the script to the manifest and re-run review_skill."
        )

    cmd = [runtime_binary, str(script_path)]
    if args is None:
        extra_args: List[Any] = []
    elif isinstance(args, str):
        # Mis-serialized by the caller (``args="alpha"`` would expand to
        # per-char argv under ``list(args)``). Reject explicitly.
        return (
            "⚠️ SKILL_EXEC_ERROR: 'args' must be a list of scalar "
            "strings/numbers, not a single string. Wrap as ['alpha'] "
            "for a one-element argv."
        )
    elif isinstance(args, (list, tuple)):
        extra_args = list(args)
    else:
        return (
            "⚠️ SKILL_EXEC_ERROR: 'args' must be a list of scalar "
            f"strings/numbers. Got {type(args).__name__}={args!r}."
        )
    for arg in extra_args:
        if not isinstance(arg, (str, int, float)) or isinstance(arg, bool):
            return (
                "⚠️ SKILL_EXEC_ERROR: args must be a list of scalar "
                f"strings/numbers. Element {arg!r} ({type(arg).__name__}) "
                "is not allowed."
            )
        cmd.append(str(arg))

    timeout = _bound_timeout(loaded.manifest.timeout_sec)
    from ouroboros.skill_loader import skill_state_dir

    state_dir = skill_state_dir(drive_root, loaded.name)
    env = _scrub_env(
        manifest_env_keys=list(loaded.manifest.env_from_settings or []),
        skill_state_dir_path=state_dir,
        skill_name=loaded.name,
    )

    try:
        returncode, stdout_bytes, stderr_bytes, overflowed = _run_skill_subprocess(
            cmd,
            cwd=str(loaded.skill_dir),
            env=env,
            timeout_sec=timeout,
            stdout_cap=_MAX_STDOUT_BYTES,
            stderr_cap=_MAX_STDERR_BYTES,
        )
    except subprocess.TimeoutExpired as exc:
        return (
            f"⚠️ SKILL_EXEC_TIMEOUT: skill {skill_name!r} script "
            f"{script_rel!r} exceeded {timeout}s limit.\n"
            f"stdout_partial:\n{_cap(exc.stdout or b'', _MAX_STDOUT_BYTES, 'stdout')}\n"
            f"stderr_partial:\n{_cap(exc.stderr or b'', _MAX_STDERR_BYTES, 'stderr')}"
        )
    except FileNotFoundError:
        return (
            f"⚠️ SKILL_EXEC_ERROR: runtime binary {runtime_binary!r} is no "
            "longer available."
        )
    except OSError as exc:
        return f"⚠️ SKILL_EXEC_ERROR: OS error running skill: {exc}"

    # ``overflowed`` means we killed the skill because it exceeded the
    # per-stream byte cap. The buffers are already bounded by that cap,
    # so ``_cap()`` below is a no-op safety net that ALSO appends the
    # human-readable OMISSION NOTE the downstream consumer expects.
    payload = {
        "skill": loaded.name,
        "script": script_rel,
        "runtime": runtime,
        "exit_code": int(returncode),
        "timeout_sec": timeout,
        "output_overflow": overflowed,
        "stdout": _cap(stdout_bytes, _MAX_STDOUT_BYTES, "stdout"),
        "stderr": _cap(stderr_bytes, _MAX_STDERR_BYTES, "stderr"),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if overflowed:
        # Process was killed for flooding stdout/stderr — surface a
        # dedicated sentinel so the model does not confuse it with a
        # normal successful run.
        return (
            f"⚠️ SKILL_EXEC_OVERFLOW: skill {loaded.name!r} script "
            f"{script_rel!r} exceeded stdout/stderr byte caps "
            f"(stdout<={_MAX_STDOUT_BYTES}B, stderr<={_MAX_STDERR_BYTES}B) "
            "and was killed.\n\n" + rendered
        )
    if returncode != 0:
        return (
            f"⚠️ SKILL_EXEC_FAILED: skill {loaded.name!r} script "
            f"{script_rel!r} exited with code {returncode}.\n\n"
            + rendered
        )
    return rendered


_TRUE_LITERALS = {"true", "yes", "on", "1"}
_FALSE_LITERALS = {"false", "no", "off", "0"}


def _coerce_bool_arg(value: Any) -> Optional[bool]:
    """Strictly coerce an LLM tool argument to a bool.

    Returns ``None`` for values that are not unambiguously boolean — so
    the handler can reject malformed input instead of silently running
    ``bool("false") == True`` and flipping enabled ON.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_LITERALS:
            return True
        if lowered in _FALSE_LITERALS:
            return False
    return None


def _handle_toggle_skill(
    ctx: ToolContext,
    skill: str = "",
    enabled: Any = None,
    **_kwargs: Any,
) -> str:
    err = _skill_tool_preflight(ctx)
    if err:
        return err
    skill_name = str(skill or "").strip()
    if not skill_name:
        return "⚠️ SKILL_TOGGLE_ERROR: 'skill' argument is required."
    if enabled is None:
        return "⚠️ SKILL_TOGGLE_ERROR: 'enabled' (true|false) is required."
    coerced = _coerce_bool_arg(enabled)
    if coerced is None:
        return (
            "⚠️ SKILL_TOGGLE_ERROR: 'enabled' must be a boolean or one of "
            f"{sorted(_TRUE_LITERALS | _FALSE_LITERALS)}. "
            f"Got {enabled!r} ({type(enabled).__name__})."
        )
    drive_root = pathlib.Path(ctx.drive_root)
    loaded = find_skill(drive_root, skill_name)
    if loaded is None:
        return (
            f"⚠️ SKILL_TOGGLE_ERROR: skill {skill_name!r} not found in "
            "OUROBOROS_SKILLS_REPO_PATH."
        )
    # Mirror the skill_exec / review_skill guards: a skill flagged with
    # ``load_error`` (broken manifest, sanitised-name collision, etc.)
    # must not have its state mutated through the tool surface. The
    # durable state dir would otherwise be shared between two colliding
    # directories, exactly the bug ``discover_skills`` is trying to
    # surface.
    if loaded.load_error:
        return (
            f"⚠️ SKILL_TOGGLE_ERROR: skill {skill_name!r} cannot be toggled "
            f"— loader rejected it ({loaded.load_error})."
        )
    save_enabled(drive_root, loaded.name, coerced)
    note = ""
    if coerced and loaded.review.status != "pass":
        note = (
            " (skill will remain non-executable until review_skill "
            f"returns 'pass'; current status: {loaded.review.status!r})"
        )
    return json.dumps(
        {
            "skill": loaded.name,
            "enabled": coerced,
            "review_status": loaded.review.status,
            "message": f"Skill {loaded.name!r} enabled={coerced}{note}",
        },
        ensure_ascii=False,
        indent=2,
    )


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------

_LIST_SCHEMA = {
    "name": "list_skills",
    "description": (
        "List external skill packages discovered in OUROBOROS_SKILLS_REPO_PATH. "
        "Returns counts + per-skill metadata (name, type, enabled, review_status, "
        "available_for_execution). Read-only."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}

_REVIEW_SCHEMA = {
    "name": "review_skill",
    "description": (
        "Run tri-model skill review on one external skill package using the "
        "same review infrastructure as repo commits but scored against the "
        "Skill Review Checklist section in docs/CHECKLISTS.md. Persists the "
        "verdict to data/state/skills/<name>/review.json with a content "
        "hash so a later edit invalidates the review automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (directory name in OUROBOROS_SKILLS_REPO_PATH).",
            },
        },
        "required": ["skill"],
    },
}

_EXEC_SCHEMA = {
    "name": "skill_exec",
    "description": (
        "Execute a script from an external skill package. The skill must be "
        "enabled and carry a fresh PASS review verdict. Only type=script "
        "skills execute in Phase 3 — type=instruction skills are "
        "catalogued + reviewable but have no executable payload by "
        "design; type=extension execution lands in Phase 4 (returns "
        "SKILL_EXEC_DEFERRED). The ``script`` argument must match a "
        "``name`` entry in the manifest's ``scripts:`` array (SKILL.md "
        "body and assets/* are reviewed content but not executable). "
        "Runtime allowlist: python/python3/bash/node. The subprocess "
        "runs with cwd=skill_dir, a scrubbed env (env_from_settings "
        "keys only), panic-kill tracking, and a timeout from the "
        "manifest (capped at 300s). Blocked entirely when "
        "OUROBOROS_RUNTIME_MODE=light."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (directory name in OUROBOROS_SKILLS_REPO_PATH).",
            },
            "script": {
                "type": "string",
                "description": (
                    "Relative path of the script inside the skill directory "
                    "(e.g. 'scripts/fetch.py'). Absolute paths and '..' "
                    "traversal are rejected."
                ),
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional argv for the script.",
            },
        },
        "required": ["skill", "script"],
    },
}

_TOGGLE_SCHEMA = {
    "name": "toggle_skill",
    "description": (
        "Enable or disable a skill. Disabled skills are excluded from "
        "skill_exec regardless of review status. Enabling a skill with a "
        "non-PASS review is allowed but does not make it executable."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name.",
            },
            "enabled": {
                "type": "boolean",
                "description": "True to enable, False to disable.",
            },
        },
        "required": ["skill", "enabled"],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="list_skills",
            schema=_LIST_SCHEMA,
            handler=_handle_list_skills,
            is_code_tool=False,
            timeout_sec=30,
        ),
        ToolEntry(
            name="review_skill",
            schema=_REVIEW_SCHEMA,
            handler=_handle_review_skill,
            is_code_tool=False,
            timeout_sec=_HARD_TIMEOUT_CEILING_SEC,
        ),
        ToolEntry(
            name="skill_exec",
            schema=_EXEC_SCHEMA,
            handler=_handle_skill_exec,
            is_code_tool=False,
            timeout_sec=_HARD_TIMEOUT_CEILING_SEC,
        ),
        ToolEntry(
            name="toggle_skill",
            schema=_TOGGLE_SCHEMA,
            handler=_handle_toggle_skill,
            is_code_tool=False,
            timeout_sec=15,
        ),
    ]


__all__ = [
    "get_tools",
    "_ALLOWED_RUNTIMES",
    "_HARD_TIMEOUT_CEILING_SEC",
]
