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
CHILD_RESULT_DISPOSITION_TYPE = "child_result_disposition"
CHILD_RESULT_DISPOSITIONS = frozenset({"integrated", "irrelevant", "deferred"})
_CHILD_RESULT_DISPOSITION_FIELDS = frozenset({
    "type",
    "child_task_id",
    "disposition",
    "child_result_sha256",
})

_MAX_TEXT_CHARS = 4000
# Bound runaway growth — this is a coordination ledger, not a bulk-data store.
_MAX_LEDGER_BYTES = 2 * 1024 * 1024


def tree_ledger_path(
    root_id: str,
    *,
    data_root: pathlib.Path | None = None,
) -> pathlib.Path:
    # Strict: a root_id is always an internally-generated task id, so validate_task_id RAISES on a
    # malformed id and a typo can never build a bogus task-tree path. Read callers treat the raise as
    # "no such tree" (fail-soft); the write path (tree_ledger_append) surfaces it as a TOOL_ARG_ERROR.
    root = pathlib.Path(data_root) if data_root is not None else pathlib.Path(DATA_DIR)
    return root / "task_trees" / validate_task_id(root_id) / "blackboard.jsonl"


def normalize_child_result_disposition_payload(payload: Any) -> Dict[str, str] | None:
    """Validate the one typed child-disposition row shape.

    The task-tree row is the sole durable authority. Consumers deliberately
    ignore malformed rows instead of interpreting free text or task-result
    compatibility fields as a decision.
    """

    if not isinstance(payload, dict) or set(payload) != _CHILD_RESULT_DISPOSITION_FIELDS:
        return None
    if str(payload.get("type") or "") != CHILD_RESULT_DISPOSITION_TYPE:
        return None
    try:
        child_task_id = validate_task_id(payload.get("child_task_id"))
    except ValueError:
        return None
    disposition = str(payload.get("disposition") or "").strip().lower()
    if disposition not in CHILD_RESULT_DISPOSITIONS:
        return None
    result_sha256 = str(payload.get("child_result_sha256") or "").strip().lower()
    if len(result_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in result_sha256):
        return None
    return {
        "type": CHILD_RESULT_DISPOSITION_TYPE,
        "child_task_id": child_task_id,
        "disposition": disposition,
        "child_result_sha256": result_sha256,
    }


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
    allow_child_result_disposition: bool = False,
    data_root: pathlib.Path | None = None,
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
        elif kind_norm == "decision" and allow_child_result_disposition:
            normalized = normalize_child_result_disposition_payload(payload)
            if normalized is None:
                return (
                    "⚠️ CHILD_RESULT_DISPOSITION_INVALID: payload must contain exactly "
                    "type, child_task_id, disposition, and child_result_sha256."
                )
            # This flag is used only by join_ledger after direct-lineage and
            # current-result validation. Generic tree_note cannot bypass it.
            payload_out = normalized
        else:
            return (
                "⚠️ TOOL_ARG_ERROR (tree_note): structured payload is supported only for "
                "delegation_constraint and validated decision contracts."
            )
    path = tree_ledger_path(rid, data_root=data_root)
    try:
        # Validated child-result dispositions are the sole disposition authority
        # and are bounded by the number of children — a chatty swarm filling the
        # blackboard must not be able to lock finalization out of its ledger.
        disposition_exempt = bool(
            kind_norm == "decision" and allow_child_result_disposition
        )
        if (
            not disposition_exempt
            and path.is_file()
            and path.stat().st_size > _MAX_LEDGER_BYTES
        ):
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
    try:
        written = append_jsonl(path, row)
    except Exception:
        log.warning("Failed to append task-tree ledger row for %s", rid, exc_info=True)
        written = False
    if not written:
        return (
            "⚠️ TREE_LEDGER_WRITE_FAILED: the task-tree ledger row was not "
            "durably appended; no success acknowledgement was issued."
        )
    return f"OK: task-tree ledger[{rid}] += {kind_norm} entry ({len(body)} chars)."


def tree_ledger_rows(
    root_id: str,
    *,
    data_root: pathlib.Path | None = None,
) -> List[Dict[str, Any]]:
    try:
        path = tree_ledger_path(root_id, data_root=data_root)  # raises on malformed root_id
    except ValueError:
        return []  # reads are fail-soft: a bad/unknown scope simply has no rows
    if not path.is_file():
        return []
    return [r for r in iter_jsonl_objects(path) if isinstance(r, dict)]


def child_result_disposition_row(
    root_id: str,
    parent_task_id: str,
    child_task_id: str,
    child_result_sha256: str,
    *,
    data_root: pathlib.Path | None = None,
) -> Dict[str, Any]:
    """Fold the ledger to the latest decision for one exact child result.

    Rows for older semantic hashes remain audit evidence but never close the
    changed result. A later valid row for the same hash intentionally replaces
    an earlier decision; append order is the deterministic conflict rule.
    """

    try:
        rid = validate_task_id(root_id)
        parent = validate_task_id(parent_task_id)
        child = validate_task_id(child_task_id)
    except ValueError:
        return {}
    expected = str(child_result_sha256 or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        return {}
    current: Dict[str, Any] = {}
    for row in tree_ledger_rows(rid, data_root=data_root):
        if str(row.get("kind") or "") != "decision":
            continue
        if str(row.get("task_id") or "").strip() != parent:
            continue
        payload = normalize_child_result_disposition_payload(row.get("payload"))
        if payload is None:
            continue
        if payload["child_task_id"] != child or payload["child_result_sha256"] != expected:
            continue
        if not str(row.get("text") or "").strip():
            continue
        current = {**row, "payload": payload}
    return current


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
        elif str(payload.get("type") or "") == "child_result_disposition":
            payload_note = (
                f" {{child={payload.get('child_task_id')}, disposition={payload.get('disposition')}, "
                f"sha256={str(payload.get('child_result_sha256') or '')[:12]}}}"
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
    "CHILD_RESULT_DISPOSITION_TYPE",
    "CHILD_RESULT_DISPOSITIONS",
    "normalize_child_result_disposition_payload",
    "child_result_disposition_row",
    "tree_ledger_path",
    "tree_ledger_append",
    "tree_ledger_rows",
    "tree_ledger_tail_digest",
    "tree_ledger_attention_after",
    "open_delegation_constraints",
]
