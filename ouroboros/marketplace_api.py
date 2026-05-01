"""HTTP surface for the ClawHub marketplace (v4.50).

Endpoints:

- ``GET  /api/marketplace/clawhub/search?q=&official=&limit=&cursor=``
- ``GET  /api/marketplace/clawhub/info/{slug}``
- ``GET  /api/marketplace/clawhub/installed`` — local catalog snapshot
- ``POST /api/marketplace/clawhub/install``     ``{slug, version?, auto_review?, overwrite?}``
- ``POST /api/marketplace/clawhub/update/{name}``   ``{version?}``
- ``POST /api/marketplace/clawhub/uninstall/{name}``
- ``GET  /api/marketplace/clawhub/preview/{slug}`` — staged adapter preview

Every mutating endpoint defers the heavy work to ``asyncio.to_thread``
so the Starlette event loop stays responsive while the registry HTTP
+ stage + adapter + skill_review pipeline runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import shutil
import tempfile
from typing import Any, Dict, Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros.marketplace.adapter import adapt_openclaw_skill
from ouroboros.marketplace.clawhub import (
    ClawHubClientError,
    ClawHubClientHostBlocked,
    download as _registry_download,
    info as _registry_info,
    search as _registry_search,
)
from ouroboros.marketplace.fetcher import FetchError, stage as _stage_archive
from ouroboros.marketplace.install import (
    _run_skill_review,
    install_skill,
    uninstall_skill,
    update_skill,
)
from ouroboros.marketplace.provenance import read_provenance
from ouroboros.marketplace import ouroboroshub
from ouroboros.skill_lifecycle_queue import run_lifecycle_job

log = logging.getLogger(__name__)


def _request_drive_root(request: Request) -> pathlib.Path:
    from ouroboros.config import DATA_DIR

    if hasattr(request.app, "state") and hasattr(request.app.state, "drive_root"):
        return pathlib.Path(request.app.state.drive_root)  # type: ignore[attr-defined]
    return pathlib.Path(DATA_DIR)


def _request_repo_dir(request: Request) -> pathlib.Path:
    from ouroboros.config import REPO_DIR

    if hasattr(request.app, "state") and hasattr(request.app.state, "repo_dir"):
        return pathlib.Path(request.app.state.repo_dir)  # type: ignore[attr-defined]
    return pathlib.Path(REPO_DIR)


def _enabled_check() -> Optional[JSONResponse]:
    """Compatibility no-op; ClawHub is no longer user-disabled."""
    return None


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    if isinstance(value, int):
        return bool(value)
    return default


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _client_error_response(exc: Exception, *, default_status: int = 502) -> JSONResponse:
    """Map a registry-client exception to a JSON error response."""
    if isinstance(exc, ClawHubClientHostBlocked):
        status = 400
    elif isinstance(exc, ClawHubClientError):
        status = default_status
    else:
        status = 500
    log.warning("marketplace error: %s", exc, exc_info=True)
    return JSONResponse({"error": str(exc), "code": exc.__class__.__name__}, status_code=status)


# ---------------------------------------------------------------------------
# Search / info / preview
# ---------------------------------------------------------------------------


async def api_marketplace_search(request: Request) -> JSONResponse:
    blocked = _enabled_check()
    if blocked:
        return blocked
    qp = request.query_params
    query = qp.get("q") or qp.get("query") or ""
    sort = qp.get("sort") or "registry"
    limit = _coerce_int(qp.get("limit"), 25)
    include_plugins = _coerce_bool(qp.get("include_plugins"), False)
    official_only = _coerce_bool(qp.get("official") or qp.get("only_official"), False)
    cursor = qp.get("cursor") or None
    is_text_search = bool(str(query or "").strip())
    effective_cursor = None if is_text_search else cursor
    registry_official_only = False if is_text_search else official_only
    try:
        page = await asyncio.to_thread(
            _registry_search,
            query,
            limit=limit,
            sort=sort,
            cursor=effective_cursor,
            official_only=registry_official_only,
            include_metadata=True,
            timeout_sec=15 if is_text_search else 5,
        )
    except Exception as exc:
        return _client_error_response(exc)
    results = list(page.get("results") or [])
    if not include_plugins:
        results = [r for r in results if not r.is_plugin]
    if is_text_search and official_only:
        results = [r for r in results if bool((r.badges or {}).get("official"))]
    return JSONResponse(
        {
            "query": query,
            "sort": sort,
            "limit": limit,
            "offset": 0,
            "cursor": effective_cursor,
            "next_cursor": page.get("next_cursor") or "",
            "official": official_only,
            "registry_path": page.get("path") or "packages",
            "registry_attempts": page.get("attempts") or [],
            "registry_warnings": page.get("warnings") or [],
            "registry_empty": not bool(results),
            "count": len(results),
            "results": [r.to_dict() for r in results],
        }
    )


async def api_marketplace_info(request: Request) -> JSONResponse:
    blocked = _enabled_check()
    if blocked:
        return blocked
    slug = (request.path_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    try:
        summary = await asyncio.to_thread(_registry_info, slug)
    except Exception as exc:
        return _client_error_response(exc)
    return JSONResponse(summary.to_dict())


def _preview_pipeline(slug: str, version: Optional[str]) -> Dict[str, Any]:
    """Synchronous helper for ``/preview``.

    Downloads + stages + adapts the skill into a temporary directory
    that we tear down before returning. The response carries enough
    information for the UI to show a confirmation dialog (translated
    manifest, blockers, warnings, file list, registry summary).
    """
    summary = _registry_info(slug)
    archive = _registry_download(slug, version=version or summary.latest_version)
    # Use a freshly-minted private staging directory per preview so a
    # local attacker cannot pre-create or symlink a shared root.
    # Cycle-2 GPT critic: wrap the entire stage+adapt path in a
    # try/finally so a failure in ``_stage_archive`` does not leak
    # the mkdtemp'd ``staging_root``.
    staging_root = pathlib.Path(
        tempfile.mkdtemp(prefix="ouroboros_marketplace_preview_")
    )
    try:
        staged = _stage_archive(
            archive.content,
            slug=slug,
            version=archive.version or summary.latest_version,
            expected_sha256=archive.sha256,
            staging_root=staging_root,
        )
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    try:
        adapter = adapt_openclaw_skill(
            staged.staging_dir,
            slug=slug,
            version=archive.version or summary.latest_version,
            sha256=archive.sha256,
            is_plugin=staged.has_plugin_manifest,
        )
        skill_md_path = staged.staging_dir / "SKILL.md"
        original_md_path = staged.staging_dir / "SKILL.openclaw.md"
        return {
            "slug": slug,
            "version": archive.version or summary.latest_version,
            "summary": summary.to_dict(),
            "archive": {
                "sha256": archive.sha256,
                "size_bytes": len(archive.content),
            },
            "staging": {
                "file_count": staged.file_count,
                "total_bytes": staged.total_bytes,
                "files": staged.file_list,
                "is_plugin": staged.has_plugin_manifest,
            },
            "adapter": {
                "ok": adapter.ok,
                "sanitized_name": adapter.sanitized_name,
                "warnings": adapter.warnings,
                "blockers": adapter.blockers,
                "translated_manifest": adapter.translated_frontmatter,
                "original_frontmatter": adapter.original_frontmatter,
                "skill_md_text": (
                    skill_md_path.read_text(encoding="utf-8")
                    if skill_md_path.is_file() else ""
                ),
                "openclaw_md_text": (
                    original_md_path.read_text(encoding="utf-8")
                    if original_md_path.is_file() else ""
                ),
            },
        }
    finally:
        # Clean both the inner staging dir AND the mkdtemp'd root so we
        # leave no orphaned tmp directories behind (the inner dir is a
        # child of staging_root since we passed staging_root explicitly).
        shutil.rmtree(staged.staging_dir, ignore_errors=True)
        shutil.rmtree(staging_root, ignore_errors=True)


async def api_marketplace_preview(request: Request) -> JSONResponse:
    blocked = _enabled_check()
    if blocked:
        return blocked
    slug = (request.path_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    version = (request.query_params.get("version") or "").strip() or None
    try:
        payload = await asyncio.to_thread(_preview_pipeline, slug, version)
    except FetchError as exc:
        return JSONResponse({"error": f"fetch: {exc}", "code": "FetchError"}, status_code=400)
    except ClawHubClientError as exc:
        return _client_error_response(exc)
    except Exception as exc:
        log.exception("marketplace preview failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Install / update / uninstall
# ---------------------------------------------------------------------------


def _serialize_install_result(result: Any) -> Dict[str, Any]:
    """Project an :class:`InstallResult` into a JSON-friendly dict."""
    payload: Dict[str, Any] = {
        "ok": bool(result.ok),
        "sanitized_name": result.sanitized_name,
        "error": result.error,
    }
    if result.target_dir is not None:
        payload["target_dir"] = str(result.target_dir)
    if result.summary is not None:
        payload["summary"] = result.summary.to_dict()
    if result.archive is not None:
        payload["archive"] = {
            "sha256": result.archive.sha256,
            "size_bytes": len(result.archive.content),
            "version": result.archive.version,
        }
    if result.adapter is not None:
        payload["adapter"] = {
            "ok": result.adapter.ok,
            "warnings": result.adapter.warnings,
            "blockers": result.adapter.blockers,
            "sanitized_name": result.adapter.sanitized_name,
            "is_plugin": result.adapter.is_plugin,
        }
    payload["review_status"] = result.review_status
    payload["review_findings"] = result.review_findings
    payload["review_error"] = result.review_error
    payload["deps_status"] = getattr(result, "deps_status", "")
    payload["deps_error"] = getattr(result, "deps_error", "")
    payload["deps_fingerprint"] = getattr(result, "deps_fingerprint", {})
    payload["provenance"] = result.provenance
    return payload


async def api_marketplace_install(request: Request) -> JSONResponse:
    blocked = _enabled_check()
    if blocked:
        return blocked
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    version = str(body.get("version") or "").strip() or None
    auto_review = _coerce_bool(body.get("auto_review"), True)
    overwrite = _coerce_bool(body.get("overwrite"), False)

    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    async def _run_install() -> Any:
        return await asyncio.to_thread(
            install_skill,
            drive_root,
            repo_dir,
            slug=slug,
            version=version,
            auto_review=auto_review,
            overwrite=overwrite,
        )

    try:
        result = await run_lifecycle_job(
            kind="install",
            target=slug,
            source="clawhub",
            message=f"Installing {slug}",
            runner=_run_install,
            result_message=lambda item: (
                f"Installed as {item.sanitized_name}"
                if getattr(item, "ok", False)
                else getattr(item, "error", "install failed")
            ),
            result_error=lambda item: (
                getattr(item, "error", "install failed")
                if not getattr(item, "ok", False)
                else (getattr(item, "deps_error", "") if getattr(item, "deps_status", "") == "failed" else "")
            ),
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc), "code": "marketplace_disabled"}, status_code=403)
    except Exception as exc:
        log.exception("marketplace install failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    payload = _serialize_install_result(result)
    return JSONResponse(payload, status_code=200 if result.ok else 400)


async def api_marketplace_update(request: Request) -> JSONResponse:
    blocked = _enabled_check()
    if blocked:
        return blocked
    sanitized = (request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(sanitized)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    version = str(body.get("version") or "").strip() or None
    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)
    async def _run_update() -> Any:
        return await asyncio.to_thread(
            update_skill,
            drive_root,
            repo_dir,
            sanitized_name=sanitized,
            version=version,
        )

    try:
        result = await run_lifecycle_job(
            kind="update",
            target=sanitized,
            source="clawhub",
            message=f"Updating {sanitized}",
            runner=_run_update,
            result_message=lambda item: (
                f"Updated {item.sanitized_name}"
                if getattr(item, "ok", False)
                else getattr(item, "error", "update failed")
            ),
            result_error=lambda item: (
                getattr(item, "error", "update failed")
                if not getattr(item, "ok", False)
                else (getattr(item, "deps_error", "") if getattr(item, "deps_status", "") == "failed" else "")
            ),
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc), "code": "marketplace_disabled"}, status_code=403)
    except Exception as exc:
        log.exception("marketplace update failed")
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(_serialize_install_result(result), status_code=200 if result.ok else 400)


def _validate_path_param_name(name: str) -> Optional[str]:
    """Reject names that look like path traversal at the HTTP boundary.

    Cycle 2 critics found that Starlette's ``{name}`` matcher (default
    regex ``[^/]+``) accepts ``%2e%2e`` (``..``) which then flows into
    ``shutil.rmtree(parent / "..")`` and wipes the data plane. We
    refuse the request at the HTTP layer before ANY downstream code
    runs — the install module re-validates as defence-in-depth.
    Returns an error string (caller turns into 400) or ``None`` on OK.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return "missing name"
    if cleaned in {".", ".."}:
        return f"invalid name: {cleaned!r}"
    if "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        return f"name must not contain path separators: {cleaned!r}"
    return None


