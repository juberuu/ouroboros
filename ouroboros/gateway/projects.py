"""Projects gateway handlers (multi-project, v6.32.0).

Thin transport over ``ouroboros.projects_registry`` — list/create plus the
per-project chat id the UI needs to open a project thread. No business logic
here (Gateway Boundary rule).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_exception, request_drive_root, request_repo_dir

log = logging.getLogger(__name__)

# Project name auto-derived from the task objective is capped here so the
# sidebar label stays readable; the live card keeps showing full progress.
_MAX_DERIVED_NAME = 60


def _task_from_live_queue(drive_root: object, task_id: str) -> dict:
    """The task dict of a still-RUNNING/PENDING task from the queue snapshot.

    A main-chat task's task_result carries its fields only once it is written (a
    plain chat task writes them at finish). But the owner converts a card while
    the task is IN-PROGRESS, so load_task_result can miss it and the name falls
    back to the bare id (observed live: task-ae349c73). The queue snapshot
    persists every PENDING/RUNNING task (title/objective/description) at
    assignment, so it is the reliable in-flight source. Never raises."""
    try:
        import json
        import pathlib

        snap = pathlib.Path(str(drive_root)) / "state" / "queue_snapshot.json"
        if not snap.exists():
            return {}
        data = json.loads(snap.read_text(encoding="utf-8"))
        for bucket in ("running", "pending"):
            for row in (data.get(bucket) or []):
                if not isinstance(row, dict):
                    continue
                task = row.get("task") if isinstance(row.get("task"), dict) else {}
                if str(task.get("id") or row.get("id") or "") == str(task_id):
                    return task
    except Exception:
        log.debug("_task_from_live_queue failed", exc_info=True)
    return {}


def _derive_project_name(drive_root: object, task_id: str) -> str:
    """Best-effort, NO-extra-request project name for a "turn into project" card.

    Names the project with zero human input and zero extra LLM call (owner P1).
    Preference order: the model-coined short ``title`` (set at card creation),
    then the task ``objective`` (the owner's original request), then
    ``description`` — each looked up first in the persisted task_result and then
    in the live queue snapshot (for an in-progress conversion). Finally an empty
    string so the caller supplies a generic id fallback. Never raises."""
    try:
        from ouroboros.task_results import load_task_result

        result = load_task_result(drive_root, task_id) or {}
    except Exception:
        log.debug("_derive_project_name: load_task_result failed", exc_info=True)
        result = {}
    live = _task_from_live_queue(drive_root, task_id)
    raw = ""
    for field in ("title", "objective", "description"):
        for src in (result, live):
            value = str((src or {}).get(field) or "").strip()
            if value:
                raw = value
                break
        if raw:
            break
    cleaned = " ".join(raw.split())
    if len(cleaned) > _MAX_DERIVED_NAME:
        cleaned = cleaned[: _MAX_DERIVED_NAME - 1].rstrip() + "…"
    return cleaned


async def api_projects_list(request: Request) -> JSONResponse:
    try:
        from ouroboros.projects_registry import projects_summary

        return JSONResponse({"projects": projects_summary(request_drive_root(request), limit=200)})
    except Exception as exc:
        return json_exception(exc)


async def api_projects_create(request: Request) -> JSONResponse:
    try:
        from ouroboros.project_facts import explicit_project_id_ok, sanitize_project_id
        from ouroboros.projects_registry import create_project, ensure_project_workspace

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        raw_id = str(body.get("id") or body.get("project_id") or "").strip()
        if not raw_id:
            return JSONResponse({"error": "id is required"}, status_code=400)
        if not explicit_project_id_ok(raw_id):
            return JSONResponse(
                {"error": f"id {raw_id!r} is not filesystem-clean (lowercase alphanumeric/_/-/., <=64 chars)"},
                status_code=400,
            )
        drive_root = request_drive_root(request)
        entry = create_project(
            drive_root,
            sanitize_project_id(raw_id),
            name=str(body.get("name") or "").strip(),
            origin="owner_ui",
        )
        if bool(body.get("with_workspace")):
            workspace = ensure_project_workspace(drive_root, entry["id"], request_repo_dir(request))
            if workspace:
                entry = dict(entry)
                entry["working_dir"] = workspace
        return JSONResponse({"project": entry})
    except Exception as exc:
        return json_exception(exc)


async def api_project_from_task(request: Request) -> JSONResponse:
    """Create/get a project from an existing task and bind the task to it."""
    try:
        from ouroboros.project_facts import explicit_project_id_ok, sanitize_project_id
        from ouroboros.projects_registry import bind_task_to_project, create_project, touch_project

        body = await request.json()
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        task_id = str(body.get("task_id") or "").strip()
        if not task_id:
            return JSONResponse({"error": "task_id is required"}, status_code=400)
        raw_id = str(body.get("id") or body.get("project_id") or f"task-{task_id}").strip()
        if not explicit_project_id_ok(raw_id):
            return JSONResponse(
                {"error": f"id {raw_id!r} is not filesystem-clean (lowercase alphanumeric/_/-/., <=64 chars)"},
                status_code=400,
            )
        drive_root = request_drive_root(request)
        # Auto-name from the task's own title/objective when the caller sends none
        # (the one-click convert path), so no human input and no extra LLM call
        # are needed (owner P1). An explicit name still wins. Order: explicit name ->
        # server-derived (title/objective/queue) -> the frontend's objective_hint
        # (the owner's original request, for a still in-progress DIRECT chat task
        # with no server-side source yet) -> a neutral "New project". Never the bare
        # task id — the owner explicitly does not want names surfacing as "task-…".
        supplied_name = str(body.get("name") or "").strip()
        hint = " ".join(str(body.get("objective_hint") or "").split())
        if len(hint) > _MAX_DERIVED_NAME:
            hint = hint[: _MAX_DERIVED_NAME - 1].rstrip() + "…"
        project_name = supplied_name or _derive_project_name(drive_root, task_id) or hint or "New project"
        project = create_project(
            drive_root,
            sanitize_project_id(raw_id),
            name=project_name,
            origin="task_card",
        )
        binding = bind_task_to_project(drive_root, task_id, str(project["id"]), project.get("chat_id"))
        touch_project(drive_root, str(project["id"]))
        # Broadcast so every open tab + the live WS fan-out learns the new project
        # immediately, instead of waiting for the periodic /api/state poll (mirrors
        # the promote path in supervisor/workers.py).
        try:
            from supervisor.message_bus import get_bridge

            get_bridge().broadcast({
                "type": "projects_changed",
                "project_id": str(project["id"]),
                "chat_id": project.get("chat_id"),
            })
        except Exception:
            log.debug("api_project_from_task: projects_changed broadcast failed", exc_info=True)
        return JSONResponse({"project": project, "binding": binding})
    except Exception as exc:
        return json_exception(exc)


__all__ = [
    "api_project_from_task",
    "api_projects_create",
    "api_projects_list",
]
