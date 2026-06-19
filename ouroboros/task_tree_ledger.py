"""Task-tree coordination ledger — the swarm blackboard + typed child->parent beacons.

Scoped by ROOT_TASK_ID (the whole task tree), so it works for ANY swarm — project or
not (email triage, research, a presentation, an OS from scratch). One append-only JSONL
holds both coordination artifacts and beacons; durable project milestones still belong in
the project journal (this ledger is EPHEMERAL coordination for one swarm run).

Domain-agnostic by design: a 'contract' is code-module APIs OR presentation
section-ownership+style OR a research claim/source schema OR an email-triage category
schema — whatever the integration seam is for THIS task. Deterministic code enforces only
form (scope, kinds, append-only, size caps); the LLM interprets meaning (BIBLE P5).
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, List

from ouroboros.config import DATA_DIR
from ouroboros.task_results import validate_task_id
from ouroboros.utils import append_jsonl, iter_jsonl_objects, utc_now_iso

log = logging.getLogger(__name__)

# Coordination artifacts + typed child->parent beacons, in one append-only ledger.
COORDINATION_KINDS = ("contract", "decision", "fact", "note")
BEACON_KINDS = ("milestone", "partial_finding", "blocker", "question", "interface_contract")
LEDGER_KINDS = COORDINATION_KINDS + BEACON_KINDS
# Beacons that ask the parent to look NOW (surface an early return from a sliced wait): a child is
# stuck (blocker), needs an answer (question), or needs the shared seam/contract changed
# (interface_contract) — each requires the parent to reconcile before the child can safely proceed.
ATTENTION_KINDS = ("blocker", "question", "interface_contract")

_MAX_TEXT_CHARS = 4000
# Bound runaway growth — this is a coordination ledger, not a bulk-data store.
_MAX_LEDGER_BYTES = 2 * 1024 * 1024


def tree_ledger_path(root_id: str) -> pathlib.Path:
    # Strict: a root_id is always an internally-generated task id, so validate_task_id RAISES on a
    # malformed id and a typo can never build a bogus task-tree path. Read callers treat the raise as
    # "no such tree" (fail-soft); the write path (tree_ledger_append) surfaces it as a TOOL_ARG_ERROR.
    return pathlib.Path(DATA_DIR) / "task_trees" / validate_task_id(root_id) / "blackboard.jsonl"


def tree_ledger_append(
    root_id: str,
    kind: str,
    text: str,
    *,
    task_id: str = "",
    role: str = "",
    needs_parent_attention: bool = False,
) -> str:
    try:
        rid = validate_task_id(root_id)
    except ValueError:
        return "⚠️ TOOL_ARG_ERROR (tree_note): no/invalid task-tree scope (root_task_id missing or malformed)."
    kind_norm = str(kind or "note").strip().lower()
    if kind_norm not in LEDGER_KINDS:
        return f"⚠️ TOOL_ARG_ERROR (tree_note): kind must be one of {LEDGER_KINDS}"
    body = str(text or "").strip()
    if not body:
        return "⚠️ TOOL_ARG_ERROR (tree_note): text is required"
    if len(body) > _MAX_TEXT_CHARS:
        return (
            f"⚠️ TOOL_ARG_ERROR (tree_note): entry exceeds {_MAX_TEXT_CHARS} chars "
            f"({len(body)}) — a ledger entry is a short coordination note; keep it terse "
            "and move bulk detail to an artifact."
        )
    path = tree_ledger_path(rid)
    try:
        if path.is_file() and path.stat().st_size > _MAX_LEDGER_BYTES:
            return (
                "⚠️ TOOL_ARG_ERROR (tree_note): the task-tree ledger is full (>2MB) — it is for "
                "coordination artifacts, not bulk data; summarize or move detail to artifacts."
            )
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    attention = bool(needs_parent_attention) or kind_norm in ATTENTION_KINDS
    append_jsonl(
        path,
        {
            "ts": utc_now_iso(),
            "kind": kind_norm,
            "text": body,
            "task_id": str(task_id or ""),
            "role": str(role or ""),
            "needs_parent_attention": attention,
        },
    )
    return f"OK: task-tree ledger[{rid}] += {kind_norm} entry ({len(body)} chars)."


def tree_ledger_rows(root_id: str) -> List[Dict[str, Any]]:
    try:
        path = tree_ledger_path(root_id)  # raises on a malformed root_id
    except ValueError:
        return []  # reads are fail-soft: a bad/unknown scope simply has no rows
    if not path.is_file():
        return []
    return [r for r in iter_jsonl_objects(path) if isinstance(r, dict)]


def tree_ledger_tail_digest(root_id: str, *, limit: int = 40) -> str:
    """Recent ledger entries for context injection (no ctx needed). Each entry shown in
    full; older entries beyond the tail represented by a visible pointer to tree_read."""
    rows = tree_ledger_rows(root_id)
    if not rows:
        return ""
    take = rows[-max(1, int(limit)):]
    omitted = len(rows) - len(take)
    lines: List[str] = []
    if omitted:
        lines.append(f"- …[{omitted} earlier ledger entries via tree_read]")
    for r in take:
        flag = " ⚠needs_parent_attention" if r.get("needs_parent_attention") else ""
        who = str(r.get("role") or "") or str(r.get("task_id") or "")[:8]
        lines.append(
            f"- [{str(r.get('ts') or '')[:16]}] {str(r.get('kind') or 'note')}{flag} "
            f"({who}): {str(r.get('text') or '')}"
        )
    return "\n".join(lines)


def tree_ledger_attention_after(root_id: str, after_ts: str) -> List[Dict[str, Any]]:
    """Attention-beacons (blocker/question/interface_contract) strictly after after_ts — drives the
    sliced wait's early return so a parent reacts to a child's beacon without waiting for it to
    terminate."""
    out: List[Dict[str, Any]] = []
    for r in tree_ledger_rows(root_id):
        if not r.get("needs_parent_attention"):
            continue
        ts = str(r.get("ts") or "")
        if after_ts and ts <= after_ts:
            continue
        out.append(r)
    return out


__all__ = [
    "LEDGER_KINDS",
    "COORDINATION_KINDS",
    "BEACON_KINDS",
    "ATTENTION_KINDS",
    "tree_ledger_path",
    "tree_ledger_append",
    "tree_ledger_rows",
    "tree_ledger_tail_digest",
    "tree_ledger_attention_after",
]