async def api_marketplace_uninstall(request: Request) -> JSONResponse:
    blocked = _enabled_check()
    if blocked:
        return blocked
    sanitized = (request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(sanitized)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    drive_root = _request_drive_root(request)
    async def _run_uninstall() -> Any:
        return await asyncio.to_thread(
            uninstall_skill,
            drive_root,
            sanitized_name=sanitized,
        )

    try:
        result = await run_lifecycle_job(
            kind="uninstall",
            target=sanitized,
            source="clawhub",
            message=f"Uninstalling {sanitized}",
            runner=_run_uninstall,
            result_message=lambda item: (
                f"Uninstalled {item.sanitized_name}"
                if getattr(item, "ok", False)
                else getattr(item, "error", "uninstall failed")
            ),
            result_error=lambda item: "" if getattr(item, "ok", False) else getattr(item, "error", "uninstall failed"),
        )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc), "code": "marketplace_disabled"}, status_code=403)
    return JSONResponse(
        {
            "ok": result.ok,
            "sanitized_name": result.sanitized_name,
            "error": result.error,
        },
        status_code=200 if result.ok else 400,
    )


async def api_marketplace_installed(request: Request) -> JSONResponse:
    """List ClawHub-installed skills + provenance for the UI."""
    blocked = _enabled_check()
    if blocked:
        return blocked
    drive_root = _request_drive_root(request)
    from ouroboros.skill_loader import discover_skills, grant_status_for_skill
    from ouroboros.config import get_skills_repo_path

    skills = discover_skills(drive_root, repo_path=get_skills_repo_path())
    out = []
    for skill in skills:
        if skill.source != "clawhub":
            continue
        prov = read_provenance(drive_root, skill.name) or {}
        payload_root = ""
        try:
            rel_skill_dir = skill.skill_dir.resolve().relative_to(drive_root.resolve())
            if rel_skill_dir.parts[:1] == ("skills",):
                payload_root = rel_skill_dir.as_posix()
        except Exception:
            payload_root = ""
        out.append(
            {
                "name": skill.name,
                "type": skill.manifest.type,
                "version": skill.manifest.version,
                "review_status": skill.review.status,
                "review_stale": skill.review.is_stale_for(skill.content_hash),
                "review_findings": list(skill.review.findings or []),
                "enabled": skill.enabled,
                "load_error": skill.load_error,
                "grants": grant_status_for_skill(drive_root, skill),
                "payload_root": payload_root,
                "provenance": prov,
            }
        )
    return JSONResponse({"count": len(out), "skills": out})


