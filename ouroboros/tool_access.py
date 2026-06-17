"""Tool API v2 access matrix.

This is the single policy shape for LLM-visible tools: a profile asks to run an
operation against a resource root and receives an allow/block decision. The
legacy per-tool checks still provide defense-in-depth while the public API is
migrated to neutral tool names.
"""

from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass
from typing import Any, Literal

from ouroboros.artifacts import task_artifact_dir_path, task_id_for_artifacts
from ouroboros.tool_capabilities import ACTING_SUBAGENT_MODE, LOCAL_READONLY_SUBAGENT_MODE
from ouroboros.contracts.task_constraint import VALID_WRITE_SURFACES, normalize_task_constraint
from ouroboros.contracts.skill_payload_policy import resolve_skill_payload_target
from ouroboros.shell_parse import is_absolute_path_text
from ouroboros.utils import safe_relpath


ToolProfile = Literal[
    "self_modification",
    "workspace_task",
    "external_workspace_task",
    "acting_subagent",
    "skill_repair",
    "local_readonly_subagent",
    "operator_control",
]
ResourceRoot = Literal[
    "active_workspace",
    "system_repo",
    "runtime_data",
    "task_drive",
    "skill_payload",
    "artifact_store",
    "user_files",
]
Operation = Literal[
    "read",
    "list",
    "search",
    "write",
    "edit",
    "shell",
    "vcs",
    "review",
    "delegate",
    "service",
]


@dataclass(frozen=True)
class ToolAccessDecision:
    allow: bool
    reason: str = ""
    guard: str = ""


_ALL_ROOTS: frozenset[str] = frozenset({
    "active_workspace",
    "system_repo",
    "runtime_data",
    "task_drive",
    "skill_payload",
    "artifact_store",
    "user_files",
})

_READ_OPS = frozenset({"read", "list", "search"})
_USER_FILES_SECRET_COMPONENTS = frozenset({
    ".aws",
    ".azure",
    ".config",
    ".docker",
    ".gnupg",
    ".kube",
    ".local",
    ".netrc",
    ".ssh",
    "library",
})
_USER_FILES_SECRET_NAMES = frozenset({
    ".env",
    "auth.json",
    "credentials",
    "credentials.json",
    "secrets.json",
    "settings.json",
    "token.json",
    "tokens.json",
})
_USER_FILES_SECRET_RE = re.compile(r"(?:^|[._-])(api[_-]?key|credential|password|secret|token)(?:[._-]|$)", re.I)

_POLICY: dict[str, dict[str, set[str]]] = {
    "local_readonly_subagent": {
        "active_workspace": set(_READ_OPS),
        "system_repo": set(_READ_OPS),
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list"},
        "artifact_store": {"read", "list"},
    },
    "skill_repair": {
        "skill_payload": {"read", "list", "search", "write", "edit", "review"},
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list"},
        "artifact_store": {"read", "list"},
    },
    "workspace_task": {
        "active_workspace": {"read", "list", "search", "write", "edit", "shell", "vcs", "service"},
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list", "write", "edit", "shell", "service"},
        "artifact_store": {"read", "list", "write", "shell", "service"},
    },
    # Top-level EXTERNAL-workspace task (ctx.workspace_mode == "external"). Same
    # authority as workspace_task PLUS read/list/search/shell on user_files so the
    # agent can inspect host scratch and run commands there (a repo under /tmp, a
    # /build tree, sibling checkouts). NO write/edit/vcs on user_files: structured
    # edits go through active_workspace / task_drive; this is read+inspect+run.
    # The user_files PATH guards (is_external_workspace + user_files_path_block_reason)
    # still confine it to non-runtime, non-credential paths. Kept distinct from
    # workspace_task so non-external workspace modes and self_worktree/genesis
    # acting surfaces never inherit the host-scratch reach.
    "external_workspace_task": {
        "active_workspace": {"read", "list", "search", "write", "edit", "shell", "vcs", "service"},
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list", "write", "edit", "shell", "service"},
        "artifact_store": {"read", "list", "write", "shell", "service"},
        "user_files": {"read", "list", "search", "shell"},
    },
    # Mutative (acting) subagents write only inside their isolated active
    # workspace (self_worktree / external_workspace / genesis). No vcs-commit /
    # review here; the parent integrates and commits. self_worktree additionally
    # keeps protected-path discipline active in the registry (it is the system
    # repo). runtime_data stays read-only.
    "acting_subagent": {
        # Acting children write ONLY inside their isolated surface (active_workspace =
        # the self_worktree / external_workspace / genesis). task_drive / artifact_store
        # are read-only here (no extra write surface); the deliverable is a workspace.patch.
        "active_workspace": {"read", "list", "search", "write", "edit", "shell", "vcs", "service"},
        "runtime_data": {"read", "list"},
        "task_drive": {"read", "list"},
        "artifact_store": {"read", "list"},
    },
    "self_modification": {
        "active_workspace": {"read", "list", "search", "write", "edit", "shell", "vcs", "review", "service"},
        "system_repo": {"read", "list", "search", "write", "edit", "shell", "vcs", "review", "service"},
        "runtime_data": {"read", "list", "write", "edit"},
        "task_drive": {"read", "list", "write", "edit", "shell", "service"},
        "skill_payload": {"read", "list", "search", "write", "edit", "review"},
        "artifact_store": {"read", "list", "write", "shell", "service"},
        "user_files": {"read", "list", "search", "write", "edit", "shell", "service"},
    },
    "operator_control": {root: {"read", "list", "search", "write", "edit", "shell", "vcs", "review", "delegate", "service"} for root in _ALL_ROOTS},
}


