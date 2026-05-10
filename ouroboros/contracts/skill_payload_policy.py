"""Shared skill-payload path resolution policy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional

from ouroboros.contracts.task_constraint import TaskConstraint, normalize_task_constraint
from ouroboros.utils import safe_relpath

SKILL_PAYLOAD_BUCKETS = frozenset({
    "external",
    "clawhub",
    "ouroboroshub",
})

SKILL_PAYLOAD_CONTROL_FILENAMES = frozenset({
    ".clawhub.json",
    ".ouroboroshub.json",
    ".self_authored.json",
    ".seed-origin",
    "skill.openclaw.md",
    ".ouroboros_env",
    "node_modules",
    "__pycache__",
})


class SkillPayloadPathError(ValueError):
    """Raised when a path cannot be confined to a skill payload."""


@dataclass(frozen=True)
class SkillPayloadTarget:
    bucket: str
    skill: str
    payload_root: Path
    target_path: Path
    rel_path: str
    control_plane: bool = False


def _clean_data_rel(raw: str) -> str:
    norm = str(raw or "").replace("\\", "/").strip().lstrip("/")
    if norm.startswith("data/"):
        norm = norm[len("data/"):]
    return norm


def _constraint_payload_root(constraint: Optional[TaskConstraint]) -> str:
    tc = normalize_task_constraint(constraint)
    if not tc or tc.mode != "skill_repair" or not tc.payload_root:
        return ""
    return _clean_data_rel(tc.payload_root)


def _sanitize_skill_name(name: str) -> str:
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name or "").strip()
    )
    cleaned = cleaned.strip("._")
    return (cleaned or "_unnamed")[:64]


def _rel_from_raw(drive: Path, raw_path: str) -> tuple[str, bool]:
    raw = str(raw_path or "").strip()
    if not raw:
        return "", False
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve(strict=False).relative_to(drive)
        except ValueError as exc:
            raise SkillPayloadPathError("absolute path is outside data root") from exc
        return rel.as_posix(), True
    return _clean_data_rel(raw), False


def resolve_skill_payload_target(
    drive_root: Path,
    path_text: str,
    *,
    constraint: Optional[TaskConstraint] = None,
    allow_short_relative: bool = False,
) -> SkillPayloadTarget:
    """Resolve *path_text* to a path confined inside one skill payload.

    Without a constraint, callers must pass an explicit data-skill path
    (``skills/<bucket>/<skill>/...`` or ``data/skills/...`` or an absolute
    path under ``drive_root``).  With a repair constraint and
    ``allow_short_relative=True``, short paths such as ``plugin.py`` resolve
    under the selected payload root.
    """

    drive = Path(drive_root).resolve(strict=False)
    rel, was_absolute = _rel_from_raw(drive, path_text)
    payload_root = _constraint_payload_root(constraint)
    if payload_root:
        root_parts = PurePosixPath(payload_root).parts
        if len(root_parts) < 3 or root_parts[0] != "skills" or root_parts[1] not in SKILL_PAYLOAD_BUCKETS:
            raise SkillPayloadPathError("repair payload root must be data/skills/<bucket>/<skill>")
        tc = normalize_task_constraint(constraint)
        if tc and tc.skill_name and root_parts[2] != _sanitize_skill_name(tc.skill_name):
            raise SkillPayloadPathError("repair payload root does not match constrained skill name")
        if rel in ("", ".", "./"):
            rel = payload_root
        elif rel.startswith("skills/"):
            if rel != payload_root and not rel.startswith(payload_root + "/"):
                raise SkillPayloadPathError("path points at a different skill payload")
        elif allow_short_relative and not was_absolute:
            rel = f"{payload_root}/{safe_relpath(rel or '.')}"
        else:
            raise SkillPayloadPathError("path must be explicit or payload-relative under the repair constraint")

    parts = PurePosixPath(rel).parts
    if len(parts) < 3 or parts[0] != "skills" or parts[1] not in SKILL_PAYLOAD_BUCKETS:
        raise SkillPayloadPathError("path must point inside data/skills/<bucket>/<skill>")
    if any(part in {"", ".", ".."} for part in parts):
        raise SkillPayloadPathError("path contains unsafe path segment")

    bucket, skill = parts[1], parts[2]
    payload = (drive / "skills" / bucket / skill).resolve(strict=False)
    suffix = PurePosixPath(*parts[3:]).as_posix() if len(parts) > 3 else "."
    target = (payload / safe_relpath(suffix)).resolve(strict=False)
    try:
        target.relative_to(payload)
    except ValueError as exc:
        raise SkillPayloadPathError("path escapes skill payload") from exc

    rel_inside = "." if suffix in ("", ".") else suffix
    rel_parts = [part.lower() for part in PurePosixPath(rel_inside).parts]
    control = any(part in SKILL_PAYLOAD_CONTROL_FILENAMES for part in rel_parts)
    return SkillPayloadTarget(
        bucket=bucket,
        skill=skill,
        payload_root=payload,
        target_path=target,
        rel_path=rel_inside,
        control_plane=control,
    )


def is_skill_payload_path(
    drive_root: Path,
    path_text: str,
    *,
    constraint: Optional[TaskConstraint] = None,
    allow_short_relative: bool = False,
    allow_control_plane: bool = False,
) -> bool:
    try:
        target = resolve_skill_payload_target(
            drive_root,
            path_text,
            constraint=constraint,
            allow_short_relative=allow_short_relative,
        )
    except SkillPayloadPathError:
        return False
    return allow_control_plane or not target.control_plane


__all__ = [
    "SKILL_PAYLOAD_BUCKETS",
    "SKILL_PAYLOAD_CONTROL_FILENAMES",
    "SkillPayloadPathError",
    "SkillPayloadTarget",
    "is_skill_payload_path",
    "resolve_skill_payload_target",
]
