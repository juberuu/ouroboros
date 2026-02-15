"""Управляющие инструменты: restart, promote, schedule, cancel, review, chat_history, update_scratchpad."""

from __future__ import annotations

import json
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso, write_text, run_cmd


def _request_restart(ctx: ToolContext, reason: str) -> str:
    if str(ctx.current_task_type or "") == "evolution" and not ctx.last_push_succeeded:
        return "⚠️ RESTART_BLOCKED: in evolution mode, commit+push first."
    # Persist expected SHA for post-restart verification
    try:
        sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=ctx.repo_dir)
        branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=ctx.repo_dir)
        verify_path = ctx.drive_path("state") / "pending_restart_verify.json"
        write_text(verify_path, json.dumps({
            "ts": utc_now_iso(), "expected_sha": sha,
            "expected_branch": branch, "reason": reason,
        }, ensure_ascii=False, indent=2))
    except Exception:
        pass
    ctx.pending_events.append({"type": "restart_request", "reason": reason, "ts": utc_now_iso()})
    ctx.last_push_succeeded = False
    return f"Restart requested: {reason}"


def _promote_to_stable(ctx: ToolContext, reason: str) -> str:
    ctx.pending_events.append({"type": "promote_to_stable", "reason": reason, "ts": utc_now_iso()})
    return f"Promote to stable requested: {reason}"


def _schedule_task(ctx: ToolContext, description: str) -> str:
    ctx.pending_events.append({"type": "schedule_task", "description": description, "ts": utc_now_iso()})
    return f"Scheduled: {description}"


def _cancel_task(ctx: ToolContext, task_id: str) -> str:
    ctx.pending_events.append({"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()})
    return f"Cancel requested: {task_id}"


def _request_review(ctx: ToolContext, reason: str) -> str:
    ctx.pending_events.append({"type": "review_request", "reason": reason, "ts": utc_now_iso()})
    return f"Review requested: {reason}"


def _chat_history(ctx: ToolContext, count: int = 100, offset: int = 0, search: str = "") -> str:
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    return mem.chat_history(count=count, offset=offset, search=search)


def _update_scratchpad(ctx: ToolContext, content: str) -> str:
    """LLM-driven scratchpad update (Constitution P3: LLM-first)."""
    from ouroboros.memory import Memory
    mem = Memory(drive_root=ctx.drive_root)
    mem.ensure_files()
    mem.save_scratchpad(content)
    mem.append_journal({
        "ts": utc_now_iso(),
        "content_preview": content[:500],
        "content_len": len(content),
    })
    return f"OK: scratchpad updated ({len(content)} chars)"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("request_restart", {
            "name": "request_restart",
            "description": "Ask supervisor to restart runtime (after successful push).",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }, _request_restart),
        ToolEntry("promote_to_stable", {
            "name": "promote_to_stable",
            "description": "Promote ouroboros -> ouroboros-stable. Call when you consider the code stable.",
            "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
        }, _promote_to_stable),
        ToolEntry("schedule_task", {
            "name": "schedule_task",
            "description": "Schedule a background task.",
            "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
        }, _schedule_task),
        ToolEntry("cancel_task", {
            "name": "cancel_task",
            "description": "Cancel a task by ID.",
            "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
        }, _cancel_task),
        ToolEntry("request_review", {
            "name": "request_review",
            "description": "Request a deep review of code, prompts, and state. You decide when a review is needed.",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string", "description": "Why you want a review (context for the reviewer)"},
            }, "required": ["reason"]},
        }, _request_review),
        ToolEntry("chat_history", {
            "name": "chat_history",
            "description": "Retrieve messages from chat history. Supports search.",
            "parameters": {"type": "object", "properties": {
                "count": {"type": "integer", "default": 100, "description": "Number of messages (from latest)"},
                "offset": {"type": "integer", "default": 0, "description": "Skip N from end (pagination)"},
                "search": {"type": "string", "default": "", "description": "Text filter"},
            }, "required": []},
        }, _chat_history),
        ToolEntry("update_scratchpad", {
            "name": "update_scratchpad",
            "description": "Update your working memory (scratchpad). Write the full new content. "
                           "Sections: CurrentProjects, OpenThreads, InvestigateLater, RecentEvidence. "
                           "Call after significant tasks to persist what you learned.",
            "parameters": {"type": "object", "properties": {
                "content": {"type": "string", "description": "Full scratchpad content (markdown with ## sections)"},
            }, "required": ["content"]},
        }, _update_scratchpad),
    ]
