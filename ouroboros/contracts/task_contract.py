"""Task contract normalization.

The contract is a durable, LLM-readable description of what this task is trying
to accomplish.  It is not a deterministic success oracle: code records the
declared goal, constraints, resources, and artifacts; LLM review/evaluation
interprets whether the objective was met.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


_BOOLEAN_RESOURCE_NAMES = frozenset({
    "web",
    "allow_web",
    "network",
    "allow_network",
    "internet",
    "external_network",
})


def normalize_allowed_resources(value: Any) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    out: Dict[str, Any] = {}
    for key, raw in value.items():
        name = str(key or "").strip()
        if not name:
            continue
        if isinstance(raw, bool):
            out[name] = raw
        elif isinstance(raw, (int, float)) and raw in (0, 1):
            out[name] = bool(raw)
        elif isinstance(raw, str):
            text = raw.strip().lower()
            if text in {"1", "true", "yes", "y", "on", "allowed", "allow", "enabled", "enable"}:
                out[name] = True
            elif text in {"0", "false", "no", "n", "off", "denied", "deny", "disabled", "disable", "blocked", "block", "forbidden"}:
                out[name] = False
            elif name in _BOOLEAN_RESOURCE_NAMES:
                out[name] = False
            else:
                out[name] = raw
        elif raw is not None:
            out[name] = raw
    return out


def normalize_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def build_task_contract(task: Mapping[str, Any] | None) -> Dict[str, Any]:
    task = task or {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    existing = task.get("task_contract") if isinstance(task.get("task_contract"), Mapping) else {}
    existing_meta = metadata.get("task_contract") if isinstance(metadata.get("task_contract"), Mapping) else {}
    merged = {**existing_meta, **existing}

    allowed_resources = normalize_allowed_resources(
        merged.get("allowed_resources")
        or metadata.get("allowed_resources")
        or task.get("allowed_resources")
        or {}
    )
    objective = str(
        merged.get("objective")
        or task.get("objective")
        or task.get("description")
        or task.get("text")
        or ""
    ).strip()
    expected_output = str(
        merged.get("expected_output")
        or task.get("expected_output")
        or metadata.get("expected_output")
        or ""
    ).strip()
    constraints = str(
        merged.get("constraints")
        or task.get("constraints")
        or metadata.get("constraints")
        or ""
    ).strip()
    deadline_at = str(
        merged.get("deadline_at")
        or task.get("deadline_at")
        or metadata.get("deadline_at")
        or ""
    ).strip()
    workspace_root = str(
        merged.get("workspace_root")
        or task.get("workspace_root")
        or metadata.get("workspace_root")
        or ""
    ).strip()
    workspace_mode = str(
        merged.get("workspace_mode")
        or task.get("workspace_mode")
        or metadata.get("workspace_mode")
        or ""
    ).strip()
    task_type = str(merged.get("task_type") or task.get("type") or "task").strip() or "task"

    contract = {
        "schema_version": 1,
        "status": str(merged.get("status") or "draft"),
        "source": str(merged.get("source") or "host_draft"),
        "task_type": task_type,
        "objective": objective,
        "expected_output": expected_output,
        "constraints": constraints,
        "success_criteria": list(merged.get("success_criteria") or [])
        if isinstance(merged.get("success_criteria"), list)
        else [],
        "allowed_resources": allowed_resources,
        "deadline_at": deadline_at,
        "context_requires_self_body_docs": normalize_bool(
            merged.get("context_requires_self_body_docs")
            if "context_requires_self_body_docs" in merged
            else task.get("context_requires_self_body_docs", metadata.get("context_requires_self_body_docs"))
        ),
        "workspace": {
            "root": workspace_root,
            "mode": workspace_mode,
        },
        "lineage": {
            "parent_task_id": str(task.get("parent_task_id") or metadata.get("parent_task_id") or ""),
            "root_task_id": str(task.get("root_task_id") or metadata.get("root_task_id") or task.get("id") or ""),
            "session_id": str(task.get("session_id") or metadata.get("session_id") or ""),
            "delegation_role": str(task.get("delegation_role") or metadata.get("delegation_role") or "root"),
        },
    }
    for key in ("notes", "review_notes"):
        if merged.get(key):
            contract[key] = merged.get(key)
    return contract


def attach_task_contract(task: Dict[str, Any]) -> Dict[str, Any]:
    contract = build_task_contract(task)
    task["task_contract"] = contract
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    metadata["task_contract"] = contract
    task["metadata"] = metadata
    return task


__all__ = ["attach_task_contract", "build_task_contract", "normalize_allowed_resources", "normalize_bool"]
