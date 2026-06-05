"""Task-contract protected artifact enforcement helpers."""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Iterable, List

from ouroboros.shell_parse import (
    shell_argv,
    shell_command_string,
    strip_leading_env_assignments,
    unwrap_env_argv,
)
from ouroboros.tool_access import resolve_shell_cwd

_DEFAULT_DENIED_OPERATIONS = frozenset({
    "read_bytes",
    "copy",
    "hash",
    "static_introspection",
    "dynamic_trace",
    "debug",
})
_SHELLS = frozenset({"bash", "sh", "zsh"})
_HIGH_RISK_INTERPRETERS = frozenset({
    "bash", "sh", "zsh", "python", "python3", "node", "ruby", "perl", "php",
})
_SHELL_COMMAND_OPERATIONS = {
    "cat": "read_bytes",
    "head": "read_bytes",
    "tail": "read_bytes",
    "less": "read_bytes",
    "more": "read_bytes",
    "file": "static_introspection",
    "strings": "static_introspection",
    "hexdump": "static_introspection",
    "xxd": "static_introspection",
    "objdump": "static_introspection",
    "readelf": "static_introspection",
    "nm": "static_introspection",
    "otool": "static_introspection",
    "cp": "copy",
    "dd": "copy",
    "sha256sum": "hash",
    "shasum": "hash",
    "md5sum": "hash",
    "strace": "dynamic_trace",
    "ltrace": "dynamic_trace",
    "dtruss": "dynamic_trace",
    "gdb": "debug",
    "lldb": "debug",
}


def _task_contract(ctx: Any) -> Dict[str, Any]:
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    contract = metadata.get("task_contract") if isinstance(metadata.get("task_contract"), dict) else {}
    if not contract and isinstance(getattr(ctx, "task_contract", None), dict):
        contract = getattr(ctx, "task_contract")
    return dict(contract) if isinstance(contract, dict) else {}


def _artifact_records(ctx: Any) -> List[Dict[str, Any]]:
    policy = _task_contract(ctx).get("resource_policy")
    if not isinstance(policy, dict):
        return []
    records = policy.get("protected_artifacts")
    return [dict(item) for item in records if isinstance(item, dict)] if isinstance(records, list) else []


def _base_roots(ctx: Any) -> List[pathlib.Path]:
    roots: List[pathlib.Path] = []
    for value in (
        getattr(ctx, "workspace_root", None),
        getattr(ctx, "repo_dir", None),
        getattr(ctx, "system_repo_dir", None),
        getattr(ctx, "drive_root", None),
    ):
        if value is None:
            continue
        try:
            path = pathlib.Path(value).expanduser().resolve(strict=False)
        except (OSError, TypeError, ValueError):
            continue
        if path not in roots:
            roots.append(path)
    return roots


def _resolve_policy_path(ctx: Any, raw_path: str) -> pathlib.Path | None:
    text = str(raw_path or "").strip()
    if not text:
        return None
    try:
        path = pathlib.Path(text).expanduser()
    except (OSError, TypeError, ValueError):
        return None
    if path.is_absolute():
        return path.resolve(strict=False)
    roots = _base_roots(ctx)
    if not roots:
        return path.resolve(strict=False)
    return (roots[0] / path).resolve(strict=False)


def protected_artifact_paths(ctx: Any) -> List[pathlib.Path]:
    paths: List[pathlib.Path] = []
    for record in _artifact_records(ctx):
        for raw_path in record.get("paths") or []:
            resolved = _resolve_policy_path(ctx, str(raw_path))
            if resolved is not None and resolved not in paths:
                paths.append(resolved)
    return paths


def _operation_denied(record: Dict[str, Any], operation: str) -> bool:
    allow = {str(item).strip() for item in (record.get("allow") or []) if str(item).strip()}
    if operation in allow:
        return False
    deny = {str(item).strip() for item in (record.get("deny") or []) if str(item).strip()}
    if deny:
        return operation in deny
    return str(record.get("role") or "") == "black_box_reference" and operation in _DEFAULT_DENIED_OPERATIONS


def _matches(candidate: pathlib.Path, protected_path: pathlib.Path) -> bool:
    try:
        candidate_resolved = pathlib.Path(candidate).expanduser().resolve(strict=False)
        protected_resolved = pathlib.Path(protected_path).expanduser().resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return False
    if candidate_resolved == protected_resolved:
        return True
    if protected_resolved.is_dir():
        try:
            candidate_resolved.relative_to(protected_resolved)
            return True
        except ValueError:
            return False
    return False


