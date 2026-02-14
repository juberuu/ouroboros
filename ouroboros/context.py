"""
Ouroboros context builder.

Assembles LLM context from prompts, memory, logs, and runtime state.
Extracted from agent.py to keep the agent thin and focused.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, clip_text, estimate_tokens, get_git_info,
)
from ouroboros.memory import Memory


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
    
    # --- Runtime context JSON ---
    runtime_ctx = json.dumps({
        "utc_now": utc_now_iso(),
        "repo_dir": str(env.repo_dir),
        "drive_root": str(env.drive_root),
        "git_head": git_sha,
        "git_branch": git_branch,
        "task": {"id": task.get("id"), "type": task.get("type")},
    }, ensure_ascii=False, indent=2)
    
    # --- Assemble messages ---
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": base_prompt},
        {"role": "system", "content": "## BIBLE.md\n\n" + clip_text(bible_md, 180000)},
        {"role": "system", "content": "## README.md\n\n" + clip_text(readme_md, 180000)},
        {"role": "system", "content": "## Drive state\n\n" + clip_text(state_json, 90000)},
        {"role": "system", "content": "## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000)},
        {"role": "system", "content": "## Identity\n\n" + clip_text(identity_raw, 80000)},
        {"role": "system", "content": "## Runtime context\n\n" + runtime_ctx},
    ]
    
    if chat_summary:
        messages.append({"role": "system", "content": "## Recent chat\n\n" + chat_summary})
    if tools_summary:
        messages.append({"role": "system", "content": "## Recent tools\n\n" + tools_summary})
    if events_summary:
        messages.append({"role": "system", "content": "## Recent events\n\n" + events_summary})
    if supervisor_summary:
        messages.append({"role": "system", "content": "## Supervisor\n\n" + supervisor_summary})
    
    # --- Review tasks: inject code snapshot + metrics ---
    if str(task.get("type") or "") == "review" and review_context_builder is not None:
        try:
            review_ctx = review_context_builder()
            if review_ctx:
                messages.append({"role": "system", "content": review_ctx})
        except Exception:
            pass
    
    # --- User message ---
    messages.append({"role": "user", "content": task.get("text", "")})
    
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
    estimated = sum(estimate_tokens(str(m.get("content", ""))) + 6 for m in messages)
    info: Dict[str, Any] = {
        "estimated_tokens_before": estimated,
        "estimated_tokens_after": estimated,
        "soft_cap_tokens": soft_cap_tokens,
        "trimmed_sections": [],
    }
    
    if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
        return messages, info
    
    # Prune log summaries first (least critical)
    prunable = ["## Recent chat", "## Recent tools", "## Recent events", "## Supervisor"]
    pruned = list(messages)
    for prefix in prunable:
        if estimated <= soft_cap_tokens:
            break
        for i, msg in enumerate(pruned):
            content = msg.get("content")
            if isinstance(content, str) and content.startswith(prefix):
                pruned.pop(i)
                info["trimmed_sections"].append(prefix)
                estimated = sum(estimate_tokens(str(m.get("content", ""))) + 6 for m in pruned)
                break
    
    info["estimated_tokens_after"] = estimated
    return pruned, info


def _safe_read(path: pathlib.Path, fallback: str = "") -> str:
    """Read a file, returning fallback if it doesn't exist or errors."""
    try:
        if path.exists():
            return read_text(path)
    except Exception:
        pass
    return fallback