def _is_subagent_ctx(ctx: Any) -> bool:
    """True when the task is a delegated subagent (by lineage metadata)."""
    for attr in ("task_metadata", "task_contract"):
        data = getattr(ctx, attr, None)
        if isinstance(data, dict) and str(data.get("delegation_role") or "").strip() == "subagent":
            return True
    return False


def is_external_workspace(ctx: Any) -> bool:
    """True for an EXTERNAL-workspace top-level task (not the system repo).

    External-workspace tasks operate on a pre-existing working tree somewhere on
    the host (container scratch, a repo cloned under ``/tmp`` or ``/build``,
    etc.). They legitimately read, run commands, and use git OUTSIDE the user
    home, while the Ouroboros runtime (system repo + data drive) and
    credential-like files stay protected by the per-path guards. ``self_worktree``
    and ``genesis`` are acting-subagent SURFACES (``acting_subagent`` profile),
    never this profile, so they keep full home/runtime confinement.
    """
    try:
        if not bool(getattr(ctx, "is_workspace_mode", lambda: False)()):
            return False
    except Exception:
        return False
    return str(getattr(ctx, "workspace_mode", "") or "").strip().lower() == "external"


def active_tool_profile(ctx: Any) -> ToolProfile:
    constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    mode = str(getattr(constraint, "mode", "") or "").strip()
    if mode == LOCAL_READONLY_SUBAGENT_MODE:
        return "local_readonly_subagent"
    if mode == ACTING_SUBAGENT_MODE:
        # Acting subagents require a resolved write surface; otherwise fail
        # closed to read-only rather than inheriting a broader profile.
        surface = str(getattr(constraint, "surface", "") or "").strip()
        if surface in VALID_WRITE_SURFACES:
            return "acting_subagent"
        return "local_readonly_subagent"
    if mode == "skill_repair":
        return "skill_repair"
    # Fail-closed floor (BIBLE P3), checked BEFORE workspace/direct-chat: a
    # delegated subagent without a valid readonly/acting/skill constraint is
    # read-only and must never inherit workspace_task / operator_control /
    # self_modification. The parent remains the sole local writer/committer.
    if _is_subagent_ctx(ctx):
        return "local_readonly_subagent"
    if bool(getattr(ctx, "is_workspace_mode", lambda: False)()):
        # External workspaces additionally reach host scratch via user_files;
        # other workspace modes keep the tighter workspace_task envelope.
        if is_external_workspace(ctx):
            return "external_workspace_task"
        return "workspace_task"
    if bool(getattr(ctx, "is_direct_chat", False)):
        return "operator_control"
    return "self_modification"


def decide_tool_access(
    *,
    profile: ToolProfile,
    root: ResourceRoot,
    operation: Operation,
) -> ToolAccessDecision:
    allowed = operation in _POLICY.get(profile, {}).get(root, set())
    if allowed:
        return ToolAccessDecision(True, guard=f"{profile}:{root}:{operation}")
    return ToolAccessDecision(
        False,
        reason=f"profile={profile} cannot {operation} root={root}",
        guard=f"{profile}:{root}:{operation}",
    )


def normalize_root(root: str | None, *, default: ResourceRoot = "active_workspace") -> ResourceRoot:
    candidate = str(root or default).strip() or default
    if candidate not in _ALL_ROOTS:
        raise ValueError(f"unknown root {candidate!r}; expected one of {sorted(_ALL_ROOTS)}")
    return candidate  # type: ignore[return-value]


