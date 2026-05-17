"""HTTP endpoints for extension catalogue, manifests, modules, and dispatch."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import pathlib
from datetime import datetime, timezone
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ouroboros.extension_loader import list_routes, snapshot
from ouroboros.gateway._helpers import (
    coerce_bool,
    request_drive_root as _request_drive_root,
    request_repo_dir as _request_repo_dir,
)
from ouroboros.skill_lifecycle_queue import (
    LifecycleJobOptions,
    queue_snapshot,
    run_blocking_preserving_cancellation,
    run_lifecycle_job,
)
from ouroboros.skill_loader import (
    discover_skills,
    find_skill,
    grant_status_for_skill,
    review_status_allows_execution,
    skill_review_gate,
)

log = logging.getLogger(__name__)


def _coerce_bool_arg(value: Any) -> bool | None:
    """Tri-state bool coercion; ``None`` means unparseable/absent."""
    sentinel = object()
    coerced = coerce_bool(value, default=sentinel)  # type: ignore[arg-type]
    return None if coerced is sentinel else coerced


def _broadcast_extension_lifecycle(request: Request, skill: str, action: Any, reason: Any = "") -> None:
    if not action:
        return
    try:
        broadcaster = getattr(request.app.state, "broadcast_ws_sync", None)
    except Exception:
        broadcaster = None
    if not callable(broadcaster):
        return
    broadcaster({
        "type": "extension_lifecycle",
        "skill": str(skill or ""),
        "action": str(action or ""),
        "reason": str(reason or ""),
    })


async def api_extensions_index(request: Request) -> JSONResponse:
    """Return discovered extensions plus live loader snapshot.

    The synchronous body runs in a worker thread and reuses discovered skills
    to avoid repeated filesystem walks during Widgets/Skills refresh.
    """
    try:
        import asyncio

        from ouroboros.config import get_skills_repo_path
        from ouroboros.skill_review_runner import reconcile_stale_review_jobs

        drive_root = _request_drive_root(request)
        repo_path = get_skills_repo_path()
        await asyncio.to_thread(reconcile_stale_review_jobs, drive_root)
        payload = await asyncio.to_thread(_build_extensions_index, drive_root, repo_path)
        return JSONResponse(payload)
    except Exception as exc:
        log.exception("api_extensions_index failure")
        return JSONResponse({"error": str(exc)}, status_code=500)


def _build_extensions_index(drive_root, repo_path):
    """Threaded, request-scope-free body for ``GET /api/extensions``."""
    from ouroboros.extension_loader import extension_name_prefix, runtime_state_for_loaded_skill

    live_snapshot = snapshot()
    # Scan data plane plus optional external checkout; bootstrap copies native refs.
    skills = discover_skills(drive_root, repo_path=repo_path)
    runtime_states = {
        s.name: runtime_state_for_loaded_skill(s, drive_root)
        for s in skills
        if s.manifest.is_extension()
    }

    def _live_tool_count(skill_name: str) -> int:
        prefix = extension_name_prefix(skill_name)
        return sum(1 for name in live_snapshot.get("tools", []) if str(name).startswith(prefix))

    def _live_route_count(skill_name: str) -> int:
        prefix = f"/api/extensions/{skill_name}/"
        return sum(1 for name in live_snapshot.get("routes", []) if str(name).startswith(prefix))

    def _live_ws_count(skill_name: str) -> int:
        prefix = extension_name_prefix(skill_name)
        return sum(1 for name in live_snapshot.get("ws_handlers", []) if str(name).startswith(prefix))

    def _pending_ui_tabs(skill_name: str) -> list[str]:
        prefix = f"{skill_name}:"
        return [
            str(name)
            for name in live_snapshot.get("ui_tabs_pending", [])
            if str(name).startswith(prefix)
        ]

    # Inline ClawHub provenance so Installed UI avoids a second round-trip.
    try:
        from ouroboros.marketplace.provenance import read_provenance
    except Exception:  # pragma: no cover — defensive
        read_provenance = lambda *_a, **_kw: None  # type: ignore[assignment]
    marketplace_enabled = True

    catalog = []

    def _path_installed_at(skill_dir: pathlib.Path) -> str:
        candidates = [skill_dir / "SKILL.md", skill_dir / "plugin.py", skill_dir]
        stamps: list[float] = []
        for candidate in candidates:
            try:
                if candidate.exists():
                    stamps.append(candidate.stat().st_mtime)
            except OSError:
                continue
        if not stamps:
            return ""
        return datetime.fromtimestamp(min(stamps), tz=timezone.utc).isoformat().replace("+00:00", "Z")

    for s in skills:
        payload_root = ""
        try:
            rel_skill_dir = s.skill_dir.resolve().relative_to(drive_root.resolve())
            if rel_skill_dir.parts[:1] == ("skills",):
                payload_root = rel_skill_dir.as_posix()
        except Exception:
            payload_root = ""
        entry = {
            "name": s.name,
            "type": s.manifest.type,
            "version": s.manifest.version,
            "description": s.manifest.description,
            "enabled": s.enabled,
            "review_status": s.review.status,
            "review_stale": s.review.is_stale_for(s.content_hash),
            "review_gate": skill_review_gate(
                s.review.status,
                stale=s.review.is_stale_for(s.content_hash),
            ),
            "executable_review": skill_review_gate(
                s.review.status,
                stale=s.review.is_stale_for(s.content_hash),
            )["executable_review"],
            "permissions": list(s.manifest.permissions or []),
            "load_error": runtime_states.get(s.name, {}).get("load_error", s.load_error),
            "desired_live": runtime_states.get(s.name, {}).get("desired_live", False),
            "live_loaded": runtime_states.get(s.name, {}).get("live_loaded", False),
            "live_reason": runtime_states.get(s.name, {}).get("reason", "not_extension"),
            "dispatch_live": bool(
                _live_tool_count(s.name)
                or _live_route_count(s.name)
                or _live_ws_count(s.name)
            ),
            "ui_tabs_pending": _pending_ui_tabs(s.name),
            "review_findings": list(s.review.findings or []),
            "grants": grant_status_for_skill(drive_root, s),
            "is_self_authored": bool(getattr(s, "is_self_authored", False)),
            # Keep source explicit so marketplace skills are not mislabeled native.
            "source": s.source,
            "payload_root": payload_root,
            "installed_at": _path_installed_at(s.skill_dir),
        }
        if s.source == "clawhub":
            try:
                prov = read_provenance(drive_root, s.name) or {}
            except Exception:  # pragma: no cover
                prov = {}
            if prov:
                if prov.get("installed_at"):
                    entry["installed_at"] = str(prov.get("installed_at") or "")
                entry["provenance"] = {
                    "slug": prov.get("slug", ""),
                    "version": prov.get("version", ""),
                    "sha256": prov.get("sha256", ""),
                    "adapter_version": prov.get("adapter_version", ""),
                    "openclaw_compat": dict(prov.get("openclaw_compat") or {}),
                    "installed_at": prov.get("installed_at", ""),
                    "updated_at": prov.get("updated_at", ""),
                }
                if marketplace_enabled:
                    entry["provenance"].update({
                        "homepage": prov.get("homepage", ""),
                        "license": prov.get("license", ""),
                        "primary_env": prov.get("primary_env", ""),
                        "adapter_warnings": list(prov.get("adapter_warnings") or []),
                        "original_manifest_sha256": prov.get("original_manifest_sha256", ""),
                        "translated_manifest_sha256": prov.get("translated_manifest_sha256", ""),
                        "registry_url": prov.get("registry_url", ""),
                    })
        catalog.append(entry)
    return {"skills": catalog, "live": live_snapshot}


async def api_extension_manifest(request: Request) -> JSONResponse:
    """GET /api/extensions/<skill>/manifest — raw manifest metadata."""
    from ouroboros.config import get_skills_repo_path
    from ouroboros.extension_loader import runtime_state_for_skill_name

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)
    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    loaded = await asyncio.to_thread(find_skill, drive_root, skill_name, repo_path=repo_path)
    if loaded is None:
        return JSONResponse({"error": "skill not found"}, status_code=404)
    runtime_state = await asyncio.to_thread(
        runtime_state_for_skill_name,
        skill_name,
        drive_root,
        repo_path=repo_path,
    )
    load_error = runtime_state.get("load_error")
    if not isinstance(load_error, str) or not load_error.strip():
        load_error = loaded.load_error
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
            "review_gate": skill_review_gate(
                loaded.review.status,
                stale=loaded.review.is_stale_for(loaded.content_hash),
            ),
            "executable_review": skill_review_gate(
                loaded.review.status,
                stale=loaded.review.is_stale_for(loaded.content_hash),
            )["executable_review"],
            "content_hash": loaded.content_hash,
            "load_error": load_error,
        }
    )


async def api_extension_module(request: Request) -> Response:
    """Serve reviewed widget module JS only for live registered tab entries."""
    from ouroboros.config import get_skills_repo_path
    from ouroboros.extension_loader import runtime_state_for_skill_name

    skill_name = str(request.path_params.get("skill") or "").strip()
    entry = str(request.path_params.get("entry") or "").strip()
    if not skill_name or not entry:
        return JSONResponse({"error": "missing skill/module entry"}, status_code=400)
    if "/" in entry or "\\" in entry or ".." in entry or entry.startswith("."):
        return JSONResponse({"error": "invalid module entry"}, status_code=400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    state = await asyncio.to_thread(
        runtime_state_for_skill_name,
        skill_name,
        drive_root,
        repo_path=repo_path,
    )
    if not state.get("desired_live"):
        return JSONResponse(
            {"error": f"extension {skill_name!r} not live: {state.get('reason')}", "state": state},
            status_code=409,
        )
    loaded = await asyncio.to_thread(find_skill, drive_root, skill_name, repo_path=repo_path)
    if loaded is None:
        return JSONResponse({"error": "skill not found"}, status_code=404)
    # Authorize against live PluginAPI tab registrations, not only manifest ui_tab.
    live = snapshot()
    module_declared = any(
        str(tab.get("skill") or "") == skill_name
        and str((tab.get("render") or {}).get("kind") or "") == "module"
        and str((tab.get("render") or {}).get("entry") or "") == entry
        for tab in live.get("ui_tabs", [])
    )
    if not module_declared:
        return JSONResponse({"error": "module entry is not declared by a live widget tab"}, status_code=404)
    target = (loaded.skill_dir / entry).resolve()
    try:
        target.relative_to(loaded.skill_dir.resolve())
    except ValueError:
        return JSONResponse({"error": "module entry escapes skill directory"}, status_code=400)
    if not target.is_file():
        return JSONResponse({"error": "module entry file not found"}, status_code=404)
    try:
        text = await asyncio.to_thread(target.read_text, encoding="utf-8")
    except UnicodeDecodeError:
        return JSONResponse({"error": "module entry is not UTF-8 text"}, status_code=400)
    return Response(
        text,
        media_type="application/javascript; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def api_extension_settings_section(request: Request) -> JSONResponse:
    """Return declarative Settings sections registered by one extension."""
    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)
    live = snapshot()
    sections = [
        item
        for item in live.get("settings_sections", [])
        if str(item.get("skill") or "") == skill_name
    ]
    return JSONResponse({"skill": skill_name, "sections": sections})


async def api_extension_dispatch(request: Request) -> Response:
    """Dispatch an extension route after reconciling live loader state."""
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros.extension_loader import reconcile_extension, runtime_state_for_skill_name

    skill = str(request.path_params.get("skill") or "").strip()
    rest = str(request.path_params.get("rest") or "").strip()
    mount = f"/api/extensions/{skill}/{rest}"
    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    spec = list_routes().get(mount)
    if spec is None and skill:
        state = await asyncio.to_thread(
            runtime_state_for_skill_name,
            skill,
            drive_root,
            repo_path=repo_path,
        )
        if state.get("desired_live"):
            state = await asyncio.to_thread(
                reconcile_extension,
                skill,
                drive_root,
                load_settings,
                repo_path=repo_path,
            )
            spec = list_routes().get(mount)
            if spec is None and state.get("action") == "extension_load_error":
                return JSONResponse(
                    {"error": f"extension {skill!r} failed to go live", "state": state},
                    status_code=409,
                )
        elif state.get("reason") != "missing":
            return JSONResponse(
                {"error": f"extension {skill!r} not live: {state.get('reason')}", "state": state},
                status_code=409,
            )
    if spec is None:
        return JSONResponse(
            {"error": f"no extension route registered for {mount!r}"},
            status_code=404,
        )
    state = await asyncio.to_thread(
        runtime_state_for_skill_name,
        str(spec.get("skill") or skill),
        drive_root,
        repo_path=repo_path,
    )
    if not state.get("desired_live") or not state.get("live_loaded"):
        state = await asyncio.to_thread(
            reconcile_extension,
            skill,
            drive_root,
            load_settings,
            repo_path=repo_path,
        )
        spec = list_routes().get(mount)
        if state.get("action") == "extension_load_error":
            return JSONResponse(
                {"error": f"extension {skill!r} failed to go live", "state": state},
                status_code=409,
            )
    if not state.get("desired_live") or not state.get("live_loaded"):
        return JSONResponse(
            {"error": f"extension {skill!r} not live: {state.get('reason')}", "state": state},
            status_code=409,
        )
    if spec is None:
        return JSONResponse(
            {"error": f"no extension route registered for {mount!r}"},
            status_code=404,
        )
    method = request.method.upper()
    allowed = {m.upper() for m in spec.get("methods", ("GET",))}
    if "GET" in allowed:
        allowed.add("HEAD")
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
        if inspect.iscoroutinefunction(handler):
            result = await handler(request)
        else:
            result = await asyncio.to_thread(handler, request)
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
    """Toggle a skill from the UI and run extension load/unload reconciliation."""
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros.skill_loader import find_skill, grant_status_for_skill, save_enabled
    from ouroboros import extension_loader

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    enabled = _coerce_bool_arg(body.get("enabled"))
    if enabled is None:
        return JSONResponse({"error": "'enabled' must be a boolean"}, status_code=400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()

    initial = await asyncio.to_thread(find_skill, drive_root, skill_name, repo_path=repo_path)
    if initial is None:
        return JSONResponse({"error": "skill not found"}, status_code=404)
    def _run_toggle_sync() -> dict[str, Any]:
        loaded = find_skill(drive_root, skill_name, repo_path=repo_path)
        if loaded is None:
            return {"error": "skill not found", "status_code": 404}
        collision_load_error = loaded.load_error.lower().startswith("skill name collision:")
        if enabled and loaded.load_error:
            return {"error": f"cannot enable: {loaded.load_error}", "status_code": 400}
        if enabled:
            stale = loaded.review.is_stale_for(loaded.content_hash)
            grants = grant_status_for_skill(drive_root, loaded)
            gate = skill_review_gate(loaded.review.status, stale=stale)
            if not gate["executable_review"]:
                return {
                    "error": "cannot enable until review status is a fresh executable review",
                    "status_code": 409,
                    "review_status": loaded.review.status,
                    "review_stale": stale,
                    "review_gate": gate,
                    "executable_review": gate["executable_review"],
                    "grants": grants,
                }
            if not grants.get("all_granted", True):
                return {
                    "error": "cannot enable until requested key grants are approved",
                    "status_code": 409,
                    "review_status": loaded.review.status,
                    "review_stale": stale,
                    "review_gate": gate,
                    "executable_review": gate["executable_review"],
                    "grants": grants,
                }
            # Mirror toggle_skill's isolated-dependency enable guard for the UI.
            try:
                from ouroboros.marketplace.install_specs import install_specs_hash
                from ouroboros.marketplace.isolated_deps import read_deps_state
                from ouroboros.skill_dependencies import auto_install_specs_for_skill

                auto_specs = auto_install_specs_for_skill(drive_root, loaded)
                if auto_specs:
                    deps_state = read_deps_state(drive_root, loaded.name)
                    deps_status = str(deps_state.get("status") or "pending")
                    expected_hash = install_specs_hash(auto_specs)
                    actual_hash = str(deps_state.get("specs_hash") or "")
                    if deps_status != "installed":
                        return {
                            "error": "cannot enable until isolated dependencies are installed",
                            "status_code": 409,
                            "deps_status": deps_status,
                            "deps_error": deps_state.get("error", ""),
                            "review_status": loaded.review.status,
                            "review_stale": stale,
                            "review_gate": gate,
                            "executable_review": gate["executable_review"],
                            "grants": grants,
                        }
                    if actual_hash != expected_hash:
                        return {
                            "error": "cannot enable until isolated dependency fingerprint is refreshed",
                            "status_code": 409,
                            "deps_status": "stale",
                            "review_status": loaded.review.status,
                            "review_stale": stale,
                            "review_gate": gate,
                            "executable_review": gate["executable_review"],
                            "grants": grants,
                        }
            except Exception:
                log.debug("api_skill_toggle deps probe failed", exc_info=True)
        if not enabled and collision_load_error:
            action = None
            if loaded.name in extension_loader.snapshot()["extensions"]:
                extension_loader.unload_extension(loaded.name)
                action = "extension_unloaded"
            return {
                "error": (
                    "cannot persist disable because this skill's sanitized "
                    "name collides with another skill directory; rename one "
                    "of the directories first"
                ),
                "status_code": 400,
                "extension_action": action,
                "extension_reason": "name_collision",
            }
        save_enabled(drive_root, loaded.name, enabled)
        action = None
        live_reason = "not_extension"
        if loaded.manifest.is_extension() or loaded.name in extension_loader.snapshot()["extensions"]:
            state = extension_loader.reconcile_extension(
                loaded.name,
                drive_root,
                load_settings,
                repo_path=repo_path,
                retry_load_error=True,
            )
            action = state.get("action")
            live_reason = str(state.get("reason") or "")
        return {
            "skill": loaded.name,
            "source": loaded.source,
            "review_status": loaded.review.status,
            "review_stale": loaded.review.is_stale_for(loaded.content_hash),
            "review_gate": skill_review_gate(
                loaded.review.status,
                stale=loaded.review.is_stale_for(loaded.content_hash),
            ),
            "executable_review": skill_review_gate(
                loaded.review.status,
                stale=loaded.review.is_stale_for(loaded.content_hash),
            )["executable_review"],
            "grants": grant_status_for_skill(drive_root, loaded),
            "action": action,
            "live_reason": live_reason,
        }

    async def _run_toggle() -> dict[str, Any]:
        return await run_blocking_preserving_cancellation(
            _run_toggle_sync,
            log_label="skill toggle lifecycle operation",
        )

    queued = await run_lifecycle_job(
        kind="enable" if enabled else "disable",
        target=initial.name,
        source=initial.source,
        message=("Enabling" if enabled else "Disabling") + f" {initial.name}",
        runner=_run_toggle,
        options=LifecycleJobOptions(
            result_message=lambda item: (
                item.get("error", "")
                or (("Enabled" if enabled else "Disabled") + f" {item.get('skill', initial.name)}")
            ),
            result_error=lambda item: item.get("error", ""),
        ),
    )
    if queued.get("error"):
        return JSONResponse(queued, status_code=int(queued.get("status_code") or 400))
    _broadcast_extension_lifecycle(
        request,
        str(queued.get("skill") or initial.name),
        queued.get("action"),
        queued.get("live_reason"),
    )
    return JSONResponse(
        {
            "skill": queued.get("skill", initial.name),
            "enabled": enabled,
            "review_status": queued.get("review_status"),
            "review_stale": queued.get("review_stale"),
            "review_gate": queued.get("review_gate"),
            "executable_review": queued.get("executable_review"),
            "grants": queued.get("grants", {}),
            "extension_action": queued.get("action"),
            "extension_reason": queued.get("live_reason"),
        }
    )


class _ApiReviewCtx:
    """Minimal ToolContext-compatible carrier for HTTP-triggered review."""

    def __init__(self, drive_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        self.drive_root = drive_root
        self.repo_dir = repo_dir
        self.task_id = "api_skill_review"
        self.current_chat_id = 0
        self.pending_events: list = []
        self.emit_progress_fn = None
        self.event_queue = None  # _emit_usage_event falls back to pending_events
        self.messages: list = []


async def api_skill_review(request: Request) -> JSONResponse:
    """Queue tri-model skill review from the UI without blocking the event loop."""
    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)

    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    ctx = _ApiReviewCtx(drive_root, repo_dir)
    from ouroboros.skill_review_runner import run_skill_review_lifecycle
    from ouroboros.skill_review import review_skill as _review_skill_impl

    payload = await run_skill_review_lifecycle(
        ctx,
        skill_name,
        source="skills",
        review_impl=_review_skill_impl,
    )
    return JSONResponse(payload)


async def api_skill_lifecycle_queue(request: Request) -> JSONResponse:
    """GET /api/skills/lifecycle-queue — recent mutating skill operations."""

    try:
        from ouroboros.skill_review_runner import reconcile_stale_review_jobs

        await asyncio.to_thread(reconcile_stale_review_jobs, _request_drive_root(request))
    except Exception:
        log.debug("stale review job reconciliation failed", exc_info=True)
    return JSONResponse(queue_snapshot())


async def api_skill_grants(request: Request) -> JSONResponse:
    """Reject direct grant writes; desktop launcher owns this boundary."""
    return JSONResponse(
        {
            "error": "key grants require desktop launcher confirmation",
            "code": "owner_confirmation_required",
        },
        status_code=403,
    )


async def api_skill_reconcile(request: Request) -> JSONResponse:
    """Re-run the extension load gate after launcher-owned grants change."""
    from ouroboros.config import get_skills_repo_path, load_settings
    from ouroboros import extension_loader

    skill_name = str(request.path_params.get("skill") or "").strip()
    if not skill_name:
        return JSONResponse({"error": "missing skill name"}, status_code=400)

    drive_root = _request_drive_root(request)
    repo_path = get_skills_repo_path()
    state = await asyncio.to_thread(
        extension_loader.reconcile_extension,
        skill_name,
        drive_root,
        load_settings,
        repo_path=repo_path,
        retry_load_error=True,
    )
    _broadcast_extension_lifecycle(
        request,
        skill_name,
        state.get("action"),
        state.get("reason"),
    )
    return JSONResponse(
        {
            "skill": skill_name,
            "extension_action": state.get("action"),
            "extension_reason": state.get("reason"),
            "live_loaded": bool(state.get("live_loaded")),
            "load_error": state.get("load_error"),
        }
    )


__all__ = [
    "api_extensions_index",
    "api_extension_manifest",
    "api_extension_module",
    "api_extension_settings_section",
    "api_extension_dispatch",
    "api_skill_toggle",
    "api_skill_review",
    "api_skill_grants",
    "api_skill_reconcile",
]
