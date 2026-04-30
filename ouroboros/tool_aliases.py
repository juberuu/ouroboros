"""OpenClaw-style tool aliases over Ouroboros canonical tools.

Aliases are a compatibility boundary, not new tool identities.  The LLM
may see familiar names from OpenClaw/AgentSkills documentation, but every
call is normalized to the existing Ouroboros tool before safety, timeout,
parallelism, and dispatch logic runs.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict


OPENCLAW_TOOL_ALIASES: dict[str, str] = {
    "web_fetch": "browse_page",
    "fetch": "browse_page",
    "exec": "run_shell",
    "bash": "run_shell",
    "shell": "run_shell",
    "run_command": "run_shell",
    "message": "send_user_message",
    "message_user": "send_user_message",
    "notify_user": "send_user_message",
    "read_file": "repo_read",
    "write_file": "repo_write",
    "edit": "str_replace_editor",
}


def canonical_tool_name(name: str) -> str:
    """Return the canonical Ouroboros tool name for ``name``."""

    text = str(name or "").strip()
    return OPENCLAW_TOOL_ALIASES.get(text, text)


def is_tool_alias(name: str) -> bool:
    return canonical_tool_name(name) != str(name or "").strip()


def adapt_tool_args(name: str, args: Dict[str, Any] | None) -> Dict[str, Any]:
    """Translate common OpenClaw-style argument names to canonical shapes."""

    canonical = canonical_tool_name(name)
    out: Dict[str, Any] = dict(args or {})

    if canonical == "browse_page":
        if "uri" in out and "url" not in out:
            out["url"] = out.pop("uri")
        # OpenClaw's web_fetch is normally "fetch readable text".
        if str(name or "").strip() in {"web_fetch", "fetch"} and not out.get("output"):
            out["output"] = "text"

    if canonical == "run_shell":
        if "command" in out and "cmd" not in out:
            out["cmd"] = out.pop("command")

    if canonical == "send_user_message":
        for key in ("content", "message"):
            if key in out and "text" not in out:
                out["text"] = out.pop(key)
                break
        if "reason" not in out:
            out["reason"] = "OpenClaw-compatible tool alias"

    if canonical in {"repo_read", "repo_write", "str_replace_editor"}:
        if "file_path" in out and "path" not in out:
            out["path"] = out.pop("file_path")

    if canonical == "str_replace_editor":
        if "old_string" in out and "old_str" not in out:
            out["old_str"] = out.pop("old_string")
        if "new_string" in out and "new_str" not in out:
            out["new_str"] = out.pop("new_string")

    return out


def alias_schema(alias: str, canonical_schema: Dict[str, Any]) -> Dict[str, Any]:
    """Return a schema clone exposed under ``alias`` with honest copy."""

    cloned = deepcopy(canonical_schema)
    cloned["name"] = alias
    description = str(cloned.get("description") or "")
    cloned["description"] = (
        f"OpenClaw-compatible alias for `{canonical_tool_name(alias)}`. "
        f"{description}"
    ).strip()
    return cloned


def aliases_for_canonical(canonical: str) -> list[str]:
    return [
        alias
        for alias, target in sorted(OPENCLAW_TOOL_ALIASES.items())
        if target == canonical
    ]


__all__ = [
    "OPENCLAW_TOOL_ALIASES",
    "adapt_tool_args",
    "alias_schema",
    "aliases_for_canonical",
    "canonical_tool_name",
    "is_tool_alias",
]
