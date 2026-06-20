"""Thin per-project journal/workpad tools (multi-project, v6.32.0).

The journal is the project's durable milestone memory (start / blocked /
checkpoint / done / note rows); the workpad is a free-form scratch page. Both
live in the per-project store (``data/projects/<id>/``), which generic data
tools cannot reach (``project_store_access_block``) — these scoped tools are
the only write path, exactly like project knowledge.

Tools resolve the project from the CURRENT task (``ctx.project_id``); an
explicit ``project_id`` argument lets the main-chat agent annotate a specific
project (e.g. when curating from the штаб).
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List

from ouroboros.project_facts import (
    project_journal_path,
    project_workpad_path,
    sanitize_project_id,
)
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import append_jsonl, iter_jsonl_objects, utc_now_iso

log = logging.getLogger(__name__)

_JOURNAL_KINDS = ("start", "checkpoint", "blocked", "done", "note")
_MAX_TEXT_CHARS = 4000
_WORKPAD_MAX_BYTES = 256 * 1024


def _authorized_project_id(ctx: ToolContext, explicit: Any) -> str:
    """AUTHORIZATION (not membership): which project THIS journal write may touch.
    Distinct from project_facts.resolve_project_id (task->project MEMBERSHIP): a
    project-scoped task may only journal into ITS OWN project (no cross-project
    writes); an explicit id is honored only from an unscoped (main/штаб) context,
    where curating a specific project is legitimate. Never consults post-hoc UI
    bindings — only the task's resolved scope (ctx.project_id) + an explicit arg."""
    own = sanitize_project_id(getattr(ctx, "project_id", "") or "")
    requested = sanitize_project_id(explicit) if explicit else ""
    if own:
        return own
    return requested


def _journal_write(ctx: ToolContext, kind: str, text: str, project_id: str = "") -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return ("⚠️ TOOL_ARG_ERROR (journal_write): no project scope — this task is not "
                "project-scoped and no explicit project_id was given.")
    kind_norm = str(kind or "note").strip().lower()
    if kind_norm not in _JOURNAL_KINDS:
        return f"⚠️ TOOL_ARG_ERROR (journal_write): kind must be one of {_JOURNAL_KINDS}"
    body = str(text or "").strip()
    if not body:
        return "⚠️ TOOL_ARG_ERROR (journal_write): text is required"
    # The journal is durable cognitive memory — never silently slice a stored
    # entry. Reject over-limit writes (same contract as workpad_write) so the
    # agent shortens the milestone or moves detail to the workpad/knowledge.
    if len(body) > _MAX_TEXT_CHARS:
        return (f"⚠️ TOOL_ARG_ERROR (journal_write): entry exceeds {_MAX_TEXT_CHARS} chars "
                f"({len(body)}) — a journal entry is a milestone note; keep it short and "
                "move long detail to workpad_write or knowledge_write.")
    path = project_journal_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "ts": utc_now_iso(),
        "kind": kind_norm,
        "text": body,
        "task_id": str(getattr(ctx, "task_id", "") or ""),
    }
    append_jsonl(path, row)
    try:
        from ouroboros.config import DATA_DIR
        from ouroboros.projects_registry import touch_project

        # Registry lives on the CANONICAL data dir (like project_journal_path),
        # not a forked child drive — touching ctx.drive_root would scatter
        # stray projects.json files onto subagent worktrees.
        touch_project(pathlib.Path(DATA_DIR), pid)
    except Exception:
        log.debug("journal touch_project failed", exc_info=True)
    return f"OK: journal[{pid}] += {kind_norm} entry ({len(body)} chars)."


