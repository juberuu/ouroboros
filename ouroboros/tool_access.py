"""Tool API v2 access matrix.

This is the single policy shape for LLM-visible tools: a profile asks to run an
operation against a resource root and receives an allow/block decision. The
legacy per-tool checks still provide defense-in-depth while the public API is
migrated to neutral tool names.
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass
from typing import Any, Literal

from ouroboros.tool_capabilities import LOCAL_READONLY_SUBAGENT_MODE
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.contracts.skill_payload_policy import resolve_skill_payload_target
from ouroboros.utils import safe_relpath


ToolProfile = Literal[
    "self_modification",
    "workspace_task",
    "skill_repair",
    "local_readonly_subagent",
    "operator_control",
]
ResourceRoot = Literal[
    "active_workspace",
    "system_repo",
    "runtime_data",
    "task_drive",
    "skill_payload",
    "artifact_store",
]
Operation = Literal[
    "read",
    "list",
    "search",
    "write",
    "edit",
    "shell",
    "vcs",
    "review",
    "delegate",
    "service",
]


@dataclass(frozen=True)
class ToolAccessDecision:
    allow: bool
    reason: str = ""
    guard: str = ""


_ALL_ROOTS: frozenset[str] = frozenset({
    "active_workspace",
    "system_repo",
    "runtime_data",
    "task_drive",
    "skill_payload",
    "artifact_store",
})

_READ_OPS = frozenset({"read", "list", "search"})

_POLICY: dict[str, dict[str, set[str]]] = {
    "local_readonly_subagent": {
        "active_workspace": set(_READ_OPS),
        "system_repo": set(_READ_OPS),
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list"},
        "artifact_store": {"read", "list"},
    },
    "skill_repair": {
        "skill_payload": {"read", "list", "search", "write", "edit", "review"},
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list"},
        "artifact_store": {"read", "list"},
    },
    "workspace_task": {
        "active_workspace": {"read", "list", "search", "write", "edit", "shell", "vcs", "service"},
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list", "write", "edit", "shell", "service"},
        "artifact_store": {"read", "list", "write"},
    },
    "self_modification": {
        "active_workspace": {"read", "list", "search", "write", "edit", "shell", "vcs", "review", "service"},
        "system_repo": {"read", "list", "search", "write", "edit", "shell", "vcs", "review", "service"},
        "runtime_data": {"read", "list", "write", "edit"},
        "task_drive": {"read", "list", "write", "edit", "shell", "service"},
        "skill_payload": {"read", "list", "search", "write", "edit", "review"},
        "artifact_store": {"read", "list", "write"},
    },
    "operator_control": {root: {"read", "list", "search", "write", "edit", "shell", "vcs", "review", "delegate", "service"} for root in _ALL_ROOTS},
}


def active_tool_profile(ctx: Any) -> ToolProfile:
    constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    mode = str(getattr(constraint, "mode", "") or "").strip()
    if mode == LOCAL_READONLY_SUBAGENT_MODE:
        return "local_readonly_subagent"
    if mode == "skill_repair":
        return "skill_repair"
    if bool(getattr(ctx, "is_workspace_mode", lambda: False)()):
        return "workspace_task"
    if bool(getattr(ctx, "is_direct_chat", False)):
        return "operator_control"
    return "self_modification"


def decide_tool_access(
    *,
    profile: ToolProfile,
    root: ResourceRoot,
    operation: Operation,
) -> ToolAccessDecision:
    allowed = operation in _POLICY.get(profile, {}).get(root, set())
    if allowed:
        return ToolAccessDecision(True, guard=f"{profile}:{root}:{operation}")
    return ToolAccessDecision(
        False,
        reason=f"profile={profile} cannot {operation} root={root}",
        guard=f"{profile}:{root}:{operation}",
    )


def normalize_root(root: str | None, *, default: ResourceRoot = "active_workspace") -> ResourceRoot:
    candidate = str(root or default).strip() or default
    if candidate not in _ALL_ROOTS:
        raise ValueError(f"unknown root {candidate!r}; expected one of {sorted(_ALL_ROOTS)}")
    return candidate  # type: ignore[return-value]


def _inside(root: pathlib.Path, path: pathlib.Path) -> pathlib.Path:
    resolved_root = pathlib.Path(root).resolve(strict=False)
    resolved = pathlib.Path(path).resolve(strict=False)
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path escapes {resolved_root}") from exc
    return resolved


def resource_root_path(
    ctx: Any,
    root: ResourceRoot,
    *,
    bucket: str = "",
    skill_name: str = "",
) -> pathlib.Path:
    if root == "active_workspace":
        active = getattr(ctx, "active_repo_dir", None)
        candidate = None
        if callable(active):
            try:
                candidate = active()
            except Exception:
                candidate = None
        if candidate is None or candidate.__class__.__module__.startswith("unittest.mock"):
            candidate = getattr(ctx, "repo_dir")
        return pathlib.Path(candidate).resolve(strict=False)
    if root == "system_repo":
        return pathlib.Path(getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir")).resolve(strict=False)
    if root == "runtime_data":
        return pathlib.Path(getattr(ctx, "drive_root")).resolve(strict=False)
    if root == "task_drive":
        task_drive = getattr(ctx, "task_drive_root", None)
        return pathlib.Path(task_drive() if callable(task_drive) else getattr(ctx, "drive_root")).resolve(strict=False)
    if root == "artifact_store":
        return pathlib.Path(getattr(ctx, "drive_root")).resolve(strict=False) / "task_results" / "artifacts"
    if root == "skill_payload":
        b = str(bucket or "").strip()
        s = str(skill_name or "").strip()
        if not b or not s:
            raise ValueError("root=skill_payload requires bucket and skill_name")
        target = resolve_skill_payload_target(
            pathlib.Path(getattr(ctx, "drive_root")),
            f"skills/{b}/{s}",
        )
        return target.payload_root
    raise ValueError(f"unknown root {root!r}")


def resolve_resource_path(
    ctx: Any,
    *,
    root: ResourceRoot,
    path: str,
    bucket: str = "",
    skill_name: str = "",
) -> pathlib.Path:
    base = resource_root_path(ctx, root, bucket=bucket, skill_name=skill_name)
    return _inside(base, base / safe_relpath(path or "."))