def path_is_relative_to(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        pathlib.Path(path).resolve(strict=False).relative_to(pathlib.Path(root).resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def normalize_root_relative(root: pathlib.Path, path: str) -> str:
    """Map a model-supplied path to a root-relative string when it redundantly
    encodes the root, so structural/read tools accept the paths an agent
    naturally writes: an absolute path inside the active root (e.g. ``/app/foo``
    under a workspace rooted at ``/app``) and a single redundant root-basename
    prefix (``app/foo``). Returns a RELATIVE string only — it never widens
    access: callers still apply ``safe_relpath`` + a ``relative_to`` confinement
    check, so a genuine escape is still rejected downstream.

    - absolute & inside root  -> stripped to relative
    - absolute & outside root -> returned unchanged (caller's check rejects it)
    - redundant root-basename prefix, existence-guarded -> stripped
    - otherwise -> unchanged
    """

    text = str(path or "").strip().replace("\\", "/")
    if not text or text in (".", "./"):
        return text
    try:
        root_resolved = pathlib.Path(root).resolve(strict=False)
    except (OSError, ValueError):
        return text
    # (A) absolute path that already points inside the root.
    if is_absolute_path_text(text):
        try:
            return pathlib.Path(text).resolve(strict=False).relative_to(root_resolved).as_posix()
        except (OSError, ValueError):
            return text  # outside root -> let the caller's confinement reject it
    # (B) redundant root-basename prefix ('app' or 'app/x' when root basename is
    # 'app'). Strip it UNLESS the root contains a real same-named subdir (then
    # 'app/x' is ambiguously a genuine nested path and is kept). Gating on the
    # absence of that subdir — not on the target existing — lets NEW write/create
    # targets ('app/new.py' -> 'new.py') normalize too, while a real 'app/'
    # subdir is never mis-stripped. Only ever shortens toward root (no escape).
    base = root_resolved.name
    if base and (text == base or text.startswith(base + "/")):
        try:
            if not (root_resolved / base).is_dir():
                return text[len(base):].lstrip("/") or "."
        except (ValueError, OSError):
            # `..`/traversal or stat error: leave unchanged so the caller's
            # confinement produces the canonical (not a generic) error.
            return text
    return text


def _path_is_relative_to_casefold(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path_parts = pathlib.Path(path).resolve(strict=False).parts
        root_parts = pathlib.Path(root).resolve(strict=False).parts
    except (OSError, ValueError):
        return False
    if len(path_parts) < len(root_parts):
        return False
    return tuple(part.casefold() for part in path_parts[: len(root_parts)]) == tuple(
        part.casefold() for part in root_parts
    )


def paths_overlap_casefold(left: pathlib.Path, right: pathlib.Path) -> bool:
    """Return True when two paths overlap under case-insensitive path semantics."""

    return _path_is_relative_to_casefold(left, right) or _path_is_relative_to_casefold(right, left)


def light_cognitive_or_root_redirect(tool_name: str, args: dict[str, Any]) -> str | None:
    """Precise light-mode redirect for write attempts that should use a cognitive
    tool or an explicit ``user_files`` root. Returns the message, or ``None``.

    Only ``write_file``/``edit_text`` qualify. Callers invoke this inside the
    light-mode repo-mutation block so a returned message replaces the generic
    LIGHT_MODE_BLOCKED with actionable, non-noisy guidance.
    """
    if tool_name not in ("write_file", "edit_text"):
        return None
    paths: list[str] = []
    primary = str(args.get("path", "") or "")
    if primary:
        paths.append(primary)
    for entry in args.get("files") or []:
        if isinstance(entry, dict) and entry.get("path"):
            paths.append(str(entry.get("path")))
    raw_root = str(args.get("root", "") or "active_workspace")
    try:
        root = normalize_root(raw_root)
    except Exception:
        root = "active_workspace"

    if root == "runtime_data":
        for path_text in paths:
            # Logical resource-path components. Normalize Windows separators to the
            # POSIX convention these tool paths use, then compare parts (not raw
            # separators), so both memory/identity.md and memory\identity.md match.
            parts = pathlib.PurePosixPath(str(path_text or "").replace("\\", "/")).parts
            if len(parts) >= 2 and parts[0].lower() == "memory":
                area = parts[1].lower()
                if area.startswith("identity") or area.startswith("scratchpad") or area == "knowledge":
                    return (
                        "⚠️ COGNITIVE_TOOL_REQUIRED: cognitive memory is not written via "
                        f"{tool_name!r}. Use the dedicated first-class tools (always available in "
                        "light mode): update_identity for memory/identity.md, update_scratchpad for "
                        "memory/scratchpad.md, knowledge_write for memory/knowledge/<topic>.md. They "
                        "apply the correct structure (journaling, timestamped blocks, index "
                        "maintenance). Read the current state before writing (Bible P12)."
                    )

    if root == "active_workspace":
        for path_text in paths:
            # Use pathlib semantics (no hardcoded separators): an expanded path
            # that is absolute and under the owner home should use root=user_files.
            # This is cross-platform (POSIX `/`, `~`, and Windows drive paths).
            try:
                candidate = pathlib.Path(path_text).expanduser()
                if not candidate.is_absolute():
                    continue
                candidate.resolve(strict=False).relative_to(pathlib.Path.home().resolve(strict=False))
            except (ValueError, OSError, RuntimeError):
                continue
            return (
                "⚠️ ROOT_REQUIRED_USER_FILES: an absolute home path "
                f"({path_text!r}) was given but root defaulted to 'active_workspace'. "
                "Pass root='user_files' to write under the owner's home, e.g. "
                "write_file(root='user_files', path='Desktop/file.html', content=...)."
            )
    return None


def workspace_mode_block_reason(ctx: Any) -> str:
    mode = str(getattr(ctx, "workspace_mode", "") or "").strip()
    workspace_root = getattr(ctx, "workspace_root", None)
    if not mode or workspace_root is None:
        return ""
    try:
        workspace = pathlib.Path(workspace_root).resolve(strict=False)
    except (OSError, TypeError, ValueError):
        return "workspace_root is invalid"
    protected_values = (
        ("Ouroboros system repo", getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir", None)),
        ("Ouroboros repo", getattr(ctx, "repo_dir", None)),
        ("Ouroboros data drive", getattr(ctx, "drive_root", None)),
        (
            "Ouroboros parent data drive",
            (getattr(ctx, "task_metadata", {}) or {}).get("budget_drive_root")
            if isinstance(getattr(ctx, "task_metadata", {}), dict)
            else "",
        ),
    )
    for label, value in protected_values:
        if not value:
            continue
        try:
            protected = pathlib.Path(value).resolve(strict=False)
        except (OSError, TypeError, ValueError):
            continue
        if (
            path_is_relative_to(workspace, protected)
            or path_is_relative_to(protected, workspace)
            or paths_overlap_casefold(workspace, protected)
        ):
            return f"workspace_root overlaps the {label}"
    return ""


def user_files_path_block_reason(
    ctx: Any,
    candidate: pathlib.Path,
    *,
    allow_protected_descendants: bool = False,
) -> str:
    """Return a block reason when candidate is not an external user file."""

    resolved = pathlib.Path(candidate).expanduser().resolve(strict=False)
    home = pathlib.Path.home().resolve(strict=False)
    outside_home = not path_is_relative_to(resolved, home) and not _path_is_relative_to_casefold(resolved, home)
    # External-workspace tasks may reach host scratch outside home (/tmp, /build,
    # sibling checkouts). The runtime-overlap and credential guards BELOW still
    # run on the full path, so the Ouroboros repo/data drive and secret-like
    # files stay protected even when home confinement is lifted.
    if outside_home and not is_external_workspace(ctx):
        return f"path is outside user home {home}"

    # The Ouroboros runtime/control surface is the system repo PLUS every data
    # drive the task touches: the parent drive (ctx.drive_root) and any child /
    # budget drive carried in task_metadata. External-workspace mode lifts home
    # confinement, so these must be enumerated explicitly here — otherwise a
    # child-drive control path (e.g. <child_drive>/memory) would slip through.
    protected_values: list[Any] = [
        getattr(ctx, "drive_root", None),
        getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir", None),
    ]
    meta = getattr(ctx, "task_metadata", {})
    if isinstance(meta, dict):
        for key in ("drive_root", "child_drive_root", "headless_child_drive_root", "budget_drive_root"):
            if meta.get(key):
                protected_values.append(meta.get(key))
    protected_roots: list[pathlib.Path] = []
    for value in protected_values:
        try:
            root = pathlib.Path(value).resolve(strict=False)
        except (OSError, TypeError, ValueError):
            continue
        protected_roots.append(root)
        parent = root.parent.resolve(strict=False)
        if root.name in {"repo", "data"} and path_is_relative_to(parent, home):
            protected_roots.append(parent)
    for protected in protected_roots:
        overlaps_protected = path_is_relative_to(resolved, protected) or _path_is_relative_to_casefold(resolved, protected)
        contains_protected = path_is_relative_to(protected, resolved) or _path_is_relative_to_casefold(protected, resolved)
        if overlaps_protected or (
            not allow_protected_descendants and contains_protected
        ):
            return (
                "path overlaps the Ouroboros repo/runtime workspace; use "
                "root=active_workspace, root=task_drive, root=artifact_store, "
                "or root=skill_payload instead"
            )

    try:
        parts = resolved.relative_to(home).parts
    except ValueError:
        parts = resolved.parts
    for part in parts:
        if not part:
            continue
        part_lower = part.lower()
        if part.startswith(".") or part_lower in _USER_FILES_SECRET_COMPONENTS:
            return "path is hidden or credential-like"
    name = resolved.name
    name_lower = name.lower()
    if (
        name_lower in _USER_FILES_SECRET_NAMES
        or _USER_FILES_SECRET_RE.search(name)
        or name_lower.endswith((".key", ".pem", ".p12", ".pfx"))
    ):
        return "path name is credential-like"

    return ""


def resolve_user_file_path(
    ctx: Any,
    path: str,
    *,
    allow_protected_descendants: bool = False,
) -> pathlib.Path:
    """Resolve a user_files path under the user's home and outside Ouroboros control-plane roots."""

    raw_text = str(path or ".").strip() or "."
    raw = pathlib.Path(raw_text).expanduser()
    home = pathlib.Path.home().resolve(strict=False)
    # is_absolute_path_text gives consistent cross-platform absolute detection
    # (drive-less "/x" roots and "C:\\x"/"\\\\unc" are all absolute) so Windows
    # does not silently treat a rooted path as home-relative.
    if is_absolute_path_text(raw_text):
        candidate = raw.resolve(strict=False)
    elif raw_text.startswith("~"):
        candidate = raw.resolve(strict=False)
    else:
        candidate = (home / safe_relpath(raw_text)).resolve(strict=False)
    reason = user_files_path_block_reason(
        ctx,
        candidate,
        allow_protected_descendants=allow_protected_descendants,
    )
    if reason:
        raise ValueError(f"user_files path blocked: {reason}")
    return candidate


def resolve_shell_cwd(ctx: Any, cwd: str = "", *, operation: Operation = "shell") -> tuple[pathlib.Path, str, list[tuple[str, pathlib.Path]]]:
    """Resolve process cwd using Tool API roots instead of repo-only assumptions."""

    def ensure_process_cwd(label: str, candidate: pathlib.Path) -> pathlib.Path:
        if label in {"task_drive", "artifact_store"}:
            try:
                candidate.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise ValueError(f"could not create {label} cwd {candidate}: {exc}") from exc
        return candidate

    profile = active_tool_profile(ctx)
    candidates: list[tuple[ResourceRoot, pathlib.Path]] = [("active_workspace", resource_root_path(ctx, "active_workspace"))]
    if hasattr(ctx, "drive_root"):
        candidates.extend([
            ("task_drive", resource_root_path(ctx, "task_drive")),
            ("artifact_store", resource_root_path(ctx, "artifact_store")),
        ])
        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        for key in ("drive_root", "child_drive_root", "headless_child_drive_root"):
            if meta.get(key):
                meta_drive = pathlib.Path(meta[key]).resolve(strict=False)
                task_id = task_id_for_artifacts(ctx)
                candidates.extend([
                    ("task_drive", (meta_drive / "task_drives" / task_id).resolve(strict=False)),
                    ("artifact_store", task_artifact_dir_path(meta_drive, task_id, create=False).resolve(strict=False)),
                ])
    workspace_mode = bool(getattr(ctx, "is_workspace_mode", lambda: False)())
    if not workspace_mode and hasattr(ctx, "drive_root"):
        candidates.append(("user_files", resource_root_path(ctx, "user_files")))
    allowed: list[tuple[str, pathlib.Path]] = [
        (label, root)
        for label, root in candidates
        if decide_tool_access(profile=profile, root=label, operation=operation).allow
    ]
    if not allowed:
        raise ValueError(f"profile={profile} cannot {operation} any process cwd root")

    text = str(cwd or "").strip()
    if not text or text in {".", "./"}:
        return ensure_process_cwd(allowed[0][0], allowed[0][1]), allowed[0][0], allowed

    raw = pathlib.Path(text).expanduser()
    candidates: list[pathlib.Path] = []
    if is_absolute_path_text(text) or text.startswith("~"):
        candidates.append(raw.resolve(strict=False))
    else:
        candidates.extend((root / safe_relpath(text)).resolve(strict=False) for _, root in allowed)

    for candidate in candidates:
        for label, root in allowed:
            if not path_is_relative_to(candidate, root):
                continue
            if label == "user_files":
                reason = user_files_path_block_reason(ctx, candidate)
                if reason:
                    continue
            return ensure_process_cwd(label, candidate), label, allowed

    # External-workspace tasks may run commands FROM host scratch (a repo under
    # /tmp, a /build tree, a sibling checkout). Accept an absolute cwd that clears
    # the user_files PATH guard (non-runtime, non-credential), scoped to THAT
    # exact path — never the filesystem root — so the workspace write-guard
    # allowlist (which reuses this returned root list) is not widened beyond the
    # chosen working directory.
    if is_external_workspace(ctx) and decide_tool_access(
        profile=profile, root="user_files", operation=operation
    ).allow:
        for candidate in candidates:
            if not candidate.is_absolute():
                continue
            if user_files_path_block_reason(ctx, candidate):
                continue
            scoped_allowed = [*allowed, ("user_files", candidate)]
            return ensure_process_cwd("user_files", candidate), "user_files", scoped_allowed

    raise ValueError("cwd is outside allowed roots")


def resource_root_path(
    ctx: Any,
    root: ResourceRoot,
    *,
    bucket: str = "",
    skill_name: str = "",
) -> pathlib.Path:
    if root == "active_workspace":
        active = getattr(ctx, "active_repo_dir", None)
        candidate = None
        if callable(active):
            try:
                candidate = active()
            except Exception:
                candidate = None
        if candidate is None or candidate.__class__.__module__.startswith("unittest.mock"):
            candidate = getattr(ctx, "repo_dir")
        return pathlib.Path(candidate).resolve(strict=False)
    if root == "system_repo":
        return pathlib.Path(getattr(ctx, "system_repo_dir", None) or getattr(ctx, "repo_dir")).resolve(strict=False)
    if root == "runtime_data":
        return pathlib.Path(getattr(ctx, "drive_root")).resolve(strict=False)
    if root == "task_drive":
        return (pathlib.Path(getattr(ctx, "drive_root")).resolve(strict=False) / "task_drives" / task_id_for_artifacts(ctx)).resolve(strict=False)
    if root == "artifact_store":
        return task_artifact_dir_path(pathlib.Path(getattr(ctx, "drive_root")), task_id_for_artifacts(ctx), create=False).resolve(strict=False)
    if root == "user_files":
        return pathlib.Path.home().resolve(strict=False)
    if root == "skill_payload":
        b = str(bucket or "").strip()
        s = str(skill_name or "").strip()
        if not b or not s:
            raise ValueError("root=skill_payload requires bucket and skill_name")
        target = resolve_skill_payload_target(
            pathlib.Path(getattr(ctx, "drive_root")),
            f"skills/{b}/{s}",
        )
        return target.payload_root
    raise ValueError(f"unknown root {root!r}")


def resolve_resource_path(
    ctx: Any,
    *,
    root: ResourceRoot,
    path: str,
    bucket: str = "",
    skill_name: str = "",
) -> pathlib.Path:
    if root == "user_files":
        return resolve_user_file_path(ctx, path)
    base = resource_root_path(ctx, root, bucket=bucket, skill_name=skill_name)
    resolved_base = pathlib.Path(base).resolve(strict=False)
    # Redundant-root-basename / absolute-inside-root normalization is applied ONLY
    # for the repo roots, where the dispatch boundary (registry) already normalizes
    # args['path'] so guard and operation share the SAME target. Non-repo roots
    # (runtime_data, deliverables, skill_payload, ...) resolve the RAW path in their
    # own handlers (e.g. _data_read via _normalize_data_read_path, which strips only
    # the full drive-root prefix, NOT a bare basename), so normalizing here would
    # desync the guard from the operation — keep those raw (matches the approved T2
    # dispatch-only scope).
    if root in ("active_workspace", "system_repo"):
        path = normalize_root_relative(resolved_base, path)
    resolved = (resolved_base / safe_relpath(path or ".")).resolve(strict=False)
    try:
        resolved.relative_to(resolved_base)
    except ValueError as exc:
        raise ValueError(f"path escapes {resolved_base}") from exc
    return resolved