# ---------------------------------------------------------------------------
# OuroborosHub static official-skill catalog
# ---------------------------------------------------------------------------


async def api_ouroboroshub_catalog(request: Request) -> JSONResponse:
    query = str(request.query_params.get("q") or request.query_params.get("query") or "").strip()
    try:
        results = await asyncio.to_thread(ouroboroshub.search, query)
    except Exception as exc:
        log.warning("OuroborosHub catalog failed: %s", exc, exc_info=True)
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"query": query, "count": len(results), "results": [item.to_dict() for item in results]})


async def api_ouroboroshub_preview(request: Request) -> JSONResponse:
    slug = str(request.path_params.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    try:
        summary = await asyncio.to_thread(ouroboroshub.info, slug)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return JSONResponse({"summary": summary.to_dict(), "files": summary.files})


async def api_ouroboroshub_install(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return JSONResponse({"error": "missing slug"}, status_code=400)
    overwrite = _coerce_bool(body.get("overwrite"), False)
    auto_review = _coerce_bool(body.get("auto_review"), True)
    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)

    async def _run_install() -> Dict[str, Any]:
        result = await asyncio.to_thread(ouroboroshub.install, slug, overwrite=overwrite)
        payload: Dict[str, Any] = {
            "ok": result.ok,
            "sanitized_name": result.sanitized_name,
            "error": result.error,
            "provenance": result.provenance,
            "summary": result.summary.to_dict() if result.summary else None,
        }
        if result.target_dir is not None:
            payload["target_dir"] = str(result.target_dir)
        if result.ok and auto_review:
            status, findings, error = await asyncio.to_thread(
                _run_skill_review,
                drive_root,
                repo_dir,
                result.sanitized_name,
            )
            payload.update({"review_status": status, "review_findings": findings, "review_error": error})
        return payload

    payload = await run_lifecycle_job(
        kind="install",
        target=slug,
        source="ouroboroshub",
        message=f"Installing {slug}",
        runner=_run_install,
        result_message=lambda item: (
            f"Installed as {item.get('sanitized_name')}"
            if item.get("ok")
            else item.get("error", "install failed")
        ),
        result_error=lambda item: "" if item.get("ok") else item.get("error", "install failed"),
    )
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)


