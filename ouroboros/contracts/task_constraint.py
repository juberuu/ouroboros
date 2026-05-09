"""Structured per-task execution constraints."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import pathlib
from typing import Any, Mapping, Optional

from ouroboros.utils import safe_relpath


@dataclass(frozen=True)
class TaskConstraint:
    mode: str = "normal"
    skill_name: str = ""
    payload_root: str = ""
    allow_enable: bool = True
    allow_review: bool = True
    extra_allowlist: tuple[str, ...] = ()


def normalize_task_constraint(value: Any) -> Optional[TaskConstraint]:
    if isinstance(value, TaskConstraint):
        return value
    if not isinstance(value, Mapping):
        return None
    extra = value.get("extra_allowlist") or ()
    if not isinstance(extra, (list, tuple)):
        extra = ()
    return TaskConstraint(
        mode=str(value.get("mode") or "normal").strip() or "normal",
        skill_name=str(value.get("skill_name") or "").strip(),
        payload_root=str(value.get("payload_root") or "").strip().replace("\\", "/").strip("/"),
        allow_enable=bool(value.get("allow_enable", True)),
        allow_review=bool(value.get("allow_review", True)),
        extra_allowlist=tuple(str(item) for item in extra if str(item).strip()),
    )


def resolve_payload_path(drive_root: Path, constraint: TaskConstraint, path_text: str) -> Path:
    drive = Path(drive_root).resolve(strict=False)
    payload_root = safe_relpath(constraint.payload_root)
    payload_parts = pathlib.PurePosixPath(payload_root).parts
    if len(payload_parts) < 3 or payload_parts[0] != "skills" or payload_parts[1] not in {"external", "clawhub", "ouroboroshub"}:
        raise ValueError("Repair payload root must be data/skills/{external,clawhub,ouroboroshub}/<skill>")
    if constraint.skill_name and payload_parts[2] != constraint.skill_name:
        raise ValueError("Repair payload root does not match constrained skill name")
    base = (drive / payload_root).resolve(strict=False)
    raw = str(path_text or "").replace("\\", "/").strip().lstrip("/")
    if raw.startswith("data/"):
        raw = raw[len("data/"):]
    if raw.startswith("skills/") and raw != payload_root and not raw.startswith(payload_root + "/"):
        raise ValueError("Path points at a different skill payload")
    if raw == payload_root:
        raw = ""
    elif raw.startswith(payload_root + "/"):
        raw = raw[len(payload_root) + 1:]
    rel = safe_relpath(raw or ".")
    target = (base / rel).resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError("Path escapes constrained skill payload") from exc
    return target
