"""Git rollback tool: rollback_to_target.

Wraps supervisor/git_ops.rollback_to_version() to give the agent a direct
tool for resetting the current branch to any tag or SHA.  Creates a rescue
snapshot before resetting (same safety net as the UI Restore button).

Review-exempt: restores to an already-reviewed state (see CHECKLISTS.md).
"""

from __future__ import annotations

import logging
from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _rollback_to_target(ctx: ToolContext, target: str, confirm: bool = False) -> str:
    """Reset current branch to *target* (tag name or commit SHA).

    Creates a rescue snapshot of any uncommitted work before resetting.
    Equivalent to the UI "Restore" button in Evolution → Versions.
    """
    target = (target or "").strip()
    if not target:
        return "⚠️ ROLLBACK_ERROR: target parameter is required (tag name or commit SHA)."

    if not confirm:
        # Preview: resolve target and show what will happen
        import subprocess, pathlib
        repo_dir = pathlib.Path(ctx.repo_dir)
        try:
            full_sha = subprocess.run(
                ["git", "rev-parse", "--verify", target],
                cwd=repo_dir, capture_output=True, text=True, timeout=10,
            )
            if full_sha.returncode != 0:
                return f"⚠️ ROLLBACK_ERROR: Cannot resolve '{target}' — not a valid tag or SHA."
            resolved = full_sha.stdout.strip()
        except Exception as e:
            return f"⚠️ ROLLBACK_ERROR: git rev-parse failed: {e}"

        try:
            msg = subprocess.run(
                ["git", "log", "-1", "--format=%s", resolved],
                cwd=repo_dir, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except Exception:
            msg = "(unknown)"

        try:
            head_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=repo_dir, capture_output=True, text=True, timeout=10,
            ).stdout.strip()
        except Exception:
            head_sha = "(unknown)"

        return (
            f"Will reset current branch to {target} ({resolved[:8]}):\n"
            f"  Target commit: {msg}\n"
            f"  Current HEAD:  {head_sha[:8]}\n\n"
            "A rescue snapshot of any uncommitted changes will be created first.\n"
            "Call again with confirm=true to proceed."
        )

    # Execute rollback via supervisor/git_ops
    try:
        from supervisor.git_ops import rollback_to_version
    except ImportError as e:
        return f"⚠️ ROLLBACK_ERROR: Cannot import rollback_to_version: {e}"

    ok, message = rollback_to_version(target, reason="agent_rollback_tool")
    if ok:
        log.info("rollback_to_target succeeded: %s → %s", target, message)
        # Request restart so running code matches the rolled-back worktree.
        # Uses the same ctx.pending_restart_reason pattern as _request_restart
        # in tools/control.py — flows through emit_task_results → supervisor.
        ctx.pending_restart_reason = "rollback_to_target completed"
        return f"✅ {message}\nServer restart has been requested to pick up the new state."
    else:
        return f"⚠️ ROLLBACK_ERROR: {message}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("rollback_to_target", {
            "name": "rollback_to_target",
            "description": (
                "Reset current branch to a specific tag or commit SHA. "
                "Creates a rescue snapshot first. Equivalent to UI Restore button. "
                "Review-exempt: restores to an already-reviewed state."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Tag name or commit SHA to reset to",
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": "Must be true to execute. Without it, shows a preview.",
                    },
                },
                "required": ["target", "confirm"],
            },
        }, _rollback_to_target, is_code_tool=True),
    ]