async def api_ouroboroshub_update(request: Request) -> JSONResponse:
    name = str(request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(name)
    if err:
        return JSONResponse({"error": err}, status_code=400)
    drive_root = _request_drive_root(request)
    repo_dir = _request_repo_dir(request)

    async def _run_update() -> Dict[str, Any]:
        result = await asyncio.to_thread(ouroboroshub.install, name, overwrite=True)
        payload: Dict[str, Any] = {
            "ok": result.ok,
            "sanitized_name": result.sanitized_name,
            "error": result.error,
            "provenance": result.provenance,
            "summary": result.summary.to_dict() if result.summary else None,
        }
        if result.target_dir is not None:
            payload["target_dir"] = str(result.target_dir)
        if result.ok:
            status, findings, error = await asyncio.to_thread(
                _run_skill_review,
                drive_root,
                repo_dir,
                result.sanitized_name,
            )
            payload.update({"review_status": status, "review_findings": findings, "review_error": error})
        return payload

    payload = await run_lifecycle_job(
        kind="update",
        target=name,
        source="ouroboroshub",
        message=f"Updating {name}",
        runner=_run_update,
        result_message=lambda item: (
            f"Updated {item.get('sanitized_name')}"
            if item.get("ok")
            else item.get("error", "update failed")
        ),
        result_error=lambda item: "" if item.get("ok") else item.get("error", "update failed"),
    )
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)

