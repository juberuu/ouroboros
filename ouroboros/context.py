"""
Ouroboros context builder.

Assembles LLM context from prompts, memory, logs, and runtime state.
Extracted from agent.py to keep the agent thin and focused.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, clip_text, estimate_tokens, get_git_info,
)
from ouroboros.memory import Memory


def _build_user_content(task: Dict[str, Any]) -> Any:
    """Build user message content. Supports text + optional image."""
    text = task.get("text", "")
    image_b64 = task.get("image_base64")
    image_mime = task.get("image_mime", "image/jpeg")

    if not image_b64:
        # Return fallback text if both text and image are empty
        if not text:
            return "(пустое сообщение)"
        return text

    # Multipart content with text + image
    parts = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.append({
        "type": "image_url",
        "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}
    })
    return parts


def build_llm_messages(
    env: Any,
    memory: Memory,
    task: Dict[str, Any],
    review_context_builder: Optional[Any] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Build the full LLM message context for a task.

    Args:
        env: Env instance with repo_path/drive_path helpers
        memory: Memory instance for scratchpad/identity/logs
        task: Task dict with id, type, text, etc.
        review_context_builder: Optional callable for review tasks (signature: () -> str)

    Returns:
        (messages, cap_info) tuple:
            - messages: List of message dicts ready for LLM
            - cap_info: Dict with token trimming metadata
    """
    # --- Extract task type for adaptive context ---
    task_type = str(task.get("type") or "user")

    # --- Read base prompts and state ---
    base_prompt = _safe_read(
        env.repo_path("prompts/SYSTEM.md"),
        fallback="You are Ouroboros. Your base prompt could not be loaded."
    )
    bible_md = _safe_read(env.repo_path("BIBLE.md"))
    readme_md = _safe_read(env.repo_path("README.md"))
    state_json = _safe_read(env.drive_path("state/state.json"), fallback="{}")
    
    # --- Load memory ---
    memory.ensure_files()
    scratchpad_raw = memory.load_scratchpad()
    identity_raw = memory.load_identity()
    
    # --- Summarize logs ---
    chat_summary = memory.summarize_chat(
        memory.read_jsonl_tail("chat.jsonl", 200))
    tools_summary = memory.summarize_tools(
        memory.read_jsonl_tail("tools.jsonl", 200))
    events_summary = memory.summarize_events(
        memory.read_jsonl_tail("events.jsonl", 200))
    supervisor_summary = memory.summarize_supervisor(
        memory.read_jsonl_tail("supervisor.jsonl", 200))
    
    # --- Git context ---
    try:
        git_branch, git_sha = get_git_info(env.repo_dir)
    except Exception:
        git_branch, git_sha = "unknown", "unknown"

    # --- Budget calculation ---
    budget_info = None
    try:
        state_data = json.loads(state_json)
        spent_usd = float(state_data.get("spent_usd", 0))
        total_usd = float(os.environ.get("TOTAL_BUDGET", "300"))
        remaining_usd = total_usd - spent_usd
        budget_info = {"total_usd": total_usd, "spent_usd": spent_usd, "remaining_usd": remaining_usd}
    except Exception:
        pass

    # --- Runtime context JSON ---
    runtime_data = {
        "utc_now": utc_now_iso(),
        "repo_dir": str(env.repo_dir),
        "drive_root": str(env.drive_root),
        "git_head": git_sha,
        "git_branch": git_branch,
        "task": {"id": task.get("id"), "type": task.get("type")},
    }
    if budget_info:
        runtime_data["budget"] = budget_info
    runtime_ctx = json.dumps(runtime_data, ensure_ascii=False, indent=2)
    
    # --- Assemble messages with prompt caching ---
    # Static content that doesn't change between rounds — cacheable
    # BIBLE.md always included (Constitution requires it for every decision)
    # README.md only for evolution/review (architecture context)
    needs_full_context = task_type in ("evolution", "review", "scheduled")
    static_text = (
        base_prompt + "\n\n"
        + "## BIBLE.md\n\n" + clip_text(bible_md, 180000)
    )
    if needs_full_context:
        static_text += "\n\n## README.md\n\n" + clip_text(readme_md, 180000)

    # Dynamic content that changes every round
    dynamic_parts = [
        "## Drive state\n\n" + clip_text(state_json, 90000),
        "## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000),
        "## Identity\n\n" + clip_text(identity_raw, 80000),
        "## Runtime context\n\n" + runtime_ctx,
    ]

    # Log summaries (optional)
    if chat_summary:
        dynamic_parts.append("## Recent chat\n\n" + chat_summary)
    if tools_summary:
        dynamic_parts.append("## Recent tools\n\n" + tools_summary)
    if events_summary:
        dynamic_parts.append("## Recent events\n\n" + events_summary)
    if supervisor_summary:
        dynamic_parts.append("## Supervisor\n\n" + supervisor_summary)

    # Review context
    if str(task.get("type") or "") == "review" and review_context_builder is not None:
        try:
            review_ctx = review_context_builder()
            if review_ctx:
                dynamic_parts.append(review_ctx)
        except Exception:
            pass

    dynamic_text = "\n\n".join(dynamic_parts)

    # Single system message with multipart content for prompt caching
    messages: List[Dict[str, Any]] = [
        {
            "role": "system",
            "content": [
                {
                    "type": "text",
                    "text": static_text,
                    "cache_control": {"type": "ephemeral", "ttl": "1h"},
                },
                {
                    "type": "text",
                    "text": dynamic_text,
                },
            ],
        },
        {"role": "user", "content": _build_user_content(task)},
    ]
    
    # --- Soft-cap token trimming ---
    messages, cap_info = apply_message_token_soft_cap(messages, 200000)
    
    return messages, cap_info


