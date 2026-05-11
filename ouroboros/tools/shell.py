"""Shell tools: run_shell, claude_code_edit."""

from __future__ import annotations

import ast
import json
import logging
import os
import pathlib
import re
import shlex
import signal
import subprocess
import sys
import threading
from subprocess import Popen, CompletedProcess
from typing import Any, Dict, List

from ouroboros.platform_layer import IS_WINDOWS, kill_process_tree, subprocess_new_group_kwargs
from ouroboros.config import load_settings
from ouroboros.tools.commit_gate import _invalidate_advisory
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, run_cmd
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.contracts.skill_payload_policy import (
    SkillPayloadPathError,
    cross_skill_redirect_error,
    resolve_skill_payload_target,
    synthesize_payload_constraint,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subprocess process-group registry (for panic kill)
# ---------------------------------------------------------------------------
_active_subprocesses: set = set()
_subprocess_lock = threading.Lock()

_RUN_SHELL_DEFAULT_TIMEOUT_SEC = 360


def _tracked_subprocess_run(cmd, **kwargs):
    """subprocess.run() replacement with process group tracking.

    Each subprocess gets its own session (start_new_session=True) so the
    entire process tree can be killed via os.killpg() on panic.
    """
    timeout = kwargs.pop("timeout", None)
    kwargs.update(subprocess_new_group_kwargs())
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    proc = Popen(cmd, **kwargs)
    with _subprocess_lock:
        _active_subprocesses.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return CompletedProcess(proc.args, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        _kill_process_group(proc)
        proc.wait(timeout=5)
        raise
    finally:
        with _subprocess_lock:
            _active_subprocesses.discard(proc)


def _kill_process_group(proc):
    """Kill a subprocess and its entire process tree."""
    kill_process_tree(proc)


def kill_all_tracked_subprocesses():
    """Kill all tracked subprocess trees. Called on panic."""
    with _subprocess_lock:
        procs = list(_active_subprocesses)
    for proc in procs:
        _kill_process_group(proc)
    with _subprocess_lock:
        _active_subprocesses.clear()


def _resolve_effective_timeout(default_timeout_sec: int) -> int:
    """Resolve effective timeout from settings.json with env fallback."""
    try:
        settings_val = int(load_settings().get("OUROBOROS_TOOL_TIMEOUT_SEC") or 0)
        if settings_val > 0:
            return settings_val
    except Exception:
        pass
    raw = str(os.environ.get("OUROBOROS_TOOL_TIMEOUT_SEC", "") or "").strip()
    if raw:
        try:
            parsed = int(raw)
            if parsed > 0:
                return parsed
        except ValueError:
            pass
    return max(int(default_timeout_sec), 1)


def _describe_returncode(returncode: int) -> str:
    """Render a return code with signal details when applicable."""
    if int(returncode) < 0:
        signal_num = abs(int(returncode))
        try:
            signal_name = signal.Signals(signal_num).name
        except ValueError:
            signal_name = f"SIG{signal_num}"
        return f"exit_code={returncode} (signal={signal_name})"
    return f"exit_code={returncode}"


def _format_process_output(stdout: str, stderr: str, *, limit: int = 50_000) -> str:
    """Render stdout/stderr sections with truncation."""
    stdout_text = str(stdout or "").strip()
    stderr_text = str(stderr or "").strip()
    parts: List[str] = []
    if stdout_text:
        parts.append(f"STDOUT:\n{stdout_text}")
    if stderr_text:
        parts.append(f"STDERR:\n{stderr_text}")
    rendered = "\n\n".join(parts) if parts else "STDOUT:\n(empty)"
    if len(rendered) > limit:
        rendered = rendered[: limit // 2] + "\n...(truncated)...\n" + rendered[-limit // 2 :]
    return rendered


def _format_process_failure(prefix: str, action: str, res: CompletedProcess) -> str:
    """Render a subprocess failure with output context."""
    return (
        f"{prefix}: {action} with {_describe_returncode(res.returncode)}.\n\n"
        f"{_format_process_output(res.stdout or '', res.stderr or '')}"
    )


def _resolve_git_root(path: pathlib.Path) -> pathlib.Path | None:
    try:
        from ouroboros.review_state import discover_repo_root
        root = discover_repo_root(path)
        return root if (root / ".git").exists() else None
    except Exception:
        return None


def _status_snapshot(repo_dir: pathlib.Path | None) -> list[str]:
    if repo_dir is None:
        return []
    return sorted(_get_changed_files(repo_dir))


# ---------------------------------------------------------------------------
# Shell builtins / operators that cannot run via subprocess
# ---------------------------------------------------------------------------
_SHELL_BUILTINS = frozenset([
    "cd", "source", ".", "export", "alias", "eval",
    "set", "unset", "pushd", "popd", "read", "ulimit",
])

_SHELL_OPERATORS = frozenset(["&&", "||", "|", ";", ">", ">>", "<", "<<"])
_SHELL_INTERPRETERS = frozenset({
    "sh", "bash", "zsh", "fish",
    "cmd", "cmd.exe",
    "powershell", "powershell.exe",
    "pwsh", "pwsh.exe",
})
_ENV_REF_PATTERN = re.compile(r'\$(?:\{[A-Z][A-Z0-9_]*\}|[A-Z][A-Z0-9_]*)')

# 2026-05-04: detect one bash/GNU grep alternation idiom that doesn't work
# portably in argv mode. In bash, ``grep "A\|B"`` works on GNU grep because
# basic-regex with `\|` as alternation is a GNU extension (and shell doesn't
# expand the backslash inside double quotes). BSD grep on macOS treats ``\|``
# as the literal two-character sequence, so the pattern matches nothing. Smaller
# models that learned ``grep "A\|B"`` from bash scripts hit this when their
# tool calls go to subprocess directly. Auto-correct to the portable
# extended-regex argv form (``-E "A|B"``) and annotate the output.
_GREP_TOOLS = frozenset(("grep", "egrep", "fgrep"))
_GREP_REGEX_MODE_FLAGS = frozenset((
    "-E", "--extended-regexp",
    "-P", "--perl-regexp",
    "-F", "--fixed-strings",
    "-G", "--basic-regexp",
))
_GREP_BACKSLASH_PIPE_PATTERN = re.compile(r'\\\|')
_NO_MATCH_EXIT_TOOLS = frozenset(("grep", "egrep", "fgrep", "rg", "ag", "ack"))


def _is_search_no_match(res: CompletedProcess) -> bool:
    tool = pathlib.Path(str(res.args[0] if res.args else "")).name.lower()
    return (
        int(res.returncode) == 1
        and tool in _NO_MATCH_EXIT_TOOLS
        and not str(res.stderr or "").strip()
    )


def _grep_has_explicit_regex_mode(cmd: List[str]) -> bool:
    """Return True when grep argv explicitly chooses regex/string flavor."""
    if not cmd:
        return False
    tool = pathlib.Path(cmd[0]).name.lower()
    if tool in ("egrep", "fgrep"):
        return True
    for arg in cmd[1:]:
        if not isinstance(arg, str):
            continue
        if arg in _GREP_REGEX_MODE_FLAGS:
            return True
        if arg.startswith("--"):
            continue
        # Short options may be clustered, e.g. `grep -rnE pattern path`.
        if arg.startswith("-") and any(flag in arg[1:] for flag in ("E", "P", "F", "G")):
            return True
    return False


def _maybe_autocorrect_grep_backslash_pipe(cmd: List[str]) -> tuple[List[str], str]:
    if not cmd or pathlib.Path(cmd[0]).name.lower() not in _GREP_TOOLS:
        return cmd, ""
    if _grep_has_explicit_regex_mode(cmd):
        return cmd, ""
    corrected = list(cmd)
    changed_args: list[str] = []
    for idx, arg in enumerate(corrected[1:], start=1):
        if isinstance(arg, str) and _GREP_BACKSLASH_PIPE_PATTERN.search(arg):
            corrected[idx] = _GREP_BACKSLASH_PIPE_PATTERN.sub("|", arg)
            changed_args.append(arg)
    if not changed_args:
        return cmd, ""
    corrected.insert(1, "-E")
    return corrected, (
        "⚠️ SHELL_REGEX_AUTO_CORRECTED: converted grep backslash-escaped "
        "alternation (\\|) to extended regex mode (`grep -E`) and rewrote "
        f"{changed_args!r} to use `|`.\n"
    )


# ---------------------------------------------------------------------------
# run_shell
# ---------------------------------------------------------------------------
def _run_shell(ctx: ToolContext, cmd, cwd: str = "") -> str:
    if isinstance(cmd, str):
        # Cascade recovery: try to parse the string into a list before failing.
        # LLMs frequently pass cmd as a string instead of a JSON array.
        recovered = None
        # 1. JSON array: '["grep", "-r", "pattern"]'
        try:
            parsed = json.loads(cmd)
            if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                recovered = parsed
        except (json.JSONDecodeError, ValueError):
            pass
        # 2. Python literal: "['grep', '-r']"
        if recovered is None:
            try:
                parsed = ast.literal_eval(cmd)
                if isinstance(parsed, list) and all(isinstance(x, str) for x in parsed):
                    recovered = parsed
            except (ValueError, SyntaxError):
                pass
        # 3. Shell-style string: "grep -rn pattern ."
        #
        # If the input STARTED with `[` or `{` and both structured-parse
        # layers failed, it's a malformed JSON/Python literal — NOT a
        # shell command. Refuse here rather than letting shlex.split strip
        # the brackets and produce garbage argv that subprocess will fail
        # to exec with a useless ENOENT (e.g. ``'[git,'``). 2026-05-03
        # production bug.
        if recovered is None:
            stripped = cmd.lstrip()
            is_posix_test_cmd = stripped.startswith("[ ") and stripped.rstrip().endswith(" ]")
            if stripped[:1] in ("[", "{") and not is_posix_test_cmd:
                return (
                    '⚠️ SHELL_ARG_ERROR: `cmd` looks like a JSON/Python list literal '
                    'but failed to parse cleanly (likely an escape or quote-mismatch '
                    'issue). Pass cmd as an actual array, not a stringified array.\n\n'
                    'Correct usage:\n'
                    '  run_shell(cmd=["git", "log", "--oneline", "-10"])\n\n'
                    'Wrong usage (the failure that brought you here):\n'
                    '  run_shell(cmd=\'["git", "log", "--oneline", "-10"]\')\n\n'
                    'For reading files, prefer `repo_read` / `data_read`.\n'
                    'For searching code, prefer `code_search`.'
                )
            try:
                parts = shlex.split(cmd)
                if parts:
                    recovered = parts
            except ValueError:
                pass
        if recovered is not None:
            cmd = recovered
        else:
            return (
                '⚠️ SHELL_ARG_ERROR: `cmd` must be a JSON array of strings, not a plain string.\n\n'
                'Correct usage:\n'
                '  run_shell(cmd=["grep", "-r", "pattern", "path/"])\n'
                '  run_shell(cmd=["python", "-c", "print(1+1)"])\n\n'
                'Wrong usage:\n'
                '  run_shell(cmd="grep -r pattern path/")\n\n'
                'For reading files, prefer `repo_read` / `data_read`.\n'
                'For searching code, prefer `code_search`.'
            )

    if not isinstance(cmd, list):
        return "⚠️ SHELL_ARG_ERROR: cmd must be a list of strings."
    cmd = [str(x) for x in cmd]

    executable_name = pathlib.Path(cmd[0]).name.lower() if cmd else ""
    if executable_name not in _SHELL_INTERPRETERS:
        for arg in cmd:
            match = _ENV_REF_PATTERN.search(arg)
            if match:
                return (
                    f'⚠️ SHELL_ENV_ERROR: Found literal env reference "{match.group(0)}" in cmd array. '
                    "run_shell executes argv directly, so shell variables are not expanded. "
                    'Use ["sh", "-c", "..."] if you intentionally need shell expansion, '
                    "or read the environment variable inside the called program."
                )

    # Reject shell builtins (they are not executables)
    if cmd and cmd[0] in _SHELL_BUILTINS:
        if cmd[0] == "cd":
            return (
                '⚠️ SHELL_CMD_ERROR: "cd" is a shell builtin, not an executable. '
                'Use the "cwd" parameter instead: '
                'run_shell(cmd=["git", "log"], cwd="/target/dir")'
            )
        return (
            f'⚠️ SHELL_CMD_ERROR: "{cmd[0]}" is a shell builtin and cannot '
            'be executed directly via subprocess. '
            'Use ["sh", "-c", "your command"] if you need shell builtins.'
        )

    cmd, autocorrect_note = _maybe_autocorrect_grep_backslash_pipe(cmd)

    # Reject shell operators in cmd array (subprocess doesn't interpret them)
    found_ops = _SHELL_OPERATORS.intersection(cmd)
    if found_ops:
        op = sorted(found_ops)[0]
        return (
            f'⚠️ SHELL_CMD_ERROR: Shell operator "{op}" found in cmd array. '
            'Subprocess does not interpret shell syntax. '
            'Options: (1) Split into separate run_shell calls. '
            '(2) For pipes/chaining: ["sh", "-c", "cmd1 && cmd2"]'
        )

    work_dir = ctx.repo_dir
    if cwd and cwd.strip() not in ("", ".", "./"):
        candidate = (ctx.repo_dir / cwd).resolve()
        if candidate.exists() and candidate.is_dir():
            work_dir = candidate
    repo_root = _resolve_git_root(pathlib.Path(work_dir))
    before_changed = _status_snapshot(repo_root)

    timeout_sec = _resolve_effective_timeout(_RUN_SHELL_DEFAULT_TIMEOUT_SEC)
    try:
        res = _tracked_subprocess_run(
            cmd, cwd=str(work_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=timeout_sec,
        )
        if res.returncode != 0:
            if _is_search_no_match(res):
                return autocorrect_note + (
                    f"{_describe_returncode(res.returncode)} (no matches)\n"
                    f"{_format_process_output(res.stdout or '', '')}"
                )
            return autocorrect_note + _format_process_failure(
                "⚠️ SHELL_EXIT_ERROR",
                "command exited",
                res,
            )
        after_changed = _status_snapshot(repo_root)
        if after_changed != before_changed:
            _invalidate_advisory(
                ctx,
                changed_paths=after_changed or before_changed,
                mutation_root=repo_root,
                source_tool="run_shell",
            )
        return autocorrect_note + f"exit_code=0\n{_format_process_output(res.stdout or '', res.stderr or '')}"
    except subprocess.TimeoutExpired:
        return (
            f"⚠️ TOOL_TIMEOUT (run_shell): command exceeded {timeout_sec}s. "
            "Subprocess tree was terminated."
        )
    except Exception as e:
        return f"⚠️ SHELL_ERROR: {e}"


# ---------------------------------------------------------------------------
# Orchestration helpers (live in tool layer, not in gateway)
# ---------------------------------------------------------------------------

def _load_project_context(repo_dir: pathlib.Path) -> str:
    """Load project docs for Claude Code system_prompt injection."""
    docs = [
        ("BIBLE.md", "CONSTITUTION"),
        ("docs/DEVELOPMENT.md", "DEVELOPMENT GUIDE"),
        ("docs/CHECKLISTS.md", "REVIEW CHECKLISTS"),
        ("docs/ARCHITECTURE.md", "ARCHITECTURE"),
    ]
    parts: list = []
    for relpath, label in docs:
        fpath = repo_dir / relpath
        if fpath.is_file():
            try:
                content = fpath.read_text(encoding="utf-8")
                if len(content) > 50_000:
                    content = content[:50_000] + "\n\n[... truncated for context size ...]"
                parts.append(f"## {label}\n\n{content}")
            except Exception:
                pass
    return "\n\n---\n\n".join(parts)


def _get_changed_files(repo_dir: pathlib.Path) -> list:
    """Return list of changed files after an edit."""
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return [line[3:].strip() for line in res.stdout.strip().splitlines() if len(line) > 3]
    except Exception:
        pass
    return []


def _get_diff_stat(repo_dir: pathlib.Path) -> str:
    """Return git diff --stat output."""
    try:
        res = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0:
            return res.stdout.strip()
    except Exception:
        pass
    return ""


def _run_validation(repo_dir: pathlib.Path) -> str:
    """Run basic validation after edit (tests). Returns summary."""
    agent_python = sys.executable or os.environ.get("OUROBOROS_AGENT_PYTHON") or "python3"
    try:
        res = subprocess.run(
            [agent_python, "-m", "pytest", "tests/", "--tb=line", "-q"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=60,
        )
        if res.returncode == 0:
            return "PASS: all tests passed"
        output = (res.stdout or "")[-500:]
        return f"FAIL: tests failed (exit {res.returncode})\n{output}"
    except subprocess.TimeoutExpired:
        return "TIMEOUT: validation exceeded 60s"
    except Exception as e:
        return f"ERROR: validation failed: {e}"


# ---------------------------------------------------------------------------
# claude_code_edit — SDK-only path
# ---------------------------------------------------------------------------

def _claude_code_edit(ctx: ToolContext, prompt: str, cwd: str = "",
                      budget: float = 5.0, validate: bool = False,
                      bucket: str = "", skill_name: str = "") -> str:
    """Delegate code edits via the Claude Agent SDK gateway.

    Uses the claude-agent-sdk Python package with PreToolUse safety hooks
    that block writes outside cwd and to protected runtime paths.
    """
    from ouroboros.tools.git import _acquire_git_lock, _release_git_lock

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "⚠️ CLAUDE_CODE_UNAVAILABLE: ANTHROPIC_API_KEY not set."

    work_dir = str(ctx.repo_dir)
    skill_payload_root = None
    synth = synthesize_payload_constraint(bucket, skill_name)
    if (bucket or skill_name) and synth is None:
        return (
            "⚠️ CLAUDE_CODE_ERROR: bucket and skill_name must be supplied together; "
            "bucket must be one of external/clawhub/ouroboroshub (native excluded); "
            "skill_name must sanitize to a non-empty slug."
        )
    existing_tc = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    redirect_err = cross_skill_redirect_error(existing_tc, synth)
    if redirect_err:
        return f"⚠️ SKILL_REDIRECT_BLOCKED: {redirect_err}"
    # Real skill_repair task_constraint wins over a synthesized one — repair
    # confinement is sticky.
    if existing_tc and existing_tc.mode == "skill_repair":
        task_constraint = existing_tc
    else:
        task_constraint = synth or existing_tc
    if task_constraint and task_constraint.mode == "skill_repair" and task_constraint.payload_root:
        try:
            resolved_skill_target = resolve_skill_payload_target(
                pathlib.Path(ctx.drive_root),
                cwd or ".",
                constraint=task_constraint,
                allow_short_relative=True,
            )
            work_dir = str(resolved_skill_target.target_path)
            skill_payload_root = resolved_skill_target.payload_root
        except (SkillPayloadPathError, ValueError) as e:
            return f"⚠️ CLAUDE_CODE_ERROR: {e}"
    elif cwd and cwd.strip() not in ("", ".", "./"):
        raw_cwd = cwd.strip()
        try:
            resolved_skill_target = resolve_skill_payload_target(pathlib.Path(ctx.drive_root), raw_cwd)
            candidate = resolved_skill_target.target_path
            skill_payload_root = resolved_skill_target.payload_root
        except SkillPayloadPathError as exc:
            normalized_cwd = raw_cwd.replace("\\", "/").strip().lstrip("/")
            if normalized_cwd.startswith("data/skills/") or normalized_cwd.startswith("skills/"):
                return f"⚠️ CLAUDE_CODE_ERROR: skill cwd is invalid: {exc}"
            try:
                pathlib.Path(raw_cwd).resolve(strict=False).relative_to(pathlib.Path(ctx.drive_root).resolve(strict=False))
            except ValueError:
                pass
            else:
                return f"⚠️ CLAUDE_CODE_ERROR: skill cwd is invalid: {exc}"
            candidate = (ctx.repo_dir / raw_cwd).resolve()
        if not candidate.exists() or not candidate.is_dir():
            return f"⚠️ CLAUDE_CODE_ERROR: cwd not found or not a directory: {cwd}"
        work_dir = str(candidate)
    work_dir_path = pathlib.Path(work_dir).resolve()
    repair_sidecar_snapshots = {}
    sidecar_root = pathlib.Path(skill_payload_root).resolve() if skill_payload_root is not None else None
    if sidecar_root is not None:
        for sidecar_name in (".clawhub.json", ".ouroboroshub.json", ".self_authored.json", ".seed-origin", "SKILL.openclaw.md"):
            sidecar_path = sidecar_root / sidecar_name
            try:
                repair_sidecar_snapshots[sidecar_path] = sidecar_path.read_bytes() if sidecar_path.exists() else None
            except OSError:
                repair_sidecar_snapshots[sidecar_path] = None
    target_repo_root = _resolve_git_root(work_dir_path)
    repo_mode = target_repo_root is not None
    if target_repo_root is None:
        target_repo_root = work_dir_path
    before_changed = _status_snapshot(target_repo_root)

    from ouroboros.gateways.claude_code import resolve_claude_code_model
    model = resolve_claude_code_model()

    lock = _acquire_git_lock(ctx) if repo_mode else None
    try:
        if repo_mode:
            try:
                run_cmd(["git", "checkout", ctx.branch_dev], cwd=ctx.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout): {e}"

        ctx.emit_progress_fn("Delegating to Claude Agent SDK...")

        try:
            from ouroboros.gateways.claude_code import (
                DEFAULT_CLAUDE_CODE_MAX_TURNS,
                run_edit,
            )

            system_prompt = (
                f"STRICT: Only modify files inside {work_dir}. "
                f"Git branch: {ctx.branch_dev}. Do NOT commit or push.\n\n"
                + _load_project_context(pathlib.Path(ctx.repo_dir))
            )

            result = run_edit(
                prompt=prompt,
                cwd=work_dir,
                model=model,
                max_turns=DEFAULT_CLAUDE_CODE_MAX_TURNS,
                budget=budget,
                system_prompt=system_prompt,
            )

            result.changed_files = _get_changed_files(target_repo_root)
            result.diff_stat = _get_diff_stat(target_repo_root)

            if validate and result.success:
                result.validation_summary = _run_validation(target_repo_root)

            if result.cost_usd > 0:
                ctx.pending_events.append({
                    "type": "llm_usage",
                    "provider": "claude_agent_sdk",
                    "model": model,
                    "api_key_type": "anthropic",
                    "model_category": "claude_code",
                    "usage": result.usage or {"cost": result.cost_usd},
                    "cost": result.cost_usd,
                    "source": "claude_code_edit",
                    "ts": utc_now_iso(),
                    "category": "task",
                })

            if not result.success:
                return f"⚠️ CLAUDE_CODE_ERROR: {result.error}\n\n{result.result_text}"

            restored_sidecars = []
            for sidecar_path, before_bytes in repair_sidecar_snapshots.items():
                try:
                    after_exists = sidecar_path.exists()
                    after_bytes = sidecar_path.read_bytes() if after_exists else None
                    if after_bytes != before_bytes:
                        if before_bytes is None:
                            sidecar_path.unlink(missing_ok=True)
                        else:
                            sidecar_path.write_bytes(before_bytes)
                        restored_sidecars.append(sidecar_path.name)
                except OSError:
                    restored_sidecars.append(sidecar_path.name)
            if restored_sidecars:
                return (
                    "⚠️ SKILL_PAYLOAD_CONTROL_BLOCKED: claude_code_edit attempted to modify "
                    "skill provenance/control-plane sidecars: "
                    + ", ".join(sorted(set(restored_sidecars)))
                    + ". The sidecar changes were reverted; edit payload code files instead."
                )

            after_changed = _status_snapshot(target_repo_root)
            if repo_mode and after_changed != before_changed:
                _invalidate_advisory(
                    ctx,
                    changed_paths=result.changed_files or after_changed or before_changed,
                    mutation_root=target_repo_root,
                    source_tool="claude_code_edit",
                )

            return result.to_tool_output()

        except ImportError:
            return (
                "⚠️ CLAUDE_CODE_UNAVAILABLE: claude-agent-sdk not installed. "
                "Install: pip install 'ouroboros[claude-sdk]'"
            )
        except Exception as e:
            import sys
            sdk_version = "(unknown)"
            try:
                import importlib.metadata
                sdk_version = importlib.metadata.version("claude-agent-sdk")
            except Exception:
                pass
            return (
                f"⚠️ CLAUDE_CODE_FAILED: {type(e).__name__}: {e}\n"
                f"Diagnostic: sdk_version={sdk_version}, python={sys.executable}"
            )

    finally:
        if lock is not None:
            _release_git_lock(lock)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("run_shell", {
            "name": "run_shell",
            "description": (
                "Run a command inside the repo. Returns stdout+stderr. "
                "cmd MUST be an array of strings, never a single shell-style "
                "string. Use cwd= for working directory; cd is rejected. "
                "For pipes/chaining use [\"sh\", \"-c\", \"cmd1 && cmd2\"]."
            ),
            "parameters": {"type": "object", "properties": {
                "cmd": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Argv as a JSON array of strings. Example: "
                        "[\"git\", \"log\", \"--oneline\", \"-10\"]. NEVER "
                        "pass a single string like \"git log\" or a "
                        "stringified array like '[\"git\", \"log\"]'."
                    ),
                },
                "cwd": {
                    "type": "string", "default": "",
                    "description": (
                        "Working directory relative to the repo root. Use "
                        "this instead of `cd` (which is a shell builtin "
                        "and is rejected)."
                    ),
                },
            }, "required": ["cmd"]},
        }, _run_shell, is_code_tool=True, timeout_sec=_RUN_SHELL_DEFAULT_TIMEOUT_SEC),
        ToolEntry("claude_code_edit", {
            "name": "claude_code_edit",
            "description": (
                "Delegate code edits to Claude Code (via Agent SDK with safety guards). "
                "Prefer this for anything beyond one exact replacement: large single-file "
                "edits, repeated coordinated edits, multi-hunk work, multi-file changes, "
                "renames/signature changes, or uncertain scope. Prefer it over chaining "
                "many str_replace_editor calls. It also subdivides very large writes across "
                "many small Write/Edit operations inside its own agent loop, so use it for "
                "files larger than a single LLM output can produce. Follow with repo_commit. "
                "Optional bucket+skill_name args let runtime_mode=light tasks anchor cwd "
                "under data/skills/<bucket>/<skill_name>/ without an explicit task_constraint."
            ),
            "parameters": {"type": "object", "properties": {
                "prompt": {"type": "string"},
                "cwd": {"type": "string", "default": ""},
                "budget": {"type": "number",
                           "description": "Max USD for this Claude Code call. Default: 5.0"},
                "validate": {"type": "boolean", "default": False,
                             "description": "Run post-edit validation (tests). Returns summary in result."},
                "bucket": {
                    "type": "string",
                    "enum": ["external", "clawhub", "ouroboroshub"],
                    "description": "Skill payload bucket. Pair with skill_name to resolve cwd under data/skills/<bucket>/<skill_name>/. Requires both args together.",
                },
                "skill_name": {
                    "type": "string",
                    "description": "Skill slug (sanitized to alnum/_-., ≤64 chars). Requires bucket.",
                },
            }, "required": ["prompt"]},
        }, _claude_code_edit, is_code_tool=True, timeout_sec=1200),
    ]
