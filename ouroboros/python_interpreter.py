"""Surface-aware Python selection for host process tools.

Only the four public process launch surfaces opt into this resolver.  It runs
once at registry pre-dispatch so the deterministic guards and the handler see
the same argv; launchers must not rewrite the interpreter afterwards.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import Any, Dict, Mapping, Optional

from ouroboros.contracts.task_constraint import (
    TaskConstraint,
    normalize_task_constraint,
)
from ouroboros.platform_layer import project_venv_python
from ouroboros.shell_parse import normalize_check_argv
from ouroboros.tool_access import path_is_relative_to, resolve_shell_cwd
from ouroboros.utils import append_jsonl, utc_now_iso

_PYTHON_TOKENS = frozenset({"python", "python3"})
_PROCESS_TOOLS = frozenset({"run_command", "run_script", "start_service", "verify_and_record"})
# Mirrors the run-kind set of tools/verify.py (`_RUN_KINDS`); keep in sync when
# a new verify run-kind is introduced there.
_VERIFY_RUN_KINDS = frozenset({"visible_verifier", "explicit_command", "explicit_metric"})
_VERIFIED_RESOLUTIONS = frozenset(
    {
        ("reviewed_skill_environment", "isolated_skill"),
        ("executor_backend_python3", "backend_path"),
        ("project_venv", "project_venv"),
        ("agent_python", "ouroboros_agent"),
    }
)


@dataclass(frozen=True)
class PythonResolutionTrace:
    tool: str
    requested_interpreter: str
    resolved_interpreter: str
    surface: str
    environment: str
    reason: str
    fallback_reason: str = ""
    error_reason: str = ""

    @property
    def changed(self) -> bool:
        return self.requested_interpreter != self.resolved_interpreter

    @property
    def verified(self) -> bool:
        """Whether the resolver proved the selected interpreter provenance."""

        return (self.reason, self.environment) in _VERIFIED_RESOLUTIONS

    def to_event(self) -> Dict[str, Any]:
        return {**asdict(self), "changed": self.changed}


def _python_request(tool_name: str, args: Mapping[str, Any]) -> tuple[str, list[str] | None]:
    """Return the exact eligible token and normalized argv, when applicable."""

    if tool_name in {"run_command", "start_service"}:
        raw = args.get("cmd")
        if not isinstance(raw, list) or not raw:
            return "", None
        argv = [str(part) for part in raw]
        requested = str(argv[0]).strip()
        return (requested, argv) if requested in _PYTHON_TOKENS else ("", None)

    if tool_name == "run_script":
        requested = str(args.get("interpreter") or "python3").strip() or "python3"
        return (requested, None) if requested in _PYTHON_TOKENS else ("", None)

    if tool_name == "verify_and_record":
        kind = str(args.get("contract_kind") or "").strip()
        if kind not in _VERIFY_RUN_KINDS:
            return "", None
        argv = normalize_check_argv(args.get("check")) or []
        if not argv:
            return "", None
        requested = str(argv[0]).strip()
        return (requested, argv) if requested in _PYTHON_TOKENS else ("", None)

    return "", None


def _effective_cwd_text(ctx: Any, tool_name: str, args: Mapping[str, Any], runtime_mode: str) -> str:
    cwd = str(args.get("cwd") or "")
    if (
        tool_name == "run_script"
        and not cwd.strip()
        and str(runtime_mode or "").strip() == "light"
        and not bool(getattr(ctx, "is_workspace_mode", lambda: False)())
    ):
        try:
            return str(ctx.task_drive_root())
        except Exception:
            return cwd
    return cwd


def _usable_executable(path_text: str) -> str:
    """Validate an interpreter while preserving venv symlink semantics."""

    text = str(path_text or "").strip()
    if not text:
        return ""
    candidate = pathlib.Path(text).expanduser()
    if not candidate.is_absolute():
        located = shutil.which(text)
        if not located:
            return ""
        candidate = pathlib.Path(located)
    try:
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            return ""
    except OSError:
        return ""
    # Do not resolve a venv's python symlink: executing the lexical path is what
    # lets Python discover the adjacent pyvenv.cfg and preserve the environment.
    return os.path.abspath(os.fspath(candidate))


def _reviewed_skill_python(ctx: Any) -> tuple[str, str]:
    """Return a lifecycle-proven isolated Python for a skill-origin task."""

    metadata = getattr(ctx, "task_metadata", {})
    if not isinstance(metadata, dict):
        return "", ""
    skill_name = str(metadata.get("skill") or "").strip()
    if not skill_name:
        return "", ""
    try:
        from ouroboros.marketplace.isolated_deps import python_runtime_binary, read_deps_state
        from ouroboros.skill_loader import find_skill
        from ouroboros.skill_readiness import skill_readiness_for_execution

        drive_root = pathlib.Path(getattr(ctx, "drive_root"))
        loaded = find_skill(drive_root, skill_name)
        if loaded is None or not skill_readiness_for_execution(drive_root, loaded).ready:
            return "", "reviewed_skill_environment_unavailable"
        deps_state = read_deps_state(drive_root, loaded.name, loaded.skill_dir)
        if str(deps_state.get("status") or "") != "installed":
            return "", "reviewed_skill_environment_unavailable"
        candidate = python_runtime_binary(loaded.skill_dir)
        usable = _usable_executable(str(candidate or ""))
        if usable:
            return usable, ""
    except Exception:
        return "", "reviewed_skill_environment_probe_failed"
    return "", "reviewed_skill_environment_unavailable"


def _executor_covers(ctx: Any, work_dir: pathlib.Path) -> tuple[bool, str]:
    try:
        from ouroboros.workspace_executor import executor_ref_from_ctx, map_host_path

        executor = executor_ref_from_ctx(ctx)
        if executor is None:
            return False, ""
        map_host_path(executor, pathlib.Path(work_dir).resolve(strict=False))
        return True, ""
    except ValueError:
        return False, ""
    except Exception:
        return False, "executor_resolution_failed"


def _surface_for(ctx: Any, cwd_root: str, work_dir: pathlib.Path, constraint: Optional[TaskConstraint]) -> str:
    if cwd_root != "active_workspace":
        return cwd_root or "unresolved"
    if constraint and constraint.mode == "acting_subagent" and constraint.surface == "self_worktree":
        return "system_repo"
    mode = str(getattr(ctx, "workspace_mode", "") or "").strip().lower()
    if mode in {"external", "external_workspace", "genesis"}:
        return "external_workspace"
    system_repo = pathlib.Path(
        getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir", work_dir)
    ).resolve(strict=False)
    return "system_repo" if path_is_relative_to(work_dir, system_repo) else "external_workspace"


def _project_root(ctx: Any, surface: str, work_dir: pathlib.Path) -> pathlib.Path:
    if surface == "external_workspace":
        workspace_root = getattr(ctx, "workspace_root", None)
        if workspace_root:
            candidate = pathlib.Path(workspace_root).resolve(strict=False)
            if path_is_relative_to(work_dir, candidate):
                return candidate
    return work_dir


def _replace_request(
    tool_name: str,
    args: Mapping[str, Any],
    argv: list[str] | None,
    resolved: str,
) -> Dict[str, Any]:
    out = dict(args)
    if tool_name in {"run_command", "start_service"}:
        new_argv = list(argv or [])
        new_argv[0] = resolved
        out["cmd"] = new_argv
    elif tool_name == "run_script":
        out["interpreter"] = resolved
    elif tool_name == "verify_and_record":
        new_argv = list(argv or [])
        new_argv[0] = resolved
        out["check"] = new_argv
    return out


def resolve_process_python(
    ctx: Any,
    tool_name: str,
    args: Mapping[str, Any],
    *,
    runtime_mode: str,
    effective_constraint: Optional[TaskConstraint] = None,
) -> tuple[Dict[str, Any], Optional[PythonResolutionTrace]]:
    """Resolve an exact ``python``/``python3`` request for one process tool."""

    name = str(tool_name or "").strip()
    original = dict(args or {})
    if name not in _PROCESS_TOOLS:
        return original, None
    requested, argv = _python_request(name, original)
    if not requested:
        return original, None

    constraint = normalize_task_constraint(effective_constraint)
    cwd_text = _effective_cwd_text(ctx, name, original, runtime_mode)
    try:
        operation = "service" if name == "start_service" else "shell"
        work_dir, cwd_root, _allowed = resolve_shell_cwd(ctx, cwd_text, operation=operation)
        work_dir = pathlib.Path(work_dir).resolve(strict=False)
    except Exception:
        trace = PythonResolutionTrace(
            tool=name,
            requested_interpreter=requested,
            resolved_interpreter=requested,
            surface="unresolved",
            environment="target_path",
            reason="target_path_fallback",
            fallback_reason="cwd_resolution_failed",
            error_reason="cwd_resolution_failed",
        )
        return original, trace

    fallback_reason = ""
    skill_python, skill_reason = _reviewed_skill_python(ctx)
    if skill_python:
        trace = PythonResolutionTrace(
            tool=name,
            requested_interpreter=requested,
            resolved_interpreter=skill_python,
            surface="reviewed_skill",
            environment="isolated_skill",
            reason="reviewed_skill_environment",
        )
        return _replace_request(name, original, argv, skill_python), trace
    if skill_reason:
        fallback_reason = skill_reason

    executor_active, executor_error = _executor_covers(ctx, work_dir)
    if executor_active:
        trace = PythonResolutionTrace(
            tool=name,
            requested_interpreter=requested,
            resolved_interpreter="python3",
            surface="executor",
            environment="backend_path",
            reason="executor_backend_python3",
            fallback_reason=fallback_reason,
        )
        return _replace_request(name, original, argv, "python3"), trace
    if executor_error and not fallback_reason:
        fallback_reason = executor_error

    surface = _surface_for(ctx, cwd_root, work_dir, constraint)
    if surface in {"external_workspace", "user_files"}:
        project_python = project_venv_python(_project_root(ctx, surface, work_dir))
        if project_python:
            trace = PythonResolutionTrace(
                tool=name,
                requested_interpreter=requested,
                resolved_interpreter=project_python,
                surface=surface,
                environment="project_venv",
                reason="project_venv",
                fallback_reason=fallback_reason,
            )
            return _replace_request(name, original, argv, project_python), trace
        trace = PythonResolutionTrace(
            tool=name,
            requested_interpreter=requested,
            resolved_interpreter=requested,
            surface=surface,
            environment="target_path",
            reason="target_path_fallback",
            fallback_reason=fallback_reason or "project_venv_unavailable",
        )
        return original, trace

    configured_agent_python = _usable_executable(
        os.environ.get("OUROBOROS_AGENT_PYTHON", "")
    )
    agent_python = configured_agent_python or _usable_executable(sys.executable or "")
    if agent_python:
        trace = PythonResolutionTrace(
            tool=name,
            requested_interpreter=requested,
            resolved_interpreter=agent_python,
            surface=surface,
            environment="ouroboros_agent",
            reason="agent_python",
            fallback_reason=(
                fallback_reason
                or ("agent_env_unavailable_process_fallback" if not configured_agent_python else "")
            ),
        )
        return _replace_request(name, original, argv, agent_python), trace

    trace = PythonResolutionTrace(
        tool=name,
        requested_interpreter=requested,
        resolved_interpreter=requested,
        surface=surface,
        environment="target_path",
        reason="target_path_fallback",
        fallback_reason=fallback_reason or "agent_python_unavailable",
        error_reason="agent_python_unavailable",
    )
    return original, trace


def record_python_resolution(ctx: Any, trace: Optional[PythonResolutionTrace]) -> None:
    """Persist a compact, secret-free trace in the existing events log."""

    if trace is None:
        return
    try:
        event: Dict[str, Any] = {
            "ts": utc_now_iso(),
            "type": "python_interpreter_resolution",
            "task_id": str(getattr(ctx, "task_id", "") or ""),
            **trace.to_event(),
        }
        metadata = getattr(ctx, "task_metadata", {})
        if isinstance(metadata, dict):
            for key in ("root_task_id", "parent_task_id", "delegation_role"):
                value = metadata.get(key)
                if value not in (None, ""):
                    event[key] = value
        correlation = getattr(ctx, "_current_llm_call_meta", {})
        if isinstance(correlation, dict):
            for key in ("execution_id", "round_id", "llm_call_id"):
                if correlation.get(key):
                    event[key] = correlation[key]
        drive_logs = getattr(ctx, "drive_logs", None)
        if callable(drive_logs):
            log_dir = pathlib.Path(drive_logs())
        else:
            log_dir = pathlib.Path(getattr(ctx, "drive_root")) / "logs"
        append_jsonl(log_dir / "events.jsonl", event)
    except Exception:
        # Trace persistence must not make an otherwise-valid process call fail.
        return


__all__ = [
    "PythonResolutionTrace",
    "record_python_resolution",
    "resolve_process_python",
]
