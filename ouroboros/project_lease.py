"""One-writer-per-project lease helpers (multi-project, v6.32.0).

Pure functions consumed by ``supervisor/workers.py::assign_tasks`` under the
queue lock: a PENDING task whose ``project_id`` is already RUNNING is skipped
this assignment pass (projects serialize internally; parallelism happens
BETWEEN projects and via subagent swarms WITHIN a task).

``project_id == ""`` means "no lane": ordinary unscoped tasks never serialize
against each other. Subagents carry their parent's stored ``project_id`` but
hold no lease of their own — the parent task IS the project's writer and its
swarm must not deadlock against itself, so only top-level (non-subagent)
tasks count as lane occupants.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Set


def _as_task(item: Any) -> Any:
    """Unwrap the supervisor RUNNING meta shape ({"task": {...}, ...}) to the
    task dict; pass a bare task dict through unchanged."""
    if isinstance(item, dict) and isinstance(item.get("task"), dict):
        return item["task"]
    return item


def _task_project_id(task: Any) -> str:
    task = _as_task(task)
    if not isinstance(task, dict):
        return ""
    return str(task.get("project_id") or "").strip()


def _is_lane_occupant(task: Any) -> bool:
    """Top-level project-scoped tasks occupy the lane; subagents do not."""
    task = _as_task(task)
    if not isinstance(task, dict):
        return False
    if str(task.get("delegation_role") or "") == "subagent":
        return False
    return bool(_task_project_id(task))


def running_project_ids(running: Iterable[Any]) -> Set[str]:
    """Project ids currently holding a writer lease.

    ``running`` is the supervisor's RUNNING mapping values (or any iterable of
    task dicts); read under the queue lock by the caller.
    """
    out: Set[str] = set()
    for task in running or ():
        if _is_lane_occupant(task):
            out.add(_task_project_id(task))
    return out


def candidate_is_leasable(candidate: Dict[str, Any], running_ids: Set[str]) -> bool:
    """True when ``candidate`` may be assigned now under the one-writer rule."""
    if not _is_lane_occupant(candidate):
        return True
    return _task_project_id(candidate) not in running_ids


__all__ = ["candidate_is_leasable", "running_project_ids"]
