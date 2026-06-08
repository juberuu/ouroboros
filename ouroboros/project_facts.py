"""Thin per-project facts store (Phase 3b).

A project-scoped task (an external/workspace task, or one given an explicit
``project_id``) keeps its learned FACTS in a per-project knowledge store that is:

- under the CANONICAL data dir (``config.DATA_DIR/projects/<id>/knowledge``), NOT
  a task's child drive — so it persists across forked/empty runs;
- OUTSIDE ``memory/knowledge/**`` and any ``_copy_stable_memory`` path — so it
  never leaks into the forked seed or another project (red-team R3.1/guard #2);
- never identity — there is no per-project identity.

This is a thin SSOT helper, NOT a parallel memory subsystem (P7): the existing
knowledge tool + context loader simply redirect their base dir when a task is
project-scoped, and the post-task canonical dual-run is suppressed for such tasks
so project facts cannot contaminate the global memory.
"""
from __future__ import annotations

import hashlib
import pathlib
import re
from typing import Any, Dict

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]")
# Windows reserved device names (case-insensitive, incl. extension variants like
# "con.md"): never allow these as a project dir component.
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)


def sanitize_project_id(value: Any) -> str:
    """Return a filesystem-safe project id (alphanumeric/_/-/., <=64 chars), or ""
    if unusable (empty, or a Windows reserved device name)."""
    pid = _SAFE.sub("-", str(value or "").strip())[:64].strip("-.")
    if pid.lower().split(".", 1)[0] in _RESERVED_NAMES:
        return ""
    # Canonical lowercase: on case-insensitive filesystems (macOS/Windows) "Proj"
    # and "proj" would otherwise alias the same store and break isolation.
    return pid.casefold()


def explicit_project_id_ok(raw: Any) -> bool:
    """True if an EXPLICIT project id is already filesystem-clean (no silent
    normalization). The gateway rejects explicit ids that fail this, so two
    different inputs can never collapse to the same store and an unusable id never
    silently falls back to canonical memory."""
    s = str(raw or "")
    return bool(s) and s == s.strip() and s == sanitize_project_id(s)


def resolve_project_id(task: Dict[str, Any]) -> str:
    """Resolve a task's project id (S7): explicit ``project_id`` wins; else, for a
    workspace task, a stable hash of the workspace path; else ``""`` (not
    project-scoped — canonical memory, unchanged behavior)."""
    if not isinstance(task, dict):
        return ""
    pid = sanitize_project_id(task.get("project_id"))
    if pid:
        return pid
    # Subagents inherit the parent's scope EXPLICITLY (carried on the child task);
    # never re-derive from the child's (possibly acting) workspace, which would
    # mismatch the forked seed prepared at schedule time for an unscoped parent.
    if str(task.get("delegation_role") or "") == "subagent":
        return ""
    workspace = str(task.get("workspace_root") or "").strip()
    if workspace:
        import os

        # normcase so the same workspace under different path casing maps to ONE id
        # on case-insensitive filesystems (identity on case-sensitive Linux).
        canon = os.path.normcase(str(pathlib.Path(workspace).resolve(strict=False)))
        digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:12]
        return f"proj_{digest}"
    return ""


def project_store_access_block(rel_path: Any) -> "str | None":
    """Deny message if ``rel_path`` targets the per-project store (``projects/<id>/``),
    else None. Generic data tools use this so the store is reachable ONLY via the
    project-scoped knowledge tools (no cross-project peeking). The path is normalized
    first (collapsing ``.``/``..`` and backslashes) so traversal/`./`-prefixed forms
    cannot bypass the check."""
    import os as _os

    raw = str(rel_path or "").replace("\\", "/").strip()
    raw = re.sub(r"^[a-zA-Z]:", "", raw)  # strip a Windows drive letter before checking
    normalized = _os.path.normpath(raw).replace("\\", "/").lstrip("/")
    first = normalized.split("/", 1)[0].casefold() if normalized else ""
    if first == "projects":
        return ("⚠️ ACCESS_DENIED: the per-project facts store (projects/<id>/) is not "
                "reachable via generic data tools. Use knowledge_read / knowledge_write "
                "(automatically scoped to the current project).")
    return None


def filter_out_project_store(base_rel: Any, names: Any) -> list:
    """Drop entries that would expose the per-project store from a generic listing.
    The combined path is handed to project_store_access_block, which normalizes
    `.`/`..` itself, so a traversal base like ``logs/..`` cannot smuggle ``projects``."""
    base = str(base_rel or "").rstrip("/")
    out = []
    for n in names:
        rel = (base + "/" + str(n)) if base else str(n)
        if not project_store_access_block(rel):
            out.append(n)
    return out


def project_knowledge_dir(project_id: str) -> pathlib.Path:
    """Absolute path to a project's knowledge dir under the canonical data dir."""
    from ouroboros.config import DATA_DIR

    return pathlib.Path(DATA_DIR) / "projects" / sanitize_project_id(project_id) / "knowledge"
