"""Task-tree coordination tools: tree_note / tree_read (the swarm blackboard + typed
child->parent beacons). Extracted from control.py for module size; storage lives in
``ouroboros.task_tree_ledger`` and is scoped by ``root_task_id`` (the whole task tree)."""

from __future__ import annotations

from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry


def tree_root_id(ctx: ToolContext) -> str:
    """Resolve the task-tree root (root_task_id), falling back to this task's own id (the
    root has no parent). Scopes the coordination ledger to the WHOLE swarm/tree."""
    md = getattr(ctx, "task_metadata", {})
    rid = str(md.get("root_task_id") or "").strip() if isinstance(md, dict) else ""
    return rid or str(getattr(ctx, "task_id", "") or "").strip()


def _tree_note(
    ctx: ToolContext,
    kind: str,
    text: str,
    needs_parent_attention: bool = False,
    payload: Dict[str, Any] | None = None,
) -> str:
    from ouroboros.task_tree_ledger import tree_ledger_append

    md = getattr(ctx, "task_metadata", {})
    role = str(md.get("role") or md.get("subagent_role") or "") if isinstance(md, dict) else ""
    return tree_ledger_append(
        tree_root_id(ctx),
        kind,
        text,
        task_id=str(getattr(ctx, "task_id", "") or ""),
        role=role,
        needs_parent_attention=bool(needs_parent_attention),
        payload=payload if isinstance(payload, dict) else None,
    )


def _tree_read(ctx: ToolContext, limit: int = 40) -> str:
    from ouroboros.task_tree_ledger import tree_ledger_tail_digest

    rid = tree_root_id(ctx)
    if not rid:
        return "⚠️ TOOL_ARG_ERROR (tree_read): no task-tree scope."
    try:
        lim = max(1, min(int(limit), 200))
    except (TypeError, ValueError):
        lim = 40
    digest = tree_ledger_tail_digest(rid, limit=lim)
    if not digest:
        return f"(task-tree coordination ledger [{rid}] is empty)"
    return f"## Task-tree coordination ledger ({rid})\n\n{digest}"


def get_tools() -> List[ToolEntry]:
    from ouroboros.task_tree_ledger import DELEGATION_CONSTRAINT_DIRECTIVES, LEDGER_KINDS

    return [
        ToolEntry("tree_note", {
            "name": "tree_note",
            "description": (
                "Append a coordination entry to the SHARED task-tree ledger (the swarm "
                "blackboard, scoped to root_task_id — visible to the parent and all "
                "siblings/descendants of THIS task tree). Use it to publish the shared "
                "frame BEFORE fanning out interdependent children and to coordinate while "
                "they run. kind: contract|decision|fact|note (coordination) or "
                "milestone|partial_finding|blocker|question|interface_contract|delegation_constraint "
                "(child->parent beacon). blocker/question/interface_contract/delegation_constraint (or "
                "needs_parent_attention=true) surface an early return "
                "in the parent's wait. Domain-agnostic: 'contract' = code APIs OR "
                "presentation section-ownership OR a research claim schema — the seam for "
                "THIS task. Keep entries short; bulk detail belongs in artifacts."
            ),
            "parameters": {"type": "object", "required": ["kind", "text"], "properties": {
                "kind": {"type": "string", "enum": list(LEDGER_KINDS)},
                "text": {"type": "string", "description": "Short coordination text (<=4000 chars)."},
                "needs_parent_attention": {"type": "boolean", "default": False, "description": "Force a parent early-wait return (implied by blocker/question/interface_contract)."},
                "payload": {
                    "type": "object",
                    "description": "Structured payload. Required for delegation_constraint: constraint_id(optional), directive, scope, rationale.",
                    "properties": {
                        "constraint_id": {"type": "string"},
                        "directive": {"type": "string", "enum": list(DELEGATION_CONSTRAINT_DIRECTIVES)},
                        "scope": {},
                        "rationale": {"type": "string"},
                        "created_by": {"type": "string"},
                    },
                },
            }},
        }, lambda ctx, kind, text, needs_parent_attention=False, payload=None: _tree_note(ctx, kind, text, needs_parent_attention, payload), timeout_sec=15),
        ToolEntry("tree_read", {
            "name": "tree_read",
            "description": (
                "Read the tail of the shared task-tree coordination ledger (newest last) — "
                "the shared frame, decisions, facts, and sibling beacons for THIS task tree."
            ),
            "parameters": {"type": "object", "properties": {
                "limit": {"type": "integer", "default": 40, "description": "Max entries (<=200)."},
            }},
        }, lambda ctx, limit=40: _tree_read(ctx, limit), timeout_sec=15),
    ]


__all__ = ["get_tools", "tree_root_id"]