def apply_message_token_soft_cap(
    messages: List[Dict[str, Any]],
    soft_cap_tokens: int,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Trim prunable context sections if estimated tokens exceed soft cap.

    Returns (pruned_messages, cap_info_dict).
    """
    def _estimate_message_tokens(msg: Dict[str, Any]) -> int:
        """Estimate tokens for a message, handling multipart content."""
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multipart content: sum tokens from all text blocks
            total = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    total += estimate_tokens(str(block.get("text", "")))
            return total + 6
        return estimate_tokens(str(content)) + 6

    estimated = sum(_estimate_message_tokens(m) for m in messages)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }

    if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
        return messages, info

    # Prune log summaries from the dynamic text block in multipart system messages
    prunable = ["## Recent chat", "## Recent tools", "## Recent events", "## Supervisor"]
    pruned = list(messages)
    for prefix in prunable:
        if estimated <= soft_cap_tokens:
            break
        for i, msg in enumerate(pruned):
            content = msg.get("content")

            # Handle multipart content (trim from dynamic text block)
            if isinstance(content, list) and msg.get("role") == "system":
                # Find the dynamic text block (second block without cache_control)
                for j, block in enumerate(content):
                    if (isinstance(block, dict) and
                        block.get("type") == "text" and
                        "cache_control" not in block):
                        text = block.get("text", "")
                        if prefix in text:
                            # Remove this section from the dynamic text
                            lines = text.split("\n\n")
                            new_lines = []
                            skip_section = False
                            for line in lines:
                                if line.startswith(prefix):
                                    skip_section = True
                                    info["trimmed_sections"].append(prefix)
                                    continue
                                if line.startswith("##"):
                                    skip_section = False
                                if not skip_section:
                                    new_lines.append(line)

                            block["text"] = "\n\n".join(new_lines)
                            estimated = sum(_estimate_message_tokens(m) for m in pruned)
                            break
                break

            # Handle legacy string content (for backwards compatibility)
            elif isinstance(content, str) and content.startswith(prefix):
                pruned.pop(i)
                info["trimmed_sections"].append(prefix)
                estimated = sum(_estimate_message_tokens(m) for m in pruned)
                break

    info["estimated_tokens_after"] = estimated
    return pruned, info


def compact_tool_history(messages: list, keep_recent: int = 6) -> list:
    """
    Compress old tool call/result message pairs into compact summaries.

    Keeps the last `keep_recent` tool-call rounds intact (they may be
    referenced by the LLM). Older rounds get their tool results truncated
    to a short summary line, and tool_call arguments are compacted.

    This dramatically reduces prompt tokens in long tool-use conversations
    without losing important context (the tool names and whether they succeeded
    are preserved).
    """
    # Find all indices that are tool-call assistant messages
    # (messages with tool_calls field)
    tool_round_starts = []
    for i, msg in enumerate(messages):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            tool_round_starts.append(i)

    if len(tool_round_starts) <= keep_recent:
        return messages  # Nothing to compact

    # Rounds to compact: all except the last keep_recent
    rounds_to_compact = set(tool_round_starts[:-keep_recent])

    # Build compacted message list
    result = []
    for i, msg in enumerate(messages):
        # Skip system messages with multipart content (prompt caching format)
        if msg.get("role") == "system" and isinstance(msg.get("content"), list):
            result.append(msg)
            continue

        if msg.get("role") == "tool" and i > 0:
            # Check if the preceding assistant message (with tool_calls)
            # is one we want to compact
            # Find which round this tool result belongs to
            parent_round = None
            for rs in reversed(tool_round_starts):
                if rs < i:
                    parent_round = rs
                    break

            if parent_round is not None and parent_round in rounds_to_compact:
                # Compact this tool result
                content = str(msg.get("content") or "")
                is_error = content.startswith("⚠️")
                # Create a short summary
                if is_error:
                    summary = content[:200]  # Keep error details
                else:
                    # Keep first line or first 80 chars
                    first_line = content.split('\n')[0][:80]
                    char_count = len(content)
                    summary = f"{first_line}... ({char_count} chars)" if char_count > 80 else content[:200]

                result.append({**msg, "content": summary})
                continue

        # For compacted assistant messages, also trim the content (progress notes)
        # AND compact tool_call arguments
        if i in rounds_to_compact and msg.get("role") == "assistant":
            compacted_msg = dict(msg)

            # Trim content (progress notes)
            content = msg.get("content") or ""
            if len(content) > 200:
                content = content[:200] + "..."
            compacted_msg["content"] = content

            # Compact tool_call arguments
            if msg.get("tool_calls"):
                compacted_tool_calls = []
                for tc in msg["tool_calls"]:
                    compacted_tc = dict(tc)

                    # Always preserve id and function name
                    if "function" in compacted_tc:
                        func = dict(compacted_tc["function"])
                        args_str = func.get("arguments", "")

                        if args_str:
                            compacted_tc["function"] = _compact_tool_call_arguments(
                                func["name"], args_str
                            )
                        else:
                            compacted_tc["function"] = func

                    compacted_tool_calls.append(compacted_tc)

                compacted_msg["tool_calls"] = compacted_tool_calls

            result.append(compacted_msg)
            continue

        result.append(msg)

    return result


def _compact_tool_call_arguments(tool_name: str, args_json: str) -> Dict[str, Any]:
    """
    Compact tool call arguments for old rounds.

    For tools with large content payloads, remove the large field and add _truncated marker.
    For other tools, truncate arguments if > 500 chars.

    Args:
        tool_name: Name of the tool
        args_json: JSON string of tool arguments

    Returns:
        Dict with 'name' and 'arguments' (JSON string, possibly compacted)
    """
    # Tools with large content fields that should be stripped
    LARGE_CONTENT_TOOLS = {
        "repo_write_commit": "content",
        "drive_write": "content",
        "claude_code_edit": "prompt",
        "update_scratchpad": "content",
    }

    try:
        args = json.loads(args_json)

        # Check if this tool has a large content field to remove
        if tool_name in LARGE_CONTENT_TOOLS:
            large_field = LARGE_CONTENT_TOOLS[tool_name]
            if large_field in args and args[large_field]:
                args[large_field] = {"_truncated": True}
                return {"name": tool_name, "arguments": json.dumps(args, ensure_ascii=False)}

        # For other tools, if args JSON is > 500 chars, truncate
        if len(args_json) > 500:
            truncated = args_json[:200] + "..."
            return {"name": tool_name, "arguments": truncated}

        # Otherwise return unchanged
        return {"name": tool_name, "arguments": args_json}

    except (json.JSONDecodeError, Exception):
        # If we can't parse JSON, leave it unchanged
        # But still truncate if too long
        if len(args_json) > 500:
            return {"name": tool_name, "arguments": args_json[:200] + "..."}
        return {"name": tool_name, "arguments": args_json}


def _safe_read(path: pathlib.Path, fallback: str = "") -> str:
    """Read a file, returning fallback if it doesn't exist or errors."""
    try:
        if path.exists():
            return read_text(path)
    except Exception:
        pass
    return fallback
