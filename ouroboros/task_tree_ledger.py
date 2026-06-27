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
from hashlib import sha256
from typing import Any, Dict, List

from ouroboros.config import DATA_DIR
from ouroboros.task_results import validate_task_id
from ouroboros.utils import append_jsonl, iter_jsonl_objects, utc_now_iso

log = logging.getLogger(__name__)

# Coordination artifacts + typed child->parent beacons, in one append-only ledger.
COORDINATION_KINDS = ("contract", "decision", "fact", "note")
DELEGATION_CONSTRAINT_KIND = "delegation_constraint"
BEACON_KINDS = ("milestone", "partial_finding", "blocker", "question", "interface_contract", DELEGATION_CONSTRAINT_KIND)
LEDGER_KINDS = COORDINATION_KINDS + BEACON_KINDS
# Beacons that ask the parent to look NOW (surface an early return from a sliced wait): a child is
# stuck (blocker), needs an answer (question), or needs the shared seam/contract changed
# (interface_contract) — each requires the parent to reconcile before the child can safely proceed.
ATTENTION_KINDS = ("blocker", "question", "interface_contract", DELEGATION_CONSTRAINT_KIND)
DELEGATION_CONSTRAINT_DIRECTIVES = ("halt_fanout", "cap_children", "require_lane", "block_surface")

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
    payload: Dict[str, Any] | None = None,
    allow_constraint_override: bool = False,
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
    payload_out: Dict[str, Any] = {}
    if kind_norm == DELEGATION_CONSTRAINT_KIND:
        if not isinstance(payload, dict):
            return "⚠️ TOOL_ARG_ERROR (tree_note): delegation_constraint requires a structured payload object."
        directive = str(payload.get("directive") or "").strip().lower()
        if directive not in DELEGATION_CONSTRAINT_DIRECTIVES:
            return (
                "⚠️ TOOL_ARG_ERROR (tree_note): delegation_constraint payload.directive "
                f"must be one of {DELEGATION_CONSTRAINT_DIRECTIVES}"
            )
        scope = payload.get("scope")
        if scope is not None and not isinstance(scope, (str, dict)):
            return "⚠️ TOOL_ARG_ERROR (tree_note): delegation_constraint payload.scope must be a string or object."
        raw_constraint_id = str(payload.get("constraint_id") or "").strip()
        if not raw_constraint_id:
            seed = "|".join([
                str(root_id or ""),
                str(task_id or ""),
                directive,
                str(scope or ""),
                body,
            ])
            raw_constraint_id = "dc_" + sha256(seed.encode("utf-8")).hexdigest()[:16]
        payload_out = {
            "constraint_id": raw_constraint_id,
            "directive": directive,
            "scope": scope if scope is not None else "",
            "rationale": str(payload.get("rationale") or body)[:1000],
            "created_by": str(payload.get("created_by") or task_id or role or ""),
            "advisory": bool(payload.get("advisory")),
        }
    elif payload:
        if kind_norm == "decision" and allow_constraint_override:
            payload_out = dict(payload)
        else:
            return "⚠️ TOOL_ARG_ERROR (tree_note): structured payload is supported only for delegation_constraint and override_delegation_constraint decisions."
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
    row = {
        "ts": utc_now_iso(),
        "kind": kind_norm,
        "text": body,
        "task_id": str(task_id or ""),
        "role": str(role or ""),
        "needs_parent_attention": attention,
    }
    if payload_out:
        row["payload"] = payload_out
    append_jsonl(path, row)
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
        payload = r.get("payload") if isinstance(r.get("payload"), dict) else {}
        payload_note = ""
        if str(r.get("kind") or "") == DELEGATION_CONSTRAINT_KIND and payload:
            payload_note = (
                f" {{id={payload.get('constraint_id')}, directive={payload.get('directive')}, "
                f"scope={payload.get('scope')}}}"
            )
        lines.append(
            f"- [{str(r.get('ts') or '')[:16]}] {str(r.get('kind') or 'note')}{flag} "
            f"({who}){payload_note}: {str(r.get('text') or '')}"
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


def open_delegation_constraints(root_id: str) -> List[Dict[str, Any]]:
    """Unresolved delegation constraints for a task tree.

    A constraint is resolved by a later decision row carrying
    payload.decision="overridden" for the same payload.constraint_id. Malformed
    rows are ignored by consumers (coordination must fail open).
    """

    rows = tree_ledger_rows(root_id)
    constraints: List[Dict[str, Any]] = []
    for row in rows:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        constraint_id = str(payload.get("constraint_id") or "").strip()
        if not constraint_id:
            continue
        if str(row.get("kind") or "") == "decision" and str(payload.get("decision") or "").strip().lower() == "overridden":
            constraints = [
                existing for existing in constraints
                if str((existing.get("payload") if isinstance(existing.get("payload"), dict) else {}).get("constraint_id") or "").strip()
                != constraint_id
            ]
            continue
        if str(row.get("kind") or "") == DELEGATION_CONSTRAINT_KIND:
            constraints.append(row)
    return constraints


__all__ = [
    "LEDGER_KINDS",
    "COORDINATION_KINDS",
    "BEACON_KINDS",
    "ATTENTION_KINDS",
    "DELEGATION_CONSTRAINT_DIRECTIVES",
    "DELEGATION_CONSTRAINT_KIND",
    "tree_ledger_path",
    "tree_ledger_append",
    "tree_ledger_rows",
    "tree_ledger_tail_digest",
    "tree_ledger_attention_after",
    "open_delegation_constraints",
]
