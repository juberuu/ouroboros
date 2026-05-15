"""Control, update, migration, and evolution HTTP endpoints."""

from __future__ import annotations

import asyncio
import json
import pathlib
import time
from typing import Any, Dict

from starlette.requests import Request
from starlette.responses import JSONResponse

from ouroboros import get_version
from ouroboros.gateway._helpers import request_drive_root, request_repo_dir
from ouroboros.gateway.ws import broadcast_ws_sync
from ouroboros.utils import utc_now_iso

_RECENT_VISIBLE_COMMANDS: Dict[str, float] = {}
_VISIBLE_COMMAND_DEDUPE_SEC = 5.0
_evo_cache: Dict[str, Any] = {}
_evo_task: asyncio.Task | None = None


def _request_restart(request: Request) -> None:
    callback = getattr(getattr(request.app, "state", None), "request_restart", None)
    if callable(callback):
        callback()


def _runtime_branch_defaults(request: Request) -> tuple[str, str]:
    callback = getattr(getattr(request.app, "state", None), "runtime_branch_defaults", None)
    if callable(callback):
        return callback()
    return "ouroboros", "ouroboros-stable"


async def api_reset(request: Request) -> JSONResponse:
    """Reset all runtime data (state, memory, logs, settings) but keep repo."""
    import shutil

    data_dir = request_drive_root(request)
    try:
        deleted = []
        for subdir in ("state", "memory", "logs", "archive", "locks", "task_results", "uploads"):
            target = data_dir / subdir
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
                deleted.append(subdir)
        settings_file = data_dir / "settings.json"
        if settings_file.exists():
            settings_file.unlink()
            deleted.append("settings.json")
        _request_restart(request)
        return JSONResponse({"status": "ok", "deleted": deleted, "restarting": True})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_command(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        cmd = body.get("cmd", "")
        if cmd:
            from supervisor.message_bus import get_bridge, log_chat

            bridge = get_bridge()
            visible_text = str(body.get("visible_text") or "").strip()
            task_constraint = body.get("task_constraint") if isinstance(body.get("task_constraint"), dict) else None
            visible_task_id = str(body.get("visible_task_id") or "").strip()
            if visible_task_id:
                now = time.monotonic()
                expired = [
                    key for key, ts in _RECENT_VISIBLE_COMMANDS.items()
                    if now - ts > _VISIBLE_COMMAND_DEDUPE_SEC
                ]
                for key in expired:
                    _RECENT_VISIBLE_COMMANDS.pop(key, None)
                if visible_task_id in _RECENT_VISIBLE_COMMANDS:
                    return JSONResponse({"ok": True, "deduped": True, "task_id": visible_task_id})
            send_kwargs: dict[str, Any] = {"broadcast": False, "suppress_chat_log": bool(visible_text)}
            if task_constraint:
                send_kwargs["task_constraint"] = task_constraint
            bridge.ui_send(cmd, **send_kwargs)
            if visible_task_id:
                _RECENT_VISIBLE_COMMANDS[visible_task_id] = time.monotonic()
            if visible_text:
                task_id = visible_task_id or "skill_repair"
                ts = utc_now_iso()
                payload = {
                    "type": "chat",
                    "role": "system",
                    "content": visible_text,
                    "ts": ts,
                    "source": "skill_repair",
                    "system_type": "skill_repair",
                    "task_id": task_id,
                }
                broadcast_ws_sync(payload)
                log_chat(
                    "system",
                    0,
                    0,
                    visible_text,
                    ts=ts,
                    source="skill_repair",
                    task_id=task_id,
                )
        return JSONResponse({"status": "ok"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


async def api_git_log(_request: Request) -> JSONResponse:
    """Return recent commits, tags, and current branch/sha."""
    try:
        from supervisor.git_ops import git_capture, list_commits, list_versions

        commits = list_commits(max_count=30)
        tags = list_versions(max_count=20)
        rc, branch, _ = git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
        rc2, sha, _ = git_capture(["git", "rev-parse", "--short", "HEAD"])
        return JSONResponse({
            "commits": commits,
            "tags": tags,
            "branch": branch.strip() if rc == 0 else "unknown",
            "sha": sha.strip() if rc2 == 0 else "",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_git_rollback(request: Request) -> JSONResponse:
    """Roll back to a specific commit or tag, then restart."""
    try:
        body = await request.json()
        target = body.get("target", "").strip()
        if not target:
            return JSONResponse({"error": "missing target"}, status_code=400)
        from supervisor.git_ops import rollback_to_version

        ok, msg = rollback_to_version(target, reason="ui_rollback")
        if not ok:
            return JSONResponse({"error": msg}, status_code=400)
        _request_restart(request)
        return JSONResponse({"status": "ok", "message": msg})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_git_promote(request: Request) -> JSONResponse:
    """Promote the current dev branch to the runtime's stable branch."""
    try:
        import subprocess as sp

        branch_dev, branch_stable = _runtime_branch_defaults(request)
        sp.run(
            ["git", "branch", "-f", branch_stable, branch_dev],
            cwd=str(request_repo_dir(request)),
            check=True,
            capture_output=True,
        )
        return JSONResponse({"status": "ok", "message": f"{branch_stable} updated to match {branch_dev}"})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_update_status(_request: Request) -> JSONResponse:
    """Return passive managed-update status without fetching."""
    try:
        from supervisor.git_ops import compute_managed_update_status, git_capture

        status = compute_managed_update_status(fetch=False)
        latest_version = ""
        target_ref = status.get("target_ref") or ""
        if target_ref and status.get("latest_sha"):
            rc, version_text, _ = git_capture(["git", "show", f"{target_ref}:VERSION"])
            if rc == 0:
                latest_version = version_text.strip()
        return JSONResponse({
            "current_version": get_version(),
            "latest_version": latest_version,
            "official_tags": [],
            **status,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_update_check(_request: Request) -> JSONResponse:
    """Fetch the managed remote and return fresh update status."""
    try:
        from supervisor.git_ops import compute_managed_update_status, git_capture, list_official_update_tags

        status = compute_managed_update_status(fetch=True)
        latest_version = ""
        target_ref = status.get("target_ref") or ""
        if target_ref and status.get("latest_sha"):
            rc, version_text, _ = git_capture(["git", "show", f"{target_ref}:VERSION"])
            if rc == 0:
                latest_version = version_text.strip()
        return JSONResponse({
            "current_version": get_version(),
            "latest_version": latest_version,
            "official_tags": list_official_update_tags(),
            **status,
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_update_apply(request: Request) -> JSONResponse:
    """Prepare a managed update and restart so safe_restart applies it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        strategy = str(body.get("strategy") or "replace")
        from supervisor.git_ops import BRANCH_DEV, _clear_update_intent, checkout_and_reset, prepare_managed_update

        ok, payload = prepare_managed_update(strategy)
        if not ok:
            return JSONResponse(payload, status_code=409)
        try:
            checkout_ok, checkout_msg = checkout_and_reset(
                BRANCH_DEV,
                reason="ui_update_apply",
                unsynced_policy="ignore",
            )
        except Exception as checkout_exc:
            _clear_update_intent()
            return JSONResponse(
                {"error": f"Prepared update but checkout failed: {checkout_exc}", **payload},
                status_code=409,
            )
        if not checkout_ok:
            _clear_update_intent()
            return JSONResponse(
                {"error": f"Prepared update but checkout failed: {checkout_msg}", **payload},
                status_code=409,
            )
        _request_restart(request)
        return JSONResponse({"status": "ok", "restarting": True, **payload})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


async def api_evolution_data(request: Request) -> JSONResponse:
    """Collect evolution metrics for each git tag."""
    from ouroboros.utils import collect_evolution_metrics

    global _evo_task
    now = time.time()
    force_refresh = str(request.query_params.get("force") or "").strip().lower() in {"1", "true", "yes"}
    if not force_refresh and _evo_cache.get("ts") and now - _evo_cache["ts"] < 60:
        return JSONResponse({
            "points": _evo_cache["points"],
            "generated_at": _evo_cache.get("generated_at", ""),
            "cached": True,
        })
    if _evo_task is None or _evo_task.done():
        _evo_task = asyncio.create_task(
            collect_evolution_metrics(
                str(request_repo_dir(request)),
                data_dir=str(request_drive_root(request)),
            )
        )
    data_points = await _evo_task
    _evo_cache["ts"] = time.time()
    _evo_cache["points"] = data_points
    _evo_cache["generated_at"] = utc_now_iso()
    return JSONResponse({
        "points": data_points,
        "generated_at": _evo_cache["generated_at"],
        "cached": False,
    })


async def api_migrations_list(request: Request) -> JSONResponse:
    """Return the list of unread upgrade migration records."""
    target = request_drive_root(request) / "state" / "migrations.json"
    if not target.is_file():
        return JSONResponse({"migrations": []})
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return JSONResponse({"migrations": []})
    except Exception:
        return JSONResponse({"migrations": []})
    out = []
    for key, record in data.items():
        if not isinstance(record, dict):
            continue
        if record.get("dismissed"):
            continue
        out.append({"key": str(key), **{k: v for k, v in record.items() if k != "dismissed"}})
    return JSONResponse({"migrations": out})


async def api_migrations_dismiss(request: Request) -> JSONResponse:
    """Mark a migration record as dismissed so the banner stops firing."""
    key = (request.path_params.get("key") or "").strip()
    if not key or key in {".", ".."} or "/" in key or "\\" in key or "\x00" in key:
        return JSONResponse({"error": "invalid migration key"}, status_code=400)
    target = request_drive_root(request) / "state" / "migrations.json"
    if not target.is_file():
        return JSONResponse({"ok": True, "key": key, "note": "no migrations file"})
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    record = data.get(key)
    if isinstance(record, dict):
        record["dismissed"] = True
        data[key] = record
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"ok": True, "key": key})


__all__ = [
    "api_command",
    "api_evolution_data",
    "api_git_log",
    "api_git_promote",
    "api_git_rollback",
    "api_migrations_dismiss",
    "api_migrations_list",
    "api_reset",
    "api_update_apply",
    "api_update_check",
    "api_update_status",
]
