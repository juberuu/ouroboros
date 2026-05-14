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

SKILL_PAYLOAD_ALL_BUCKETS = frozenset({
    "native",
    *SKILL_PAYLOAD_BUCKETS,
})

SKILL_PAYLOAD_CONTROL_FILENAMES = frozenset({
    ".clawhub.json",
    ".ouroboroshub.json",
    ".self_authored.json",
    ".seed-origin",
    "skill.openclaw.md",
})

SKILL_PAYLOAD_CONTROL_DIRNAMES = frozenset({
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


@dataclass(frozen=True)
class PayloadShortFormDecision:
    """Resolution decision for optional ``bucket`` + ``skill_name`` edit args."""

    constraint: Optional[TaskConstraint] = None
    error: str = ""
    ignored_reason: str = ""


_OPTIONAL_ARG_SENTINELS = frozenset({
    "__omit__",
    "<omit>",
    "__none__",
    "<none>",
    "null",
    "none",
    "undefined",
})

_DATA_ROOT_PREFIXES = frozenset({
    "archive",
    "logs",
    "memory",
    "skills",
    "state",
    "task_results",
    "uploads",
})

_DATA_ROOT_FILENAMES = frozenset({
    "settings.json",
})


def _clean_optional_short_form_arg(value: str) -> str:
    text = str(value or "").strip()
    return "" if text.lower() in _OPTIONAL_ARG_SENTINELS else text


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
    control = any(
        part in SKILL_PAYLOAD_CONTROL_FILENAMES or part in SKILL_PAYLOAD_CONTROL_DIRNAMES
        for part in rel_parts
    )
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


def synthesize_payload_constraint(
    bucket: str,
    skill_name: str,
) -> Optional[TaskConstraint]:
    """Synthesize a ``skill_repair``-flavoured ``TaskConstraint`` for tool
    handlers that received explicit ``bucket`` + ``skill_name`` args under
    ``runtime_mode=light``.

    Returns ``None`` for partial / invalid input (only one of the two
    supplied, ``native`` bucket, or a sanitized name that collapses to the
    placeholder ``_unnamed``). Callers MUST treat ``None`` as "no short-form
    payload context; fall back to the regular path-resolution flow" — they
    must not silently route a short relative path into the drive root.

    Reuses the existing ``skill_repair`` mode: no new mode is introduced.
    The semantic match is sufficient — both repair and light-mode short-form
    authoring confine the call to a single skill payload root.
    """
    b = _clean_optional_short_form_arg(bucket)
    raw_skill_name = _clean_optional_short_form_arg(skill_name)
    s = _sanitize_skill_name(raw_skill_name)
    if not b or not s or s == "_unnamed":
        return None
    if b not in SKILL_PAYLOAD_BUCKETS:
        return None
    return TaskConstraint(
        mode="skill_repair",
        skill_name=s,
        payload_root=f"skills/{b}/{s}",
    )


def _explicit_path_kind(path_text: str, *, repo_dir: Path, drive_root: Path) -> str:
    raw = str(path_text or "").replace("\\", "/").strip()
    if raw in ("", ".", "./"):
        return ""
    drive = Path(drive_root).resolve(strict=False)
    repo = Path(repo_dir).resolve(strict=False)
    candidate = Path(raw)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(repo)
            return "repo"
        except ValueError:
            pass
        try:
            rel = resolved.relative_to(drive).as_posix()
        except ValueError:
            return ""
        return "skill" if rel.startswith("skills/") else "data"
    raw_lstripped = raw.lstrip("/")
    raw_lstripped_lower = raw_lstripped.lower()
    if raw_lstripped_lower.startswith("data/"):
        data_rel = raw_lstripped[len("data/"):]
        return "skill" if data_rel.lower().startswith("skills/") else "data"
    rel = _clean_data_rel(raw)
    rel_lower = rel.lower()
    if rel_lower.startswith("skills/"):
        return "skill"
    parts = PurePosixPath(rel).parts
    if not parts:
        return ""
    first_lower = parts[0].lower()
    if first_lower in _DATA_ROOT_PREFIXES or first_lower in _DATA_ROOT_FILENAMES:
        return "data"
    if (repo / parts[0]).exists():
        return "repo"
    return ""


def decide_payload_short_form(
    *,
    bucket: str,
    skill_name: str,
    path_text: str,
    repo_dir: Path,
    drive_root: Path,
) -> PayloadShortFormDecision:
    """Resolve optional skill short-form args without overriding explicit paths."""
    clean_bucket = _clean_optional_short_form_arg(bucket)
    clean_skill_name = _clean_optional_short_form_arg(skill_name)
    if not clean_bucket and not clean_skill_name:
        return PayloadShortFormDecision()
    kind = _explicit_path_kind(path_text, repo_dir=repo_dir, drive_root=drive_root)
    if kind:
        return PayloadShortFormDecision(
            ignored_reason=(
                f"ignored bucket/skill_name because {path_text!r} is an explicit "
                f"{kind} path"
            )
        )
    synth = synthesize_payload_constraint(clean_bucket, clean_skill_name)
    if synth is None:
        return PayloadShortFormDecision(
            error=(
                "bucket and skill_name must be supplied together; bucket must be "
                "one of external/clawhub/ouroboroshub (native excluded); "
                "skill_name must sanitize to a non-empty slug."
            )
        )
    payload_root = (Path(drive_root) / synth.payload_root).resolve(strict=False)
    if not payload_root.is_dir():
        return PayloadShortFormDecision(
            error=(
                f"skill payload not found: {synth.payload_root}. "
                "Use an existing skill_name, or omit bucket/skill_name for a repo/data edit."
            )
        )
    return PayloadShortFormDecision(constraint=synth)


def cross_skill_redirect_error(
    existing_tc: Optional[TaskConstraint],
    synth_tc: Optional[TaskConstraint],
) -> str:
    """Return a non-empty error message when ``synth_tc`` (built from
    bucket+skill_name args) would redirect the call to a different skill than
    the one ``existing_tc`` confines the current task to.

    The repair-mode confinement gate validates ``path``/``cwd`` against the
    real ``existing_tc`` *but does not know about the new bucket+skill_name
    args*. Without this guard a heal task constrained to skill A could pass
    ``bucket=external, skill_name=B`` to a payload-writing tool and have the
    handler-side synth take precedence, silently writing into B's payload.

    Returns ``""`` (falsy) when there is no conflict — either no existing
    skill_repair task, or no synth, or both target the same skill.
    """
    if not (existing_tc and synth_tc):
        return ""
    if existing_tc.mode != "skill_repair":
        return ""
    if existing_tc.skill_name == synth_tc.skill_name:
        return ""
    return (
        f"a skill_repair task is active for {existing_tc.skill_name!r}; "
        f"cannot use bucket+skill_name args to redirect this call to "
        f"{synth_tc.skill_name!r}. Drop the bucket/skill_name args, or "
        f"finish/cancel the active repair task first."
    )


__all__ = [
    "SKILL_PAYLOAD_BUCKETS",
    "SKILL_PAYLOAD_ALL_BUCKETS",
    "SKILL_PAYLOAD_CONTROL_FILENAMES",
    "SKILL_PAYLOAD_CONTROL_DIRNAMES",
    "SkillPayloadPathError",
    "SkillPayloadTarget",
    "PayloadShortFormDecision",
    "decide_payload_short_form",
    "is_skill_payload_path",
    "resolve_skill_payload_target",
    "synthesize_payload_constraint",
    "cross_skill_redirect_error",
]
