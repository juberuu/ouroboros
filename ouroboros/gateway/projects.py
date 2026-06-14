"""Projects gateway handlers (multi-project, v6.32.0).

Thin transport over ``ouroboros.projects_registry`` — list/create/sleep/wake
plus the per-project chat id the UI needs to open a project thread. No
business logic here (Gateway Boundary rule).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.gateway._helpers import json_exception, request_drive_root, request_repo_dir

log = logging.getLogger(__name__)


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
        project = create_project(
            drive_root,
            sanitize_project_id(raw_id),
            name=str(body.get("name") or "").strip(),
            origin="task_card",
        )
        binding = bind_task_to_project(drive_root, task_id, str(project["id"]), project.get("chat_id"))
        touch_project(drive_root, str(project["id"]))
        return JSONResponse({"project": project, "binding": binding})
    except Exception as exc:
        return json_exception(exc)


async def api_project_sleep(request: Request) -> JSONResponse:
    try:
        from ouroboros.projects_registry import sleep_project

        entry = sleep_project(request_drive_root(request), str(request.path_params.get("project_id") or ""))
        if entry is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        return JSONResponse({"project": entry})
    except Exception as exc:
        return json_exception(exc)


async def api_project_wake(request: Request) -> JSONResponse:
    try:
        from ouroboros.projects_registry import wake_project

        entry = wake_project(request_drive_root(request), str(request.path_params.get("project_id") or ""))
        if entry is None:
            return JSONResponse({"error": "unknown project"}, status_code=404)
        return JSONResponse({"project": entry})
    except Exception as exc:
        return json_exception(exc)


__all__ = [
    "api_project_from_task",
    "api_project_sleep",
    "api_project_wake",
    "api_projects_create",
    "api_projects_list",
]
