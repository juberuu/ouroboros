"""Task-scoped long-running service manager."""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ouroboros.observability import redact_projection, write_blob
from ouroboros.platform_layer import (
    bootstrap_process_path,
    kill_process_group_id,
    kill_process_tree,
    process_group_id,
    subprocess_new_group_kwargs,
)
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for
from ouroboros.utils import safe_relpath, utc_now_iso


@dataclass
class ServiceRecord:
    name: str
    service_id: str
    task_id: str
    cmd: List[str]
    cwd: str
    log_path: pathlib.Path
    proc: subprocess.Popen
    pgid: int = 0
    started_at: float = field(default_factory=time.time)
    readiness: Dict[str, Any] = field(default_factory=dict)
    ready: bool = False


_LOCK = threading.Lock()
_SERVICES: Dict[str, ServiceRecord] = {}
_SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_MAX_SERVICE_LOG_BLOB_BYTES = 5_000_000
_MAX_SERVICE_LOG_TAIL_CHARS = 80_000


def _service_key(ctx: ToolContext, name: str) -> str:
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    return f"{task_id}:{name}"


def _resolve_cwd(ctx: ToolContext, cwd: str) -> pathlib.Path:
    root = active_repo_dir_for(ctx).resolve(strict=False)
    text = str(cwd or "").strip()
    if not text or text in {".", "./"}:
        return root
    raw = pathlib.Path(text).expanduser()
    target = raw.resolve(strict=False) if raw.is_absolute() else (root / safe_relpath(text)).resolve(strict=False)
    target.relative_to(root)
    return target


def _tail(path: pathlib.Path, chars: int) -> str:
    if not path.exists():
        return ""
    size = path.stat().st_size
    limit = max(0, int(chars))
    with path.open("rb") as fh:
        fh.seek(max(0, size - limit))
        data = fh.read(limit)
    return data.decode("utf-8", errors="replace")


def _sanitize_service_name(name: str) -> tuple[str, str]:
    service_name = str(name or "service").strip() or "service"
    if not _SERVICE_NAME_RE.fullmatch(service_name):
        return "", "⚠️ TOOL_ARG_ERROR (start_service): name must match [A-Za-z0-9_.-]{1,80}."
    return service_name, ""


def _readiness_timeout(readiness: Dict[str, Any] | None) -> tuple[float, str]:
    raw = (readiness or {}).get("timeout_sec", 5)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0, "⚠️ TOOL_ARG_ERROR (start_service): readiness.timeout_sec must be numeric."
    if value < 0:
        return 0.0, "⚠️ TOOL_ARG_ERROR (start_service): readiness.timeout_sec must be non-negative."
    return min(value, 25.0), ""


def _service_env() -> Dict[str, str]:
    allowed_exact = {
        "PATH",
        "HOME",
        "TMPDIR",
        "TMP",
        "TEMP",
        "LANG",
        "LC_ALL",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "NODE_PATH",
        "SystemRoot",
        "WINDIR",
    }
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if key not in allowed_exact and not key.startswith("LC_"):
            continue
        try:
            redacted = redact_projection(str(value))
            if redacted.records:
                continue
        except Exception:
            continue
        env[key] = str(value)
    return env


def _stop_record(record: ServiceRecord) -> None:
    if record.pgid:
        kill_process_group_id(record.pgid)
    elif record.proc.poll() is None:
        kill_process_tree(record.proc)
    try:
        record.proc.wait(timeout=5)
    except Exception:
        pass


def _refresh_ready(record: ServiceRecord) -> bool:
    if record.proc.poll() is not None:
        record.ready = False
        return False
    readiness = record.readiness or {}
    contains = str(readiness.get("stdout_contains") or readiness.get("log_contains") or "").strip()
    if not contains:
        record.ready = True
        return True
    record.ready = contains in _tail(record.log_path, 20_000)
    return record.ready


def _start_service(
    ctx: ToolContext,
    cmd: List[str],
    name: str = "service",
    cwd: str = "",
    readiness: Dict[str, Any] | None = None,
) -> str:
    if not isinstance(cmd, list) or not cmd or not all(str(x).strip() for x in cmd):
        return "⚠️ TOOL_ARG_ERROR (start_service): cmd must be a non-empty array of strings."
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    readiness_timeout, readiness_error = _readiness_timeout(readiness)
    if readiness_error:
        return readiness_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        existing = _SERVICES.get(key)
        if existing and existing.proc.poll() is None:
            return f"⚠️ SERVICE_ALREADY_RUNNING: {service_name} pid={existing.proc.pid}"
    try:
        workdir = _resolve_cwd(ctx, cwd)
    except Exception as exc:
        return f"⚠️ SERVICE_CWD_ERROR: {type(exc).__name__}: {exc}"
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    log_dir = pathlib.Path(ctx.drive_root) / "services" / task_id
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{service_name}.log"
    log_fh = log_path.open("ab")
    try:
        bootstrap_process_path()
        proc = subprocess.Popen(
            [str(part) for part in cmd],
            cwd=str(workdir),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=_service_env(),
            **subprocess_new_group_kwargs(),
        )
        pgid = process_group_id(proc.pid)
        log_fh.close()
    except Exception as exc:
        log_fh.close()
        return f"⚠️ SERVICE_START_ERROR: {type(exc).__name__}: {exc}"
    record = ServiceRecord(
        name=service_name,
        service_id=key,
        task_id=task_id,
        cmd=[str(part) for part in cmd],
        cwd=str(workdir),
        log_path=log_path,
        proc=proc,
        pgid=pgid,
        readiness=dict(readiness or {}),
    )
    with _LOCK:
        _SERVICES[key] = record
    try:
        if not bool(getattr(ctx, "is_workspace_mode", lambda: False)()):
            from ouroboros.tools.commit_gate import _invalidate_advisory

            _invalidate_advisory(
                ctx,
                changed_paths=[f"<service:{service_name}>"],
                mutation_root=workdir,
                source_tool="start_service",
            )
    except Exception:
        pass
    deadline = time.time() + readiness_timeout
    while time.time() < deadline:
        if _refresh_ready(record):
            break
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)


