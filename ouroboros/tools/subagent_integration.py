"""``integrate_subagent_patch``: the parent's manifest-first integration tool.

A mutative (acting) subagent returns its changes as a ``workspace.patch`` artifact
(produced by headless finalization, a git diff against the child's base commit).
The parent decides what to do with it — accept one (best-of-N), synthesize several,
or reject — and this tool APPLIES the chosen patch into the parent's active repo or
worktree. The parent stays the sole committer: this stages changes but never
commits; the parent reviews and runs ``commit_reviewed`` itself.

Routing is top-only: ``target_root`` defaults to ``ctx.active_repo_dir()`` — the
live repo for the root agent, or the parent's own worktree for a nested acting
parent, so descendants bubble their patches up one level at a time.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
from typing import Any, Dict, List, Tuple, Union

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.artifacts import task_artifact_dir_path, task_id_for_artifacts
from ouroboros.task_results import load_task_result
from ouroboros.review_state import invalidate_advisory_after_mutation
from ouroboros.runtime_mode_policy import (
    mode_allows_protected_write,
    protected_paths_in,
    protected_write_block_message,
)
from ouroboros.contracts.task_constraint import normalize_task_constraint
from ouroboros.tool_capabilities import ACTING_SUBAGENT_MODE
from ouroboros.config import get_runtime_mode
from ouroboros.headless import ARTIFACT_STATUS_READY_WITH_CHANGES
from ouroboros.utils import atomic_write_json, utc_now_iso


def _candidate_drive_roots(ctx: ToolContext) -> List[pathlib.Path]:
    roots: List[pathlib.Path] = []
    seen = set()
    meta = getattr(ctx, "task_metadata", {})
    meta_budget = meta.get("budget_drive_root") if isinstance(meta, dict) else ""
    for raw in (
        getattr(ctx, "drive_root", None),
        getattr(ctx, "budget_drive_root", None),
        meta_budget,
    ):
        if not raw:
            continue
        key = str(raw)
        if key in seen:
            continue
        seen.add(key)
        roots.append(pathlib.Path(raw))
    return roots


def _locate_child_patch(
    ctx: ToolContext, child_task_id: str
) -> Union[str, Tuple[pathlib.Path, Dict[str, Any], Dict[str, Any]]]:
    roots = _candidate_drive_roots(ctx)
    for root in roots:
        try:
            art_dir = task_artifact_dir_path(root, child_task_id)
        except Exception:
            continue
        manifest_path = art_dir / "workspace_patch.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            return f"⚠️ INTEGRATE_MANIFEST_UNREADABLE: {manifest_path}: {type(exc).__name__}: {exc}."
        if not isinstance(manifest, dict):
            continue
        result = load_task_result(root, child_task_id) or {}
        return art_dir / "workspace.patch", manifest, result
    listed = ", ".join(str(r) for r in roots) or "(no drive roots resolved)"
    return (
        f"⚠️ INTEGRATE_PATCH_NOT_FOUND: no workspace_patch.json for child {child_task_id!r} under {listed}. "
        "Ensure the child finished and was a mutative subagent that returned a workspace patch "
        "(retrieve it with get_task_result/wait_task first)."
    )


def _sha256_file(path: pathlib.Path) -> str:
    from hashlib import sha256

    hasher = sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _write_verdict(
    ctx: ToolContext,
    child_task_id: str,
    *,
    outcome: str,
    reason: str,
    files: List[str],
    manifest: Dict[str, Any],
    applied: bool,
    conflicts: List[str],
    protected: List[str],
    target: str = "",
) -> str:
    parent_task_id = task_id_for_artifacts(ctx)
    art_dir = task_artifact_dir_path(getattr(ctx, "drive_root", "."), parent_task_id, create=True)
    verdict = {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "tool": "integrate_subagent_patch",
        "parent_task_id": parent_task_id,
        "child_task_id": child_task_id,
        "outcome": outcome,
        "applied": bool(applied),
        "reason": str(reason or ""),
        "target_root": str(target or ""),
        "files": list(files or []),
        "protected_matches": list(protected or []),
        "conflicts": list(conflicts or []),
        "patch_sha256": str((manifest or {}).get("sha256") or ""),
        "diffstat": str((manifest or {}).get("diffstat") or ""),
    }
    path = art_dir / f"subagent_patch_verdict_{child_task_id}.json"
    try:
        atomic_write_json(path, verdict, trailing_newline=True)
    except Exception:
        return ""
    return str(path)


def _integrate_subagent_patch(
    ctx: ToolContext,
    task_id: str = "",
    decision: str = "apply",
    reason: str = "",
    target_root: str = "",
) -> str:
    child_task_id = str(task_id or "").strip()
    if not child_task_id:
        return "⚠️ TOOL_ARG_ERROR (integrate_subagent_patch): task_id is required (the child whose patch to integrate)."
    decision = str(decision or "apply").strip().lower()
    if decision not in {"apply", "reject"}:
        return "⚠️ TOOL_ARG_ERROR (integrate_subagent_patch): decision must be 'apply' or 'reject'."

    located = _locate_child_patch(ctx, child_task_id)
    if isinstance(located, str):
        return located
    patch_path, manifest, child_result = located
    touched = [str(p) for p in (manifest.get("tracked_changed") or [])]
    touched += [str(p) for p in (manifest.get("untracked_included") or [])]

    # Top-only routing: integrate only your OWN immediate children. A descendant
    # patch must bubble up through its own parent, not jump levels into this repo.
    parent_tid = str(getattr(ctx, "task_id", "") or "").strip()
    child_parent = str((child_result or {}).get("parent_task_id") or "").strip()
    if not parent_tid:
        return (
            "⚠️ INTEGRATE_LINEAGE_FORBIDDEN: this task has no task_id, so child lineage cannot be "
            "verified. Integration is only allowed from the task whose task_id is the child's parent."
        )
    if child_parent != parent_tid:
        return (
            f"⚠️ INTEGRATE_LINEAGE_FORBIDDEN: {child_task_id} is not a direct child of this task "
            f"(its parent is {child_parent or '(unknown)'!r}, not {parent_tid!r}). Top-only routing: "
            "integrate only your own immediate children; descendant patches bubble up one parent at a time."
        )

    if decision == "reject":
        verdict_path = _write_verdict(
            ctx, child_task_id, outcome="rejected", reason=reason, files=touched,
            manifest=manifest, applied=False, conflicts=[], protected=[],
        )
        return (
            f"🚫 Rejected subagent patch from {child_task_id} ({len(touched)} file(s) not applied). "
            f"Verdict: {verdict_path or '(unwritten)'}. Reason: {reason or '(none)'}."
        )

    status = str(manifest.get("status") or "")
    if status != ARTIFACT_STATUS_READY_WITH_CHANGES:
        return (
            f"⚠️ INTEGRATE_NO_CHANGES: child {child_task_id} workspace patch status={status!r}; "
            "nothing to apply."
        )
    if not patch_path.exists():
        return f"⚠️ INTEGRATE_PATCH_MISSING: workspace.patch for {child_task_id} not found at {patch_path}."
    expected_digest = str(manifest.get("sha256") or "")
    if expected_digest:
        actual_digest = _sha256_file(patch_path)
        if actual_digest != expected_digest:
            return (
                f"⚠️ INTEGRATE_PATCH_CORRUPT: sha256 mismatch for {child_task_id} "
                f"(manifest {expected_digest[:12]} != file {actual_digest[:12]}); refusing to apply."
            )

    # Top-only routing for EVERY caller: integration always targets your OWN active
    # repo/worktree. An explicit target_root must equal it (no foreign target, which
    # could be the live repo or another worktree).
    constraint = normalize_task_constraint(getattr(ctx, "task_constraint", None))
    is_acting = bool(constraint and getattr(constraint, "mode", "") == ACTING_SUBAGENT_MODE)
    try:
        active_root = pathlib.Path(ctx.active_repo_dir()).resolve(strict=False)
    except Exception as exc:
        return f"⚠️ INTEGRATE_TARGET_ERROR: could not resolve active repo: {type(exc).__name__}: {exc}."
    requested_target = str(target_root or "").strip()
    if requested_target and pathlib.Path(requested_target).resolve(strict=False) != active_root:
        return (
            "⚠️ INTEGRATE_TARGET_FORBIDDEN: integration targets only your own active repo/worktree "
            "(top-only routing). Drop target_root or set it to the active root; descendant patches "
            "bubble up one parent at a time."
        )
    target = active_root
    if not (target / ".git").exists():
        return f"⚠️ INTEGRATE_TARGET_NOT_GIT: target {target} is not a git working tree."

    runtime_mode = get_runtime_mode()
    # Derive the changed-path set from the PATCH ITSELF (not the child-controlled
    # manifest) for the protected-path gate: a child must not be able to hide a
    # protected edit by omitting it from the manifest (sha256 verifies bytes only).
    numstat = subprocess.run(
        ["git", "apply", "--numstat", str(patch_path)], cwd=str(target), capture_output=True, text=True,
    )
    if numstat.returncode != 0:
        return (
            f"⚠️ INTEGRATE_PATCH_UNREADABLE: cannot parse {child_task_id} workspace.patch for the "
            f"protected-path check (git apply --numstat failed): {(numstat.stderr or '').strip()[:300]}"
        )
    # Derive touched paths from the patch's own `diff --git a/<old> b/<new>` headers
    # (capturing BOTH sides) so a rename/copy of a protected path (e.g. BIBLE.md)
    # cannot evade the gate; union with numstat paths for completeness.
    patch_touched = set()
    try:
        patch_text = patch_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        patch_text = ""
    for m in re.finditer(r"^diff --git a/(.+?) b/(.+?)\s*$", patch_text, re.MULTILINE):
        patch_touched.add(m.group(1).strip())
        patch_touched.add(m.group(2).strip())
    for ln in numstat.stdout.splitlines():
        if ln.strip():
            patch_touched.add(ln.rsplit("\t", 1)[-1].strip())
    protected = protected_paths_in(sorted(patch_touched))
    if protected:
        grant_ok = (not is_acting) or bool(getattr(constraint, "protected_paths_grant", False))
        if not (mode_allows_protected_write(runtime_mode) and grant_ok):
            _write_verdict(
                ctx, child_task_id, outcome="blocked_protected", reason=reason, files=touched,
                manifest=manifest, applied=False, conflicts=[], protected=[p.path for p in protected],
                target=str(target),
            )
            return protected_write_block_message(
                path=protected[0].path,
                runtime_mode=runtime_mode,
                action=f"integrate subagent patch {child_task_id} touching",
            )

    # Serialize the index/worktree mutation with the SAME repo git lock that
    # commit_reviewed uses, so a concurrent integration or a reviewed commit cannot
    # race on the index.
    from ouroboros.tools.git import _acquire_git_lock, _release_git_lock

    try:
        _git_lock = _acquire_git_lock(ctx)
    except Exception as exc:
        return f"⚠️ INTEGRATE_LOCK_TIMEOUT: could not acquire the repo git lock: {type(exc).__name__}: {exc}."
    try:
        proc = subprocess.run(
            ["git", "apply", "--3way", "--index", str(patch_path)],
            cwd=str(target), capture_output=True, text=True,
        )
    finally:
        _release_git_lock(_git_lock)
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout or "").strip()
        conflicts = [ln.strip() for ln in stderr.splitlines() if "conflict" in ln.lower() or "patch failed" in ln.lower()]
        _write_verdict(
            ctx, child_task_id, outcome="conflict", reason=reason, files=touched,
            manifest=manifest, applied=False, conflicts=conflicts or [stderr[:500]],
            protected=[p.path for p in protected], target=str(target),
        )
        return (
            f"⚠️ INTEGRATE_CONFLICT: 3-way apply of {child_task_id} into {target} did not apply cleanly. "
            f"git said: {stderr[:600]}\n"
            "Inspect with vcs_diff and resolve, or run vcs_restore to abort, then retry or pick another child."
        )

    try:
        invalidate_advisory_after_mutation(
            pathlib.Path(getattr(ctx, "drive_root", ".")),
            mutation_root=target,
            changed_paths=touched,
            source_tool="integrate_subagent_patch",
        )
    except Exception:
        pass

    verdict_path = _write_verdict(
        ctx, child_task_id, outcome="applied", reason=reason, files=touched,
        manifest=manifest, applied=True, conflicts=[], protected=[p.path for p in protected],
        target=str(target),
    )
    diffstat = str(manifest.get("diffstat") or "").strip()
    note = ""
    if protected:
        note = f" Includes {len(protected)} protected path(s) (allowed: runtime_mode={runtime_mode})."
    return (
        f"✅ Integrated subagent patch from {child_task_id} into {target} ({len(touched)} file(s), staged).{note}\n"
        f"{diffstat}\n"
        f"Verdict: {verdict_path or '(unwritten)'}.\n"
        "Changes are staged but NOT committed — review and run commit_reviewed yourself (you are the sole committer)."
    )


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "integrate_subagent_patch",
            {
                "name": "integrate_subagent_patch",
                "description": (
                    "Integrate (apply) a mutative subagent's returned workspace.patch into your active "
                    "repo/worktree, or record a rejection. You remain the SOLE COMMITTER: this stages the "
                    "child's changes (manifest-first, sha256-verified, 3-way apply) but does NOT commit — "
                    "you review and run commit_reviewed yourself. Use for best-of-N: pick the best child "
                    "and integrate it, or integrate several to synthesize. Protected-path changes require "
                    "pro runtime mode (and, for a nested acting parent, protected_paths_grant). Conflicts "
                    "are reported for you to resolve (vcs_diff) or abort (vcs_restore). Writes a "
                    "subagent_patch_verdict_<task_id>.json audit artifact."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "string", "description": "The child subagent task_id whose workspace.patch to integrate."},
                        "decision": {"type": "string", "enum": ["apply", "reject"], "default": "apply", "description": "apply = stage the child's patch; reject = record a rejection verdict without applying."},
                        "reason": {"type": "string", "description": "Optional rationale recorded in the verdict (why accept / reject / synthesize)."},
                        "target_root": {"type": "string", "description": "Optional explicit target repo/worktree root. Defaults to your active repo (live repo for the root agent; your worktree for a nested acting parent — top-only routing)."},
                    },
                    "required": ["task_id"],
                },
            },
            _integrate_subagent_patch,
        ),
    ]
