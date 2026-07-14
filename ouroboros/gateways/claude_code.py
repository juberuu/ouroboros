"""Claude Agent SDK transport for edit and read-only advisory paths.

Callers own orchestration and validation. This layer keeps SDK hooks,
ANTHROPIC_API_KEY auth, bundled CLI resolution, stderr capture, and no
CLI fallback when the SDK is missing.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ouroboros.config import get_runtime_mode
from ouroboros.runtime_mode_policy import (
    SAFETY_CRITICAL_PATHS,
    is_protected_runtime_path,
    mode_allows_protected_write,
    protected_write_block_message,
)
from ouroboros.usage_accounting import (
    AttemptRequest,
    UsageAccountingError,
    UsageScope,
    current_usage_scope,
    mark_dispatched,
    mark_unresolved,
    reserve_attempt,
    settle_attempt,
    usage_scope,
)

log = logging.getLogger(__name__)

# Eager import preserves the no-CLI-fallback install hint path.
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    AssistantMessage, ResultMessage,
)

_STDERR_MAX_LINES = 200
_stderr_lock = threading.Lock()
_stderr_buffer: collections.deque[str] = collections.deque(maxlen=_STDERR_MAX_LINES)
DEFAULT_CLAUDE_CODE_MAX_TURNS = 50
_READONLY_CHILD_TIMEOUT_SEC = 900


def _stderr_callback(line: str) -> None:
    """Store raw CLI stderr for failure diagnostics."""
    log.warning("claude-cli stderr: %s", line)
    with _stderr_lock:
        _stderr_buffer.append(line)


def get_last_stderr(max_chars: int = 4000) -> str:
    """Return recent CLI stderr."""
    with _stderr_lock:
        lines = list(_stderr_buffer)
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def clear_stderr_buffer() -> None:
    """Clear captured CLI stderr."""
    with _stderr_lock:
        _stderr_buffer.clear()


def _materialize_system_prompt_file(system_prompt: Optional[str]) -> Optional[pathlib.Path]:
    if not system_prompt:
        return None
    temp_dir = pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-claude-system-"))
    try:
        temp_dir.chmod(0o700)
    except OSError:
        pass
    prompt_path = temp_dir / "system_prompt.md"
    prompt_path.write_text(system_prompt, encoding="utf-8")
    try:
        prompt_path.chmod(0o600)
    except OSError:
        pass
    return prompt_path


def _cleanup_system_prompt_file(prompt_path: Optional[pathlib.Path]) -> None:
    if prompt_path is None:
        return
    try:
        prompt_path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        prompt_path.parent.rmdir()
    except OSError:
        pass


def _claude_options_has_explicit_param(name: str) -> bool:
    import inspect

    try:
        sig = inspect.signature(ClaudeAgentOptions.__init__)
    except (TypeError, ValueError):
        return False
    return name in sig.parameters


def _system_prompt_file_value(prompt_path: pathlib.Path) -> Any:
    sdk_module = sys.modules.get("claude_agent_sdk")
    prompt_file_cls = getattr(sdk_module, "SystemPromptFile", None) if sdk_module else None
    if prompt_file_cls is None:
        return None
    for factory in (
        lambda: prompt_file_cls(path=str(prompt_path)),
        lambda: prompt_file_cls(str(prompt_path)),
    ):
        try:
            return factory()
        except TypeError:
            continue
    return None


def _system_prompt_option_kwargs(
    system_prompt: Optional[str],
    prompt_path: Optional[pathlib.Path],
) -> Dict[str, Any]:
    if not system_prompt:
        return {}
    if prompt_path is not None:
        if _claude_options_has_explicit_param("system_prompt_file"):
            return {"system_prompt_file": str(prompt_path)}
        if _claude_options_has_explicit_param("system_prompt_path"):
            return {"system_prompt_path": str(prompt_path)}
        prompt_file_value = _system_prompt_file_value(prompt_path)
        if prompt_file_value is not None and _claude_options_has_explicit_param("system_prompt"):
            return {"system_prompt": prompt_file_value}
    return {"system_prompt": system_prompt}


SAFETY_CRITICAL = SAFETY_CRITICAL_PATHS


@dataclass
class ClaudeCodeResult:
    """Structured SDK invocation result."""

    success: bool
    result_text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    stderr_tail: str = ""
    # Populated by callers after invocation.
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""
    validation_summary: str = ""

    def to_tool_output(self) -> str:
        """Return structured JSON for tool output."""
        out: Dict[str, Any] = {
            "success": self.success,
            "result": self.result_text,
        }
        if self.session_id:
            out["session_id"] = self.session_id
        if self.cost_usd:
            out["cost_usd"] = round(self.cost_usd, 6)
        if self.usage:
            out["usage"] = self.usage
        if self.changed_files:
            out["changed_files"] = self.changed_files
        if self.diff_stat:
            out["diff_stat"] = self.diff_stat
        if self.error:
            out["error"] = self.error
        if self.stderr_tail:
            out["stderr_tail"] = self.stderr_tail
        if self.validation_summary:
            out["validation"] = self.validation_summary
        return json.dumps(out, ensure_ascii=False, indent=2)


def _coerce_usage_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_sdk_usage(usage: Any) -> Dict[str, Any]:
    """Map Anthropic token usage names to Ouroboros budget/log keys."""
    if not isinstance(usage, dict):
        return {}
    normalized = dict(usage)
    normalized["prompt_tokens"] = _coerce_usage_int(
        usage.get("prompt_tokens", usage.get("input_tokens", 0))
    )
    normalized["completion_tokens"] = _coerce_usage_int(
        usage.get("completion_tokens", usage.get("output_tokens", 0))
    )
    normalized["cached_tokens"] = _coerce_usage_int(
        usage.get("cached_tokens", usage.get("cache_read_input_tokens", 0))
    )
    normalized["cache_write_tokens"] = _coerce_usage_int(
        usage.get("cache_write_tokens", usage.get("cache_creation_input_tokens", 0))
    )
    return normalized


def _reserve_sdk_attempt(
    prompt: str,
    model: str,
    *,
    max_budget_usd: Optional[float],
    source: str,
):
    reservation = reserve_attempt(AttemptRequest(
        model=str(model or "claude-code"),
        provider="anthropic",
        prompt_tokens_estimate=max(0, len(str(prompt or "")) // 4),
        max_budget_usd=max_budget_usd,
        force_unknown_reservation=max_budget_usd is None,
        source=source,
    ))
    mark_dispatched(reservation)
    return reservation


def _settle_sdk_attempt(reservation: Any, result: ClaudeCodeResult, reported_cost: Any) -> None:
    result.usage["ledger_attempt_ids"] = [reservation.attempt_id]
    try:
        cost = float(reported_cost) if reported_cost is not None else None
    except (TypeError, ValueError):
        cost = None
    try:
        settle_attempt(
            reservation,
            result.usage,
            cost_usd=cost,
            cost_final=cost is not None,
        )
    except Exception as exc:
        # Preserve an already-paid SDK result and best-effort close unresolved.
        log.exception("Failed to settle Claude SDK attempt %s", reservation.attempt_id)
        try:
            mark_unresolved(reservation, f"settlement_write_failed:{type(exc).__name__}")
        except Exception:
            log.exception("Failed to mark Claude SDK settlement unresolved")


def make_path_guard(
    cwd: str,
    repo_root: str | None = None,
    *,
    protect_runtime_paths: bool = True,
    write_path_blocker: Callable[[pathlib.Path], str] | None = None,
):
    """Block SDK writes outside cwd or runtime-protected paths."""
    cwd_resolved = pathlib.Path(cwd).resolve()
    repo_root_resolved = pathlib.Path(repo_root).resolve() if repo_root else None

    async def path_guard(input_data: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        if tool_name not in ("Edit", "Write", "MultiEdit"):
            return {}

        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
        if not file_path:
            return {}

        target = pathlib.Path(file_path)
        if not target.is_absolute():
            target = cwd_resolved / target
        target = target.resolve()

        try:
            target.relative_to(cwd_resolved)
        except ValueError:
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"SAFETY: Write blocked — target path '{file_path}' "
                        f"resolves outside the allowed working directory '{cwd}'."
                    ),
                }
            }
        if write_path_blocker is not None:
            block_reason = write_path_blocker(target)
            if block_reason:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "SAFETY: Write blocked — target path "
                            f"'{file_path}' is not allowed for this edit root: {block_reason}."
                        ),
                    }
                }

        # Prefer repo-root relative paths so subdir cwd still hits protection tables.
        rel = target.relative_to(cwd_resolved).as_posix()
        if repo_root_resolved is not None:
            try:
                rel = target.relative_to(repo_root_resolved).as_posix()
            except ValueError:
                pass
        try:
            from ouroboros.config import DATA_DIR
            from ouroboros.tools.core import is_skill_control_plane_path

            if is_skill_control_plane_path(target, pathlib.Path(DATA_DIR).resolve(strict=False)):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "SAFETY: Write blocked — skill provenance, "
                            "launcher seed, marketplace, dependency, and "
                            "self-authored markers are control-plane state."
                        ),
                    }
                }
        except Exception:
            log.debug("Claude Code skill control-plane guard probe failed", exc_info=True)
        try:
            runtime_mode = get_runtime_mode()
        except Exception:
            runtime_mode = "advanced"
        if protect_runtime_paths and is_protected_runtime_path(rel) and not mode_allows_protected_write(runtime_mode):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        protected_write_block_message(
                            path=rel,
                            runtime_mode=runtime_mode,
                            action="delegate-edit",
                        )
                    ),
                }
            }

        return {}

    return path_guard


def make_readonly_guard():
    """Deny all mutating tools in advisory mode."""

    async def readonly_guard(input_data: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "")
        if tool_name in ("Edit", "Write", "MultiEdit", "Bash"):
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"SAFETY: '{tool_name}' is not allowed in read-only advisory mode. "
                        "Only Read, Grep, Glob are permitted."
                    ),
                }
            }
        return {}

    return readonly_guard


async def _run_edit_async(
    prompt: str,
    cwd: str,
    model: str = "opus",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    budget: Optional[float] = None,
    system_prompt: Optional[str] = None,
    repo_root: Optional[str] = None,
    protect_runtime_paths: bool = True,
    write_path_blocker: Callable[[pathlib.Path], str] | None = None,
) -> ClaudeCodeResult:
    """Run edit-mode SDK with safety hooks."""
    path_guard = make_path_guard(
        cwd,
        repo_root=repo_root,
        protect_runtime_paths=protect_runtime_paths,
        write_path_blocker=write_path_blocker,
    )
    clear_stderr_buffer()

    system_prompt_file = _materialize_system_prompt_file(system_prompt)
    options_kwargs: Dict[str, Any] = dict(
        cwd=cwd,
        model=model,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit", "Write", "Grep", "Glob"],
        disallowed_tools=["Bash", "MultiEdit"],
        max_turns=max_turns,
        max_budget_usd=budget,
        stderr=_stderr_callback,
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Edit|Write|MultiEdit", hooks=[path_guard]),
            ],
        },
    )
    options_kwargs.update(_system_prompt_option_kwargs(system_prompt, system_prompt_file))

    result = ClaudeCodeResult(success=True)
    text_parts: List[str] = []
    accounting = None
    accounting_dispatched = False

    try:
        options = ClaudeAgentOptions(**options_kwargs)
        async with ClaudeSDKClient(options=options) as client:
            accounting = _reserve_sdk_attempt(
                prompt, model, max_budget_usd=budget, source="claude_code.edit",
            )
            accounting_dispatched = True
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result.session_id = getattr(message, "session_id", "") or ""
                    reported_cost = getattr(message, "total_cost_usd", None)
                    result.cost_usd = float(reported_cost or 0)
                    usage = getattr(message, "usage", None)
                    result.usage = _normalize_sdk_usage(usage)
                    _settle_sdk_attempt(accounting, result, reported_cost)
                    accounting_dispatched = False
                    subtype = getattr(message, "subtype", "")
                    if subtype and subtype != "success":
                        result.success = False
                        result.error = f"Agent ended with subtype: {subtype}"
                    break
            if accounting_dispatched:
                mark_unresolved(accounting, "Claude SDK stream ended without ResultMessage")
                accounting_dispatched = False
    except UsageAccountingError:
        raise
    except Exception as e:
        if accounting is not None and accounting_dispatched:
            try:
                mark_unresolved(accounting, f"{type(e).__name__}: {e}")
            except Exception:
                log.exception("Failed to mark Claude SDK edit attempt unresolved")
        result.success = False
        result.error = f"{type(e).__name__}: {e}"
    finally:
        _cleanup_system_prompt_file(system_prompt_file)

    if not result.success:
        result.stderr_tail = get_last_stderr()
    result.result_text = "\n".join(text_parts) if text_parts else "(no output)"
    return result


async def _run_readonly_async(
    prompt: str,
    cwd: str,
    model: str = "opus",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    effort: Optional[str] = "high",
    max_budget_usd: Optional[float] = None,
) -> ClaudeCodeResult:
    """Run read-only advisory SDK with the client lifecycle to avoid stream races."""
    clear_stderr_buffer()
    options_kwargs: Dict[str, Any] = dict(
        cwd=cwd,
        model=model,
        permission_mode="default",  # no auto-approve
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Bash", "Edit", "Write", "MultiEdit"],
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        stderr=_stderr_callback,
    )
    if effort is not None:
        # Older SDKs may lack effort; omit it rather than failing advisory.
        import inspect as _inspect
        try:
            _sig = _inspect.signature(ClaudeAgentOptions.__init__)
            if "effort" in _sig.parameters:
                options_kwargs["effort"] = effort
        except (ValueError, TypeError):
            options_kwargs["effort"] = effort

    try:
        options = ClaudeAgentOptions(**options_kwargs)
    except TypeError:
        options_kwargs.pop("effort", None)
        options = ClaudeAgentOptions(**options_kwargs)

    result = ClaudeCodeResult(success=True)
    text_parts: List[str] = []
    accounting = None
    accounting_dispatched = False

    try:
        async with ClaudeSDKClient(options=options) as client:
            accounting = _reserve_sdk_attempt(
                prompt,
                model,
                max_budget_usd=max_budget_usd,
                source="claude_code.readonly",
            )
            accounting_dispatched = True
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result.session_id = getattr(message, "session_id", "") or ""
                    reported_cost = getattr(message, "total_cost_usd", None)
                    result.cost_usd = float(reported_cost or 0)
                    usage = getattr(message, "usage", None)
                    result.usage = _normalize_sdk_usage(usage)
                    _settle_sdk_attempt(accounting, result, reported_cost)
                    accounting_dispatched = False
                    subtype = getattr(message, "subtype", "")
                    if subtype and subtype != "success":
                        result.success = False
                        result.error = f"Agent ended with subtype: {subtype}"
                    break
            if accounting_dispatched:
                mark_unresolved(accounting, "Claude SDK stream ended without ResultMessage")
                accounting_dispatched = False
    except UsageAccountingError:
        raise
    except Exception as e:
        if accounting is not None and accounting_dispatched:
            try:
                mark_unresolved(accounting, f"{type(e).__name__}: {e}")
            except Exception:
                log.exception("Failed to mark Claude SDK readonly attempt unresolved")
        result.success = False
        result.error = f"{type(e).__name__}: {e}"

    if not result.success:
        result.stderr_tail = get_last_stderr()
    result.result_text = "\n".join(text_parts) if text_parts else "(no output)"
    return result


def _run_async(coro):
    """Run async SDK code from synchronous tool handlers."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result()


