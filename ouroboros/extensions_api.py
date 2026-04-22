"""Phase 5 HTTP surface for Phase 4 ``type: extension`` skills.

Three endpoints are exposed:

- ``GET  /api/extensions``                       — catalogue snapshot.
- ``GET  /api/extensions/<skill>/manifest``      — raw manifest JSON.
- ``ALL  /api/extensions/<skill>/<rest>``        — dispatch to the handler
                                                   the extension registered via
                                                   ``PluginAPI.register_route``.

Combined with the Phase 4 ``extension_loader`` the agent/web UI can now
actually invoke the routes extensions attach, instead of only reading them
from ``extension_loader.snapshot()``.
"""

from __future__ import annotations

import inspect
import json
import logging
import pathlib
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.extension_loader import list_routes, snapshot
from ouroboros.skill_loader import discover_skills, find_skill

log = logging.getLogger(__name__)


async def api_extensions_index(request: Request) -> JSONResponse:
    """GET /api/extensions — catalogue + live registration snapshot.

    Returns a merged view: ``skills`` is the list of discovered
    ``type: extension`` skills (directory basename + manifest version
    + review status + enabled flag); ``live`` is the loader's
    ``snapshot()`` of in-process registrations. The UI can cross-
    reference the two to know which extensions are "catalogued but
    not yet loaded" vs "actively dispatching".
    """
    try:
        from ouroboros.config import get_skills_repo_path

        drive_root = pathlib.Path(
            request.app.state.drive_root  # type: ignore[attr-defined]
            if hasattr(request.app, "state") and hasattr(request.app.state, "drive_root")
            else pathlib.Path.home() / "Ouroboros" / "data"
        )
        repo_path = get_skills_repo_path()
        # Always scan — ``discover_skills`` still returns the bundled
        # ``repo/skills/`` reference set even when the user has not
        # configured ``OUROBOROS_SKILLS_REPO_PATH``. The earlier "only
        # scan when repo_path is non-empty" check silently dropped
        # the bundled weather skill on a default install.
        skills = discover_skills(drive_root, repo_path=repo_path)
        # Surface EVERY skill type (instruction / script / extension) so
        # the Skills UI can manage the full lifecycle. The UI filters /
        # badges by ``type`` client-side.
        catalog = [
            {
                "name": s.name,
                "type": s.manifest.type,
                "version": s.manifest.version,
                "description": s.manifest.description,
                "enabled": s.enabled,
                "review_status": s.review.status,
                "review_stale": s.review.is_stale_for(s.content_hash),
                "permissions": list(s.manifest.permissions or []),
                "load_error": s.load_error,
            }
            for s in skills
        ]
        return JSONResponse({"skills": catalog, "live": snapshot()})
    except Exception as exc:
        log.exception("api_extensions_index failure")
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_extension_manifest(request: Request) -> JSONResponse:
    """GET /api/extensions/<skill>/manifest — raw manifest metadata."""
    from ouroboros.config import get_skills_repo_path

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)
    drive_root = pathlib.Path(
        request.app.state.drive_root  # type: ignore[attr-defined]
        if hasattr(request.app, "state") and hasattr(request.app.state, "drive_root")
        else pathlib.Path.home() / "Ouroboros" / "data"
    )
    loaded = find_skill(drive_root, skill_name, repo_path=get_skills_repo_path())
    if loaded is None:
        return JSONResponse({"error": "skill not found"}, status_code=404)
    return JSONResponse(
        {
            "name": loaded.name,
            "manifest": {
                "name": loaded.manifest.name,
                "description": loaded.manifest.description,
                "version": loaded.manifest.version,
                "type": loaded.manifest.type,
                "entry": loaded.manifest.entry,
                "permissions": list(loaded.manifest.permissions or []),
                "env_from_settings": list(loaded.manifest.env_from_settings or []),
                "ui_tab": loaded.manifest.ui_tab,
            },
            "enabled": loaded.enabled,
            "review_status": loaded.review.status,
            "review_stale": loaded.review.is_stale_for(loaded.content_hash),
            "content_hash": loaded.content_hash,
            "load_error": loaded.load_error,
        }
    )


