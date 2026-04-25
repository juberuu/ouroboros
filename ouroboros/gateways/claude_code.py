"""Claude Agent SDK gateway.

Thin adapter wrapping the `claude-agent-sdk` Python package.
Provides two execution paths:
  - edit mode: code editing with safety guards (PreToolUse hooks)
  - read-only mode: advisory review (Read/Grep/Glob only)

This is pure transport — no business logic, no git ops, no validation.
Orchestration (context loading, git stat, validation) lives in callers.

Safety model:
  1. SDK-level: allowed_tools, disallowed_tools, permission_mode,
     PreToolUse hooks for path guards
  2. Post-edit revert (registry.py) remains as defense-in-depth

Runtime model:
  The app owns the Claude runtime (bundled SDK + bundled CLI). The
  SDK's own bundled-CLI-first resolution is preserved — this gateway
  never overrides CLI path selection. Auth is ANTHROPIC_API_KEY only.

  Full raw stderr from the CLI subprocess is captured via the SDK's
  ``stderr`` callback and surfaced in ClaudeCodeResult.error on failure.

The claude-agent-sdk package is a required dependency. If it is absent,
callers receive an ImportError-derived error result with an install hint;
there is no CLI subprocess fallback path.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import pathlib
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ouroboros.config import get_runtime_mode
from ouroboros.runtime_mode_policy import (
    SAFETY_CRITICAL_PATHS,
    is_protected_runtime_path,
    mode_allows_protected_write,
    protected_write_block_message,
)

log = logging.getLogger(__name__)

# Import SDK eagerly — ImportError surfaces SDK unavailability so callers return an install hint (no CLI fallback)
from claude_agent_sdk import (  # noqa: E402
    ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
    AssistantMessage, ResultMessage, query,
)

# ---------------------------------------------------------------------------
# Stderr capture ring buffer (thread-safe, shared across invocations)
# ---------------------------------------------------------------------------
_STDERR_MAX_LINES = 200
_stderr_lock = threading.Lock()
_stderr_buffer: collections.deque[str] = collections.deque(maxlen=_STDERR_MAX_LINES)
DEFAULT_CLAUDE_CODE_MAX_TURNS = 50


def _stderr_callback(line: str) -> None:
    """SDK stderr callback — logs and stores raw CLI output."""
    log.warning("claude-cli stderr: %s", line)
    with _stderr_lock:
        _stderr_buffer.append(line)


def get_last_stderr(max_chars: int = 4000) -> str:
    """Return the most recent stderr output from the CLI subprocess."""
    with _stderr_lock:
        lines = list(_stderr_buffer)
    if not lines:
        return ""
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[-max_chars:]
    return text


def clear_stderr_buffer() -> None:
    """Clear the stderr ring buffer (e.g. after a successful run)."""
    with _stderr_lock:
        _stderr_buffer.clear()

SAFETY_CRITICAL = SAFETY_CRITICAL_PATHS


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ClaudeCodeResult:
    """Structured result from a Claude Agent SDK invocation."""

    success: bool
    result_text: str = ""
    session_id: str = ""
    cost_usd: float = 0.0
    usage: Dict[str, int] = field(default_factory=dict)
    error: str = ""
    stderr_tail: str = ""
    # Populated by callers after invocation, not by the gateway
    changed_files: List[str] = field(default_factory=list)
    diff_stat: str = ""
    validation_summary: str = ""

    def to_tool_output(self) -> str:
        """Format as structured JSON for the tool response."""
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


# ---------------------------------------------------------------------------
# PreToolUse hook: path safety guard
# ---------------------------------------------------------------------------

def make_path_guard(cwd: str):
    """Create a PreToolUse hook that blocks writes outside cwd and protected paths."""
    cwd_resolved = pathlib.Path(cwd).resolve()

    async def path_guard(input_data: dict, tool_use_id: str, context: Any) -> dict:
        tool_name = input_data.get("tool_name", "")
        tool_input = input_data.get("tool_input", {})

        # Only guard mutating tools
        if tool_name not in ("Edit", "Write", "MultiEdit"):
            return {}

        # Extract file path from tool input
        file_path = tool_input.get("file_path", "") or tool_input.get("path", "")
        if not file_path:
            return {}

        # Resolve the target path
        target = pathlib.Path(file_path)
        if not target.is_absolute():
            target = cwd_resolved / target
        target = target.resolve()

        # Check: outside cwd?
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

        # Check: protected core/contract/release file?
        # Use pathlib.as_posix() for cross-platform forward-slash comparison
        rel = target.relative_to(cwd_resolved).as_posix()
        try:
            runtime_mode = get_runtime_mode()
        except Exception:
            runtime_mode = "advanced"
        if is_protected_runtime_path(rel) and not mode_allows_protected_write(runtime_mode):
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
    """Create a PreToolUse hook that denies ALL mutating tools."""

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


# ---------------------------------------------------------------------------
# Core async runners
# ---------------------------------------------------------------------------

async def _run_edit_async(
    prompt: str,
    cwd: str,
    model: str = "opus",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    budget: Optional[float] = None,
    system_prompt: Optional[str] = None,
) -> ClaudeCodeResult:
    """Run an edit-mode SDK query with safety hooks.

    Uses ClaudeSDKClient because hooks require the client interface.
    """
    path_guard = make_path_guard(cwd)
    clear_stderr_buffer()

    options = ClaudeAgentOptions(
        cwd=cwd,
        model=model,
        permission_mode="acceptEdits",
        allowed_tools=["Read", "Edit", "Grep", "Glob"],
        disallowed_tools=["Bash", "MultiEdit"],
        max_turns=max_turns,
        max_budget_usd=budget,
        system_prompt=system_prompt,
        stderr=_stderr_callback,
        hooks={
            "PreToolUse": [
                HookMatcher(matcher="Edit|Write|MultiEdit", hooks=[path_guard]),
            ],
        },
    )

    result = ClaudeCodeResult(success=True)
    text_parts: List[str] = []

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "text") and block.text:
                            text_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result.session_id = getattr(message, "session_id", "") or ""
                    result.cost_usd = getattr(message, "total_cost_usd", 0) or 0
                    usage = getattr(message, "usage", None)
                    if isinstance(usage, dict):
                        result.usage = usage
                    subtype = getattr(message, "subtype", "")
                    if subtype and subtype != "success":
                        result.success = False
                        result.error = f"Agent ended with subtype: {subtype}"
                    # Stop iterating after ResultMessage — CLI subprocess exits here.
                    break
    except Exception as e:
        result.success = False
        result.error = f"{type(e).__name__}: {e}"

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
) -> ClaudeCodeResult:
    """Run a read-only SDK query for advisory review.

    Uses the simpler query() function since no hooks are needed —
    disallowed_tools already blocks mutating operations at the CLI level.

    effort: "low" | "medium" | "high" | "max" — controls reasoning depth.
    Default "high" so advisory reviewer thinks as deeply as blocking reviewers.
    """
    clear_stderr_buffer()
    options_kwargs: Dict[str, Any] = dict(
        cwd=cwd,
        model=model,
        permission_mode="default",  # no auto-approve
        allowed_tools=["Read", "Grep", "Glob"],
        disallowed_tools=["Bash", "Edit", "Write", "MultiEdit"],
        max_turns=max_turns,
        stderr=_stderr_callback,
    )
    if effort is not None:
        # Guard against older SDK versions that may not support the 'effort' kwarg.
        # We probe ClaudeAgentOptions.__init__ signature at call time; if 'effort'
        # is absent we silently omit it so advisory still runs (just without the
        # reasoning-depth hint). This keeps pyproject.toml at >=0.1.50 without
        # coupling to a specific version that first added 'effort'.
        import inspect as _inspect
        try:
            _sig = _inspect.signature(ClaudeAgentOptions.__init__)
            if "effort" in _sig.parameters:
                options_kwargs["effort"] = effort
        except (ValueError, TypeError):
            # Signature introspection failed (e.g. built extension type) — try anyway
            # and fall through to the except-TypeError below.
            options_kwargs["effort"] = effort

    try:
        options = ClaudeAgentOptions(**options_kwargs)
    except TypeError:
        # SDK version does not accept 'effort' — retry without it
        options_kwargs.pop("effort", None)
        options = ClaudeAgentOptions(**options_kwargs)

    result = ClaudeCodeResult(success=True)
    text_parts: List[str] = []

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if hasattr(block, "text") and block.text:
                        text_parts.append(block.text)
            elif isinstance(message, ResultMessage):
                result.session_id = getattr(message, "session_id", "") or ""
                result.cost_usd = getattr(message, "total_cost_usd", 0) or 0
                usage = getattr(message, "usage", None)
                if isinstance(usage, dict):
                    result.usage = usage
                subtype = getattr(message, "subtype", "")
                if subtype and subtype != "success":
                    result.success = False
                    result.error = f"Agent ended with subtype: {subtype}"
                # Stop iterating — the CLI subprocess exits after ResultMessage.
                # Continuing the loop causes "Command failed with exit code 1" in
                # SDK's message reader when it tries to read from the already-closed
                # subprocess stdout. Break here to avoid the spurious error.
                break
    except Exception as e:
        result.success = False
        result.error = f"{type(e).__name__}: {e}"

    if not result.success:
        result.stderr_tail = get_last_stderr()
    result.result_text = "\n".join(text_parts) if text_parts else "(no output)"
    return result


# ---------------------------------------------------------------------------
# Synchronous entry points (called from tool handlers)
# ---------------------------------------------------------------------------

def _run_async(coro):
    """Run an async coroutine from synchronous tool context.

    Worker processes have their own event loops, so asyncio.run() is safe.
    If there's already a running loop (unlikely in workers), falls back
    to creating a new loop in a thread.
    """
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


def run_edit(
    prompt: str,
    cwd: str,
    model: str = "claude-opus-4-7[1m]",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    budget: Optional[float] = None,
    system_prompt: Optional[str] = None,
) -> ClaudeCodeResult:
    """Synchronous entry point for edit-mode SDK.

    Raises ImportError if claude-agent-sdk is not installed (caught at
    module level import above).
    """
    return _run_async(_run_edit_async(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        budget=budget,
        system_prompt=system_prompt,
    ))


def resolve_claude_code_model(default: str = "claude-opus-4-7[1m]") -> str:
    """Return the configured Claude Code model from env/settings.

    Single source of truth — used by both edit path and advisory path
    to avoid model drift.  Value comes from ``CLAUDE_CODE_MODEL`` env var
    (set by config.apply_settings_to_env).  Falls back to *default* which
    matches the shipped ``SETTINGS_DEFAULTS['CLAUDE_CODE_MODEL']`` in
    ``ouroboros/config.py``. Keeping the fallback aligned with the shipped
    default avoids a cross-module drift where code reached before settings
    are applied would resolve to a different model than fresh installs see
    in the UI.

    Callers that need the raw string (e.g. to pass to the SDK) should use
    this function rather than reading the env var directly.
    """
    return os.environ.get("CLAUDE_CODE_MODEL", default).strip() or default


def run_readonly(
    prompt: str,
    cwd: str,
    model: str = "claude-opus-4-7[1m]",
    max_turns: int = DEFAULT_CLAUDE_CODE_MAX_TURNS,
    effort: Optional[str] = "high",
) -> ClaudeCodeResult:
    """Synchronous entry point for read-only advisory review.

    effort: "low" | "medium" | "high" | "max". Default "high" to match
    the reasoning depth of the downstream blocking reviewers.
    """
    return _run_async(_run_readonly_async(
        prompt=prompt,
        cwd=cwd,
        model=model,
        max_turns=max_turns,
        effort=effort,
    ))