def _result_from_dict(data: Dict[str, Any]) -> ClaudeCodeResult:
    """Rehydrate a child-process result without trusting the JSON shape."""
    result = ClaudeCodeResult(success=bool(data.get("success")))
    result.result_text = str(data.get("result_text") or data.get("result") or "")
    result.session_id = str(data.get("session_id") or "")
    result.cost_usd = float(data.get("cost_usd") or 0.0)
    usage = data.get("usage")
    result.usage = dict(usage) if isinstance(usage, dict) else {}
    result.error = str(data.get("error") or "")
    result.stderr_tail = str(data.get("stderr_tail") or "")
    result.changed_files = list(data.get("changed_files") or [])
    result.diff_stat = str(data.get("diff_stat") or "")
    result.validation_summary = str(data.get("validation_summary") or "")
    return result


def _run_readonly_out_of_process(
    prompt: str,
    cwd: str,
    model: str,
    max_turns: int,
    effort: Optional[str],
    max_budget_usd: Optional[float] = None,
) -> ClaudeCodeResult:
    """Run advisory SDK in a child process so native aborts cannot kill workers."""
    payload = {
        "prompt": prompt,
        "cwd": cwd,
        "model": model,
        "max_turns": max_turns,
        "effort": effort,
        "max_budget_usd": max_budget_usd,
    }
    active_scope = current_usage_scope()
    if active_scope is not None:
        scope_payload = dict(vars(active_scope))
        if scope_payload.get("drive_root") is not None:
            scope_payload["drive_root"] = str(scope_payload["drive_root"])
        payload["usage_scope"] = scope_payload
    env = dict(os.environ)
    env["OUROBOROS_CLAUDE_READONLY_CHILD"] = "1"
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    pythonpath = env.get("PYTHONPATH", "")
    if str(repo_root) not in pythonpath.split(os.pathsep):
        env["PYTHONPATH"] = str(repo_root) + (os.pathsep + pythonpath if pythonpath else "")
    try:
        from ouroboros.platform_layer import subprocess_new_group_kwargs

        group_kwargs = subprocess_new_group_kwargs()
    except Exception:
        group_kwargs = {}
    cmd = [sys.executable, "-m", "ouroboros.gateways.claude_code", "--readonly-child"]
    try:
        from ouroboros.platform_layer import kill_process_tree

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            text=True,
            **group_kwargs,
        )
        try:
            stdout, stderr = proc.communicate(
                input=json.dumps(payload, ensure_ascii=False),
                timeout=_READONLY_CHILD_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            kill_process_tree(proc)
            stdout, stderr = proc.communicate(timeout=10)
            return ClaudeCodeResult(
                success=False,
                result_text="(no output)",
                error=f"Claude readonly child timed out after {_READONLY_CHILD_TIMEOUT_SEC}s",
                stderr_tail=((stdout or "") + (stderr or ""))[-4000:],
            )
    except subprocess.TimeoutExpired as exc:
        return ClaudeCodeResult(
            success=False,
            result_text="(no output)",
            error=f"Claude readonly child timed out after {_READONLY_CHILD_TIMEOUT_SEC}s",
            stderr_tail=((exc.stdout or "") + (exc.stderr or ""))[-4000:],
        )
    except Exception as exc:
        return ClaudeCodeResult(
            success=False,
            result_text="(no output)",
            error=f"Claude readonly child failed to start: {type(exc).__name__}: {exc}",
        )

    stdout = (stdout or "").strip()
    stderr = (stderr or "").strip()
    if proc.returncode == 0 and stdout:
        try:
            return _result_from_dict(json.loads(stdout.splitlines()[-1]))
        except Exception as exc:
            return ClaudeCodeResult(
                success=False,
                result_text="(no output)",
                error=f"Claude readonly child returned invalid JSON: {type(exc).__name__}: {exc}",
                stderr_tail=stderr[-4000:],
            )

    sig = ""
    if int(proc.returncode or 0) < 0:
        try:
            sig = signal.Signals(-int(proc.returncode)).name
        except (ValueError, TypeError):
            sig = {6: "SIGABRT"}.get(-int(proc.returncode), f"signal {-int(proc.returncode)}")
    error = f"Claude readonly child exited with code {proc.returncode}"
    if sig:
        error = f"Claude readonly child terminated by {sig} (code {proc.returncode})"
    return ClaudeCodeResult(
        success=False,
        result_text=stdout or "(no output)",
        error=error,
        stderr_tail=stderr[-4000:],
    )


def run_edit(
    prompt: str,
    cwd: str,
    model: str = "opus[1m]",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    budget: Optional[float] = None,
    system_prompt: Optional[str] = None,
    repo_root: Optional[str] = None,
    protect_runtime_paths: bool = True,
    write_path_blocker: Callable[[pathlib.Path], str] | None = None,
) -> ClaudeCodeResult:
    """Synchronous edit-mode SDK entry point."""
    return _run_async(_run_edit_async(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        budget=budget,
        system_prompt=system_prompt,
        repo_root=repo_root,
        protect_runtime_paths=protect_runtime_paths,
        write_path_blocker=write_path_blocker,
    ))


def resolve_claude_code_model(default: str = "opus[1m]") -> str:
    """Return the env/settings Claude Code model, aligned with config defaults."""
    return os.environ.get("CLAUDE_CODE_MODEL", default).strip() or default


def run_readonly(
    prompt: str,
    cwd: str,
    model: str = "opus[1m]",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    effort: Optional[str] = "high",
    max_budget_usd: Optional[float] = None,
) -> ClaudeCodeResult:
    """Synchronous read-only advisory entry point."""
    if os.environ.get("OUROBOROS_CLAUDE_READONLY_CHILD") == "1":
        return _run_async(_run_readonly_async(
            prompt=prompt,
            cwd=cwd,
            model=model,
            max_turns=max_turns,
            effort=effort,
            max_budget_usd=max_budget_usd,
        ))
    return _run_readonly_out_of_process(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
        max_budget_usd=max_budget_usd,
    )


def _main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--readonly-child":
        try:
            from ouroboros.process_custody import start_parent_lifeline

            start_parent_lifeline(label="claude-readonly-child")
        except Exception:
            pass
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except Exception as exc:
            print(json.dumps({
                "success": False,
                "result_text": "(no output)",
                "error": f"invalid child payload: {type(exc).__name__}: {exc}",
            }, ensure_ascii=False), flush=True)
            return 2
        data = payload if isinstance(payload, dict) else {}
        raw_scope = data.get("usage_scope")
        try:
            restored_scope = UsageScope(**raw_scope) if isinstance(raw_scope, dict) else None
            max_budget_usd = (
                float(data["max_budget_usd"])
                if data.get("max_budget_usd") is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            print(json.dumps({
                "success": False,
                "result_text": "(no output)",
                "error": f"invalid child accounting payload: {type(exc).__name__}: {exc}",
            }, ensure_ascii=False), flush=True)
            return 2
        scope_context = (
            usage_scope(restored_scope)
            if restored_scope is not None
            else contextlib.nullcontext()
        )
        with scope_context:
            result = _run_async(_run_readonly_async(
                prompt=str(data.get("prompt") or ""),
                cwd=str(data.get("cwd") or "."),
                model=str(data.get("model") or "opus[1m]"),
                max_turns=int(data.get("max_turns") or DEFAULT_CLAUDE_CODE_MAX_TURNS),
                effort=data.get("effort"),
                max_budget_usd=max_budget_usd,
            ))
        print(json.dumps(result.__dict__, ensure_ascii=False), flush=True)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(_main())