async def api_extension_dispatch(request: Request) -> Response:
    """Catch-all dispatcher for ``/api/extensions/<skill>/<rest>``.

    Looks up the fully-qualified mount point in the extension loader
    route registry and invokes the handler the extension registered
    via ``PluginAPI.register_route``. Honors the registered methods
    tuple.
    """
    skill = str(request.path_params.get("skill") or "").strip()
    rest = str(request.path_params.get("rest") or "").strip()
    mount = f"/api/extensions/{skill}/{rest}"
    spec = list_routes().get(mount)
    if spec is None:
        return JSONResponse(
            {"error": f"no extension route registered for {mount!r}"},
            status_code=404,
        )
    method = request.method.upper()
    allowed = {m.upper() for m in spec.get("methods", ("GET",))}
    if method not in allowed:
        return JSONResponse(
            {"error": f"method {method} not allowed; allowed={sorted(allowed)}"},
            status_code=405,
        )
    handler = spec.get("handler")
    if not callable(handler):
        return JSONResponse(
            {"error": "registered handler is not callable"}, status_code=500
        )
    try:
        result = handler(request)
        if inspect.iscoroutine(result):
            result = await result
    except Exception as exc:
        log.exception("extension dispatch failure: %s", mount)
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"}, status_code=500
        )
    if isinstance(result, Response):
        return result
    return JSONResponse(result if result is not None else {})


async def api_skill_toggle(request: Request) -> JSONResponse:
    """POST /api/skills/<skill>/toggle {enabled: bool}.

    Direct UI-facing endpoint so the Skills page can flip the enabled
    bit + run the extension load/unload path without routing through
    the agent. Uses the same machinery as ``toggle_skill`` tool but
    via HTTP.
    """
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros.skill_loader import find_skill, save_enabled
    from ouroboros import extension_loader

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = bool(body.get("enabled"))

    drive_root = pathlib.Path(
        request.app.state.drive_root  # type: ignore[attr-defined]
        if hasattr(request.app, "state") and hasattr(request.app.state, "drive_root")
        else pathlib.Path.home() / "Ouroboros" / "data"
    )
    loaded = find_skill(drive_root, skill_name, repo_path=get_skills_repo_path())
    if loaded is None:
        return JSONResponse({"error": "skill not found"}, status_code=404)
    if enabled and loaded.load_error:
        return JSONResponse(
            {"error": f"cannot enable: {loaded.load_error}"},
            status_code=400,
        )
    save_enabled(drive_root, loaded.name, enabled)

    action = None
    if not enabled:
        if loaded.name in extension_loader.snapshot()["extensions"]:
            extension_loader.unload_extension(loaded.name)
            action = "extension_unloaded"
    elif loaded.manifest.is_extension() and loaded.review.status == "pass":
        refreshed = find_skill(drive_root, loaded.name, repo_path=get_skills_repo_path())
        if refreshed is not None:
            extension_loader.unload_extension(loaded.name)
            err = extension_loader.load_extension(
                refreshed, load_settings, drive_root=drive_root,
            )
            action = (
                "extension_load_error: " + err if err else "extension_loaded"
            )
    return JSONResponse(
        {
            "skill": loaded.name,
            "enabled": enabled,
            "review_status": loaded.review.status,
            "extension_action": action,
        }
    )


class _ApiReviewCtx:
    """Minimal ToolContext-compatible stub for ``api_skill_review``.

    Includes every attribute the downstream review pipeline (``review.py::
    _emit_usage_event``, ``review_helpers``) reads, not only the bare
    ``drive_root`` the review itself needs. Missing ``event_queue``
    previously crashed the usage-event emission path.
    """

    def __init__(self, drive_root: pathlib.Path) -> None:
        self.drive_root = drive_root
        self.repo_dir = drive_root / ".." / "repo"  # best-effort; only referenced by some helpers
        self.task_id = "api_skill_review"
        self.current_chat_id = 0
        self.pending_events: list = []
        self.emit_progress_fn = None
        self.event_queue = None  # _emit_usage_event falls back to pending_events
        self.messages: list = []


async def api_skill_review(request: Request) -> JSONResponse:
    """POST /api/skills/<skill>/review — trigger tri-model skill review.

    Delegated Phase 5 endpoint so the Skills UI can queue a review
    without routing through the agent command bus. The tri-model
    pipeline is a multi-second blocking network op — we offload it
    to a worker thread via ``asyncio.to_thread`` so the Starlette
    event loop keeps serving other requests / WebSocket traffic while
    the review runs.
    """
    import asyncio
    from ouroboros.skill_review import review_skill as _review_skill_impl

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)

    drive_root = pathlib.Path(
        request.app.state.drive_root  # type: ignore[attr-defined]
        if hasattr(request.app, "state") and hasattr(request.app.state, "drive_root")
        else pathlib.Path.home() / "Ouroboros" / "data"
    )
    ctx = _ApiReviewCtx(drive_root)
    outcome = await asyncio.to_thread(_review_skill_impl, ctx, skill_name)
    return JSONResponse(
        {
            "skill": outcome.skill_name,
            "status": outcome.status,
            "findings": outcome.findings,
            "error": outcome.error,
            "reviewer_models": outcome.reviewer_models,
            "content_hash": outcome.content_hash,
        }
    )


__all__ = [
    "api_extensions_index",
    "api_extension_manifest",
    "api_extension_dispatch",
    "api_skill_toggle",
    "api_skill_review",
]