def block_reason_for_path(ctx: Any, target: pathlib.Path, operation: str) -> str:
    for record in _artifact_records(ctx):
        if not _operation_denied(record, operation):
            continue
        for raw_path in record.get("paths") or []:
            protected_path = _resolve_policy_path(ctx, str(raw_path))
            if protected_path is not None and _matches(pathlib.Path(target), protected_path):
                artifact_id = str(record.get("id") or pathlib.Path(str(raw_path)).name or "protected artifact")
                return (
                    "⚠️ RESOURCE_POLICY_BLOCKED: task_contract.resource_policy protects "
                    f"{artifact_id!r}; operation {operation!r} is not allowed for this black-box artifact."
                )
    return ""


def any_protected_target(ctx: Any, candidates: Iterable[pathlib.Path], operation: str) -> str:
    for candidate in candidates:
        reason = block_reason_for_path(ctx, pathlib.Path(candidate), operation)
        if reason:
            return reason
    return ""


def shell_block_reason(ctx: Any, raw_cmd: Any, *, cwd: str = "", default_cwd: pathlib.Path | None = None) -> str:
    protected_paths = protected_artifact_paths(ctx)
    if not protected_paths:
        return ""
    raw_argv = shell_argv(raw_cmd)
    env_values = [
        token.split("=", 1)[1]
        for token in raw_argv
        if "=" in token and not token.startswith("=") and token.split("=", 1)[1]
    ]
    argv = strip_leading_env_assignments(unwrap_env_argv(raw_argv))
    if not argv:
        return ""
    first = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")
    if first in _SHELLS:
        inline = shell_command_string(argv)
        if inline:
            return shell_block_reason(ctx, inline, cwd=cwd, default_cwd=default_cwd)
    operation = _SHELL_COMMAND_OPERATIONS.get(first)
    high_risk = first in _HIGH_RISK_INTERPRETERS
    try:
        work_dir, _cwd_root, _allowed = resolve_shell_cwd(ctx, cwd)
    except Exception:
        work_dir = pathlib.Path(default_cwd or ".").resolve(strict=False)
    try:
        first_path = pathlib.Path(str(argv[0] or "")).expanduser()
        first_target = first_path.resolve(strict=False) if first_path.is_absolute() else (pathlib.Path(work_dir) / first_path).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        first_target = None
    if first_target is not None:
        for protected in protected_paths:
            if first_target == pathlib.Path(protected).resolve(strict=False):
                return block_reason_for_path(ctx, first_target, "execute")
    candidates: list[pathlib.Path] = []
    for raw in [*env_values, *argv[1:]]:
        text = str(raw or "")
        if not text or text in {"|", "&&", "||", ";"}:
            continue
        if first == "dd" and text.startswith("if="):
            text = text.split("=", 1)[1]
        elif first == "dd" and "=" in text:
            continue
        if text.startswith("-") and not pathlib.Path(text).is_absolute():
            continue
        try:
            path = pathlib.Path(text).expanduser()
            candidates.append(path.resolve(strict=False) if path.is_absolute() else (pathlib.Path(work_dir) / path).resolve(strict=False))
        except (OSError, ValueError):
            continue
    if operation:
        return any_protected_target(ctx, candidates, operation)
    if not high_risk:
        return ""
    default_block = any_protected_target(ctx, candidates, "read_bytes")
    if default_block:
        return default_block
    tail_text = " ".join(str(part or "") for part in [*env_values, *argv[1:]])
    for protected in protected_paths:
        protected = pathlib.Path(protected).resolve(strict=False)
        needles = {str(protected), protected.as_posix()}
        try:
            rel = protected.relative_to(pathlib.Path(work_dir).resolve(strict=False))
            if str(rel) not in {"", "."}:
                needles.add(rel.as_posix())
                needles.add(str(rel))
        except Exception:
            pass
        if any(needle and needle in tail_text for needle in needles):
            return block_reason_for_path(ctx, protected, "read_bytes")
        parent = protected.parent.as_posix()
        name = protected.name
        stem = protected.stem
        suffix = protected.suffix
        if parent and parent in tail_text and (name in tail_text or (stem and suffix and stem in tail_text and suffix in tail_text)):
            return block_reason_for_path(ctx, protected, "read_bytes")
    return ""