def _status_payload(record: ServiceRecord) -> Dict[str, Any]:
    _refresh_ready(record)
    rc = record.proc.poll()
    state = "running" if rc is None else "exited"
    return {
        "service_id": record.service_id,
        "name": record.name,
        "task_id": record.task_id,
        "pid": record.proc.pid,
        "state": state,
        "ready": bool(record.ready),
        "returncode": rc,
        "uptime_sec": round(max(0.0, time.time() - record.started_at), 3),
        "cwd": record.cwd,
        "cmd": record.cmd,
        "log_path": str(record.log_path),
        "ts": utc_now_iso(),
    }


def _service_status(ctx: ToolContext, name: str = "service") -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if not record:
        return f"⚠️ SERVICE_NOT_FOUND: {name}"
    return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)


def _service_logs(ctx: ToolContext, name: str = "service", tail: int = 8000) -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.get(key)
    if not record:
        return f"⚠️ SERVICE_NOT_FOUND: {name}"
    try:
        tail_chars = int(tail or 8000)
    except (TypeError, ValueError):
        return "⚠️ TOOL_ARG_ERROR (service_logs): tail must be an integer."
    tail_chars = min(max(1, tail_chars), _MAX_SERVICE_LOG_TAIL_CHARS)
    text = str(redact_projection(_tail(record.log_path, tail_chars)).value)
    ref = {}
    omitted_reason = ""
    try:
        size = record.log_path.stat().st_size if record.log_path.exists() else 0
        if size <= _MAX_SERVICE_LOG_BLOB_BYTES:
            full = record.log_path.read_text(encoding="utf-8", errors="replace") if record.log_path.exists() else ""
            ref = write_blob(pathlib.Path(ctx.drive_root), full, kind="txt")
        else:
            omitted_reason = f"log exceeds {_MAX_SERVICE_LOG_BLOB_BYTES} byte blob cap"
    except Exception:
        ref = {}
    return json.dumps({
        "service_id": record.service_id,
        "name": record.name,
        "tail": text,
        "full_log_ref": ref,
        "full_log_omitted": omitted_reason,
    }, ensure_ascii=False, indent=2)


def _stop_service(ctx: ToolContext, name: str = "service") -> str:
    service_name, name_error = _sanitize_service_name(name)
    if name_error:
        return name_error
    key = _service_key(ctx, service_name)
    with _LOCK:
        record = _SERVICES.pop(key, None)
    if not record:
        return f"⚠️ SERVICE_NOT_FOUND: {name}"
    _stop_record(record)
    return json.dumps(_status_payload(record), ensure_ascii=False, indent=2)


def stop_task_services(ctx: ToolContext) -> List[Dict[str, Any]]:
    task_id = str(getattr(ctx, "task_id", "") or "manual")
    stopped: List[Dict[str, Any]] = []
    with _LOCK:
        keys = [
            key for key, record in _SERVICES.items()
            if record.task_id == task_id
        ]
    for key in keys:
        name = key.split(":", 1)[1]
        try:
            payload = json.loads(_stop_service(ctx, name=name))
            stopped.append(payload)
        except Exception:
            pass
    return stopped


def kill_all_services() -> List[Dict[str, Any]]:
    """Stop every tracked service process group for panic/shutdown paths."""

    with _LOCK:
        records = list(_SERVICES.values())
        _SERVICES.clear()
    stopped: List[Dict[str, Any]] = []
    for record in records:
        _stop_record(record)
        stopped.append(_status_payload(record))
    return stopped


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("start_service", {
            "name": "start_service",
            "description": "Start a task-scoped long-running service and return pid/readiness/state.",
            "parameters": {"type": "object", "properties": {
                "cmd": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "default": ""},
                "name": {"type": "string", "default": "service"},
                "readiness": {"type": "object", "default": {}, "description": "Optional {log_contains|stdout_contains, timeout_sec} readiness probe."},
            }, "required": ["cmd"]},
        }, _start_service, is_code_tool=True, timeout_sec=30),
        ToolEntry("service_status", {
            "name": "service_status",
            "description": "Return pid/state/readiness/uptime for a task-scoped service.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
            }, "required": []},
        }, _service_status),
        ToolEntry("service_logs", {
            "name": "service_logs",
            "description": "Return bounded service log tail plus a private full-log blob ref.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
                "tail": {"type": "integer", "default": 8000},
            }, "required": []},
        }, _service_logs),
        ToolEntry("stop_service", {
            "name": "stop_service",
            "description": "Stop a task-scoped service process group.",
            "parameters": {"type": "object", "properties": {
                "name": {"type": "string", "default": "service"},
            }, "required": []},
        }, _stop_service, is_code_tool=True, timeout_sec=30),
    ]