def append_journal_milestone(project_id: str, kind: str, text: str, task_id: str = "") -> None:
    """Append an AUTOMATIC project journal milestone (e.g. task-completion 'letters
    home'), enforcing the SAME durable per-row contract as the journal_write tool.

    The tool REJECTS over-limit input (it teaches the agent to keep milestones
    short); an automatic milestone MUST be recorded, so when the composed text
    exceeds ``_MAX_TEXT_CHARS`` it is bounded with a VISIBLE pointer instead of
    being silently sliced or dropped (the full text always survives in the task's
    task_results and the consciousness digest). Centralizing here keeps every
    project-journal append on one bounded path (no raw append_jsonl elsewhere)."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return
    raw_kind = str(kind or "").strip().lower()
    kind_norm = raw_kind or "note"
    if kind_norm not in _JOURNAL_KINDS:
        # Fail LOUD but never LOSE the entry: an explicitly-passed unknown kind is a caller
        # bug worth surfacing, yet the milestone must still be durably recorded (as a note)
        # rather than silently dropped. (An omitted/empty kind defaults to note quietly.)
        log.warning(
            "append_journal_milestone: unknown kind %r recorded as 'note' (project=%s, task=%s)",
            raw_kind, pid, str(task_id or ""),
        )
        kind_norm = "note"
    body = str(text or "").strip()
    if not body:
        return
    if len(body) > _MAX_TEXT_CHARS:
        keep = _MAX_TEXT_CHARS - 80
        body = body[:keep] + f"… [+{len(body) - keep} chars; full text in this task's task_results / digest]"
    path = project_journal_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl(path, {
        "ts": utc_now_iso(),
        "kind": kind_norm,
        "text": body,
        "task_id": str(task_id or ""),
    })
    try:
        from ouroboros.config import DATA_DIR
        from ouroboros.projects_registry import touch_project

        touch_project(pathlib.Path(DATA_DIR), pid)
    except Exception:
        log.debug("append_journal_milestone touch_project failed", exc_info=True)


_TREE_MIRROR_KINDS = {
    # task-tree ledger kind -> durable journal kind. Only the high-signal coordination
    # survives: attention beacons + interface contracts. The low-signal coordination
    # (fact/note/decision) and routine progress (milestone/partial_finding) are NOT
    # mirrored — the journal stays a curated durable record, not a tree echo.
    "blocker": "blocked",
    "question": "note",
    "interface_contract": "note",
    "contract": "note",
}


def mirror_tree_coordination_to_journal(project_id: str, root_id: str, task_id: str = "") -> None:
    """F2 (v6.39): mirror the EPHEMERAL task-tree ledger's durable-worthy swarm coordination
    (attention beacons blocker/question/interface_contract + interface contracts) into the
    DURABLE project journal, so a swarm's decisions/blockers survive the tree's GC. Call once
    on the swarm ROOT's terminal (not per sibling) to avoid re-mirroring the same rows.
    Fail-soft and bounded (each row goes through the same per-row journal contract)."""
    pid = sanitize_project_id(project_id)
    rid = str(root_id or "").strip()
    if not pid or not rid:
        return
    try:
        from ouroboros.task_tree_ledger import tree_ledger_rows
        rows = tree_ledger_rows(rid)
    except Exception:
        log.debug("mirror_tree_coordination_to_journal read failed", exc_info=True)
        return
    for r in rows:
        kind = str(r.get("kind") or "").strip().lower()
        journal_kind = _TREE_MIRROR_KINDS.get(kind)
        if not journal_kind:
            continue
        text = str(r.get("text") or "").strip()
        if not text:
            continue
        who = str(r.get("role") or "") or str(r.get("task_id") or "")[:8]
        append_journal_milestone(pid, journal_kind, f"[swarm {kind}] ({who}): {text}", task_id=task_id)


def _journal_read(ctx: ToolContext, project_id: str = "", limit: int = 30) -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return ("⚠️ TOOL_ARG_ERROR (journal_read): no project scope — this task is not "
                "project-scoped and no explicit project_id was given.")
    path = project_journal_path(pid)
    if not path.is_file():
        return f"(journal for project {pid} is empty)"
    rows: List[Dict[str, Any]] = [r for r in iter_jsonl_objects(path) if isinstance(r, dict)]
    take = max(1, min(int(limit or 30), 200))
    omitted = max(0, len(rows) - take)
    lines = []
    if omitted:
        lines.append(f"…[{omitted} earlier entries omitted — raise limit to see more]")
    for row in rows[-take:]:
        lines.append(
            f"[{str(row.get('ts') or '')[:19]}] {str(row.get('kind') or 'note').upper()}: "
            f"{str(row.get('text') or '')}"
        )
    return f"## Project journal ({pid})\n\n" + "\n".join(lines)


def _workpad_read(ctx: ToolContext, project_id: str = "") -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return "⚠️ TOOL_ARG_ERROR (workpad_read): no project scope."
    path = project_workpad_path(pid)
    if not path.is_file():
        return f"(workpad for project {pid} is empty)"
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"⚠️ TOOL_ERROR (workpad_read): {exc}"


def _workpad_write(ctx: ToolContext, content: str, project_id: str = "") -> str:
    pid = _authorized_project_id(ctx, project_id)
    if not pid:
        return "⚠️ TOOL_ARG_ERROR (workpad_write): no project scope."
    body = str(content or "")
    if len(body.encode("utf-8", errors="ignore")) > _WORKPAD_MAX_BYTES:
        return ("⚠️ TOOL_ARG_ERROR (workpad_write): workpad exceeds 256KB — keep it a "
                "working page; move durable facts to knowledge_write and history to journal_write.")
    path = project_workpad_path(pid)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return f"⚠️ TOOL_ERROR (workpad_write): {exc}"
    return f"OK: workpad[{pid}] written ({len(body)} chars)."


def journal_tail_digest(project_id: str, *, limit: int = 40) -> str:
    """Recent project-journal milestones for context injection (no ctx needed).

    Cognitive artifact (BIBLE P1): each milestone is shown in FULL — never
    per-row prefix-sliced. Older entries beyond the tail are represented by a
    VISIBLE index pointer to journal_read (horizon preserved via pointer,
    granularity varies), never silently dropped."""
    pid = sanitize_project_id(project_id)
    if not pid:
        return ""
    path = project_journal_path(pid)
    if not path.is_file():
        return ""
    rows = [r for r in iter_jsonl_objects(path) if isinstance(r, dict)]
    if not rows:
        return ""
    take = rows[-max(1, int(limit)):]
    omitted = len(rows) - len(take)
    lines = [
        f"- [{str(r.get('ts') or '')[:16]}] {str(r.get('kind') or 'note')}: {str(r.get('text') or '')}"
        for r in take
    ]
    if omitted:
        lines.insert(0, f"- …[{omitted} earlier milestones via journal_read]")
    return "\n".join(lines)


def get_tools() -> List[ToolEntry]:
    common = {
        "project_id": {
            "type": "string",
            "description": "Explicit project id (defaults to the current task's project scope).",
            "default": "",
        },
    }
    return [
        ToolEntry(
            "journal_write",
            {
                "name": "journal_write",
                "description": (
                    "Append a milestone entry to the current project's durable journal. "
                    "kind: start | checkpoint | blocked | done | note. The journal is the "
                    "project's long-term progress memory (survives task restarts; feeds "
                    "the owner-visible project digest)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(_JOURNAL_KINDS)},
                        "text": {"type": "string", "description": "Milestone text (<=4000 chars)."},
                        **common,
                    },
                    "required": ["kind", "text"],
                },
            },
            lambda ctx, kind, text, project_id="": _journal_write(ctx, kind, text, project_id),
            timeout_sec=15,
        ),
        ToolEntry(
            "journal_read",
            {
                "name": "journal_read",
                "description": "Read the tail of the current project's journal (newest last).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 30, "description": "Max entries (<=200)."},
                        **common,
                    },
                },
            },
            lambda ctx, limit=30, project_id="": _journal_read(ctx, project_id, limit),
            timeout_sec=15,
        ),
        ToolEntry(
            "workpad_read",
            {
                "name": "workpad_read",
                "description": "Read the current project's free-form workpad page.",
                "parameters": {"type": "object", "properties": dict(common)},
            },
            lambda ctx, project_id="": _workpad_read(ctx, project_id),
            timeout_sec=15,
        ),
        ToolEntry(
            "workpad_write",
            {
                "name": "workpad_write",
                "description": (
                    "Overwrite the current project's workpad page (<=256KB). A working "
                    "page for plans/links/state — durable facts belong in knowledge_write, "
                    "history in journal_write."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        **common,
                    },
                    "required": ["content"],
                },
            },
            lambda ctx, content, project_id="": _workpad_write(ctx, content, project_id),
            timeout_sec=15,
        ),
    ]


__all__ = ["get_tools", "journal_tail_digest"]