async def api_ouroboroshub_installed(request: Request) -> JSONResponse:
    drive_root = _request_drive_root(request)
    from ouroboros.config import get_skills_repo_path
    from ouroboros.skill_loader import discover_skills, grant_status_for_skill

    skills = discover_skills(drive_root, repo_path=get_skills_repo_path())
    out = []
    for skill in skills:
        if skill.source != "ouroboroshub":
            continue
        payload_root = ""
        try:
            rel_skill_dir = skill.skill_dir.resolve().relative_to(drive_root.resolve())
            if rel_skill_dir.parts[:1] == ("skills",):
                payload_root = rel_skill_dir.as_posix()
        except Exception:
            payload_root = ""
        out.append({
            "name": skill.name,
            "type": skill.manifest.type,
            "version": skill.manifest.version,
            "review_status": skill.review.status,
            "review_stale": skill.review.is_stale_for(skill.content_hash),
            "review_findings": list(skill.review.findings or []),
            "enabled": skill.enabled,
            "load_error": skill.load_error,
            "grants": grant_status_for_skill(drive_root, skill),
            "payload_root": payload_root,
        })
    return JSONResponse({"count": len(out), "skills": out})


async def api_ouroboroshub_uninstall(request: Request) -> JSONResponse:
    sanitized = str(request.path_params.get("name") or "").strip()
    err = _validate_path_param_name(sanitized)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    async def _run_uninstall() -> Dict[str, Any]:
        result = await asyncio.to_thread(ouroboroshub.uninstall, sanitized)
        return {"ok": result.ok, "sanitized_name": result.sanitized_name, "error": result.error}

    payload = await run_lifecycle_job(
        kind="uninstall",
        target=sanitized,
        source="ouroboroshub",
        message=f"Uninstalling {sanitized}",
        runner=_run_uninstall,
        result_message=lambda item: (
            f"Uninstalled {item.get('sanitized_name')}"
            if item.get("ok")
            else item.get("error", "uninstall failed")
        ),
        result_error=lambda item: "" if item.get("ok") else item.get("error", "uninstall failed"),
    )
    return JSONResponse(payload, status_code=200 if payload.get("ok") else 400)


__all__ = [
    "api_marketplace_search",
    "api_marketplace_info",
    "api_marketplace_preview",
    "api_marketplace_install",
    "api_marketplace_update",
    "api_marketplace_uninstall",
    "api_marketplace_installed",
    "api_ouroboroshub_catalog",
    "api_ouroboroshub_preview",
    "api_ouroboroshub_install",
    "api_ouroboroshub_update",
    "api_ouroboroshub_installed",
    "api_ouroboroshub_uninstall",
]
