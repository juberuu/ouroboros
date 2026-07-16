"""Task-wide mutation baselines and attributed Git candidates.

This module answers one pure evidence question: which paths were clean when
the host captured a task's baseline and changed during the task's observed
window?  The window is observational — nothing here claims or enforces
exclusive ownership of a surface.  The evidence lives in the existing task
result; no second ledger is introduced, and honest ambiguity (a pre-existing
dirty path that changed, a stale baseline) is reported as blockers for the
reviewing LLM panels to weigh, never as an automatic verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from ouroboros.task_results import STATUS_RUNNING, load_task_result, write_task_result
from ouroboros.utils import safe_relpath, utc_now_iso

MUTATION_EVIDENCE_VERSION = 1
_GIT_SURFACE_TYPES = frozenset({"system_repo", "external_workspace"})
# Full-content hashing is bounded: a dirty artifact bigger than this records a
# size-only fingerprint (audit evidence stays bounded; candidate math never
# depends on fingerprint content, only on the dirty path NAME set).
_FINGERPRINT_MAX_BYTES = 32 * 1024 * 1024
# A trashed worktree (build generations, browser caches) can list tens of
# thousands of dirty paths; storing/fingerprinting them would stall task start
# and bloat the durable task result. Past this cap the baseline is honestly
# unusable for attribution instead of expensively pretending.
_BASELINE_DIRTY_PATHS_MAX = 4096
_OBSERVED_EFFECT_STATES = frozenset({"observed_window", "quiescent"})


def attribution_task_id(results_drive_root: Any, candidates: Iterable[Any]) -> str:
    """Return the first task id in lineage order with a durable baseline."""
    for candidate in candidates:
        task_id = str(candidate or "").strip()
        if not task_id:
            continue
        try:
            result = load_task_result(results_drive_root, task_id) or {}
        except Exception:
            continue
        evidence = result.get("mutation_evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("baseline"), dict):
            return task_id
    return ""


def _canonical_root(value: Any) -> pathlib.Path:
    text = str(value or "").strip()
    if not text:
        raise ValueError("mutation surface root is required")
    return pathlib.Path(text).expanduser().resolve(strict=False)


def _stable_hash(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _run_git(root: pathlib.Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "git command failed").strip()
        raise RuntimeError(detail)
    return proc.stdout


def _git_status_paths(root: pathlib.Path) -> list[str]:
    """Return every path named by porcelain-v1, including both rename sides."""
    raw = _run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = raw.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        row = fields[index]
        index += 1
        if not row:
            continue
        if len(row) < 4:
            raise RuntimeError("malformed git porcelain row")
        status = row[:2]
        path = row[3:]
        if path:
            paths.append(path)
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError("malformed git rename/copy row")
            paths.append(fields[index])
            index += 1
    return sorted(dict.fromkeys(paths))


def _git_committed_paths(root: pathlib.Path, base_commit: str, current_head: str) -> list[str]:
    """Return both sides of paths changed after the baseline commit."""
    if not base_commit or not current_head or base_commit == current_head:
        return []
    raw = _run_git(
        root,
        "diff",
        "--name-only",
        "-z",
        "--no-renames",
        base_commit,
        current_head,
    )
    return sorted(dict.fromkeys(path for path in raw.split("\0") if path))


def _path_fingerprint(path: pathlib.Path) -> dict[str, Any]:
    """Fingerprint one exact known path without walking any parent directory."""
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    if path.is_symlink():
        return {
            "kind": "symlink",
            "target": os.readlink(path),
        }
    if path.is_file():
        if int(stat.st_size) > _FINGERPRINT_MAX_BYTES:
            return {
                "kind": "file",
                "size": int(stat.st_size),
                "sha256_skipped": "over_size_cap",
            }
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return {
            "kind": "file",
            "size": int(stat.st_size),
            "sha256": digest.hexdigest(),
        }
    if path.is_dir():
        # Deliberately do not recurse.  user_files may be the owner's home and
        # the contract permits only declared targets / existing artifact refs.
        return {"kind": "directory"}
    return {"kind": "other", "size": int(stat.st_size)}


def _normalize_known_paths(root: pathlib.Path, values: Iterable[Any]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        candidate = pathlib.Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve(strict=False)
        try:
            rel = resolved.relative_to(root).as_posix()
        except ValueError as exc:
            raise ValueError(f"known mutation target escapes surface root: {text}") from exc
        if rel not in normalized:
            normalized.append(rel)
    return sorted(normalized)


def _surface_identity(surface: Mapping[str, Any]) -> tuple[str, pathlib.Path]:
    surface_type = str(surface.get("surface_type") or surface.get("type") or "").strip()
    if surface_type not in {
        "system_repo",
        "skill_payload",
        "external_workspace",
        "user_files",
    }:
        raise ValueError(f"unknown mutation surface type: {surface_type!r}")
    # ``host_root`` is the durable surface identity.  Accept the ``root``
    # spelling only when reading transitional in-memory evidence.
    root = _canonical_root(surface.get("host_root") or surface.get("root"))
    return surface_type, root


def _capture_surface(surface: Mapping[str, Any]) -> dict[str, Any]:
    surface_type, root = _surface_identity(surface)
    known_paths = _normalize_known_paths(root, surface.get("known_paths") or [])
    row: dict[str, Any] = {
        "surface_type": surface_type,
        "canonical_root": str(root),
        "known_paths": known_paths,
        "captured_at": utc_now_iso(),
    }
    if surface_type in _GIT_SURFACE_TYPES:
        try:
            inside = _run_git(root, "rev-parse", "--is-inside-work-tree").strip() == "true"
        except Exception:
            inside = False
        if inside:
            dirty_paths = _git_status_paths(root)
            if len(dirty_paths) > _BASELINE_DIRTY_PATHS_MAX:
                row["git"] = {
                    "base_commit": _run_git(root, "rev-parse", "HEAD").strip(),
                    "base_tree": _run_git(root, "rev-parse", "HEAD^{tree}").strip(),
                    "dirty_overflow": len(dirty_paths),
                }
                return row
            row["git"] = {
                "base_commit": _run_git(root, "rev-parse", "HEAD").strip(),
                "base_tree": _run_git(root, "rev-parse", "HEAD^{tree}").strip(),
                "dirty_paths": dirty_paths,
                "dirty_fingerprints": {
                    path: _path_fingerprint(root / path) for path in dirty_paths
                },
            }
            return row
    row["known_path_fingerprints"] = {
        path: _path_fingerprint(root / path) for path in known_paths
    }
    return row


def capture_mutation_baseline(
    results_drive_root: Any,
    task_id: str,
    surfaces: Sequence[Mapping[str, Any]],
    *,
    owner_kind: str = "task_root",
    owner_id: str = "",
) -> dict[str, Any]:
    """Capture and strictly confirm surface baselines for one task.

    The host calls this when a root task starts (and may append a late,
    previously unseen surface before its first write).  Existing roots and
    known-target fingerprints are immutable once captured.
    """
    task_id = str(task_id or "").strip()
    if not task_id:
        raise ValueError("task_id is required for mutation evidence")
    existing = load_task_result(results_drive_root, task_id) or {}
    prior = existing.get("mutation_evidence")
    prior_baseline = prior.get("baseline") if isinstance(prior, dict) else None
    captured = [dict(row) for row in (prior_baseline or {}).get("surfaces") or []]
    owner_kind = str(owner_kind or "task_root")
    owner_id = str(owner_id or task_id)
    if isinstance(prior_baseline, dict):
        if (
            str(prior_baseline.get("owner_kind") or "") != owner_kind
            or str(prior_baseline.get("owner_id") or "") != owner_id
        ):
            raise RuntimeError("mutation baseline owner cannot change")
    existing_by_key = {
        (str(row.get("surface_type") or ""), str(row.get("canonical_root") or "")): row
        for row in captured
    }
    added: list[dict[str, Any]] = []
    for surface in surfaces:
        surface_type, root = _surface_identity(surface)
        key = (surface_type, str(root))
        existing_surface = existing_by_key.get(key)
        if existing_surface is not None:
            # Git baselines already cover the complete worktree. Bounded
            # non-Git surfaces add a newly declared exact target before that
            # target's first write without re-fingerprinting older targets.
            if not isinstance(existing_surface.get("git"), dict):
                requested = _normalize_known_paths(root, surface.get("known_paths") or [])
                prior_paths = {
                    str(path) for path in existing_surface.get("known_paths") or []
                }
                new_paths = sorted(path for path in requested if path not in prior_paths)
                if new_paths:
                    fingerprints = dict(existing_surface.get("known_path_fingerprints") or {})
                    fingerprints.update({
                        path: _path_fingerprint(root / path) for path in new_paths
                    })
                    existing_surface["known_paths"] = sorted(prior_paths | set(new_paths))
                    existing_surface["known_path_fingerprints"] = fingerprints
                    added.append({
                        "surface_type": key[0],
                        "canonical_root": key[1],
                        "known_paths": new_paths,
                    })
            continue
        candidate = _capture_surface(surface)
        captured.append(candidate)
        existing_by_key[key] = candidate
        added.append({"surface_type": key[0], "canonical_root": key[1]})
    if isinstance(prior_baseline, dict) and not added:
        return dict(prior)
    captured.sort(key=lambda item: (item["surface_type"], item["canonical_root"]))
    baseline = dict(prior_baseline or {})
    baseline.update({
        "captured_at": str(baseline.get("captured_at") or utc_now_iso()),
        "owner_kind": owner_kind,
        "owner_id": owner_id,
        "surfaces": captured,
    })
    if prior_baseline and added:
        extensions = [
            dict(row) for row in baseline.get("extensions") or [] if isinstance(row, dict)
        ]
        extensions.append({"captured_at": utc_now_iso(), "surfaces": added})
        baseline["extensions"] = extensions
    baseline.pop("baseline_hash", None)
    baseline["baseline_hash"] = _stable_hash(baseline)
    evidence = dict(prior or {})
    evidence.update({
        "version": MUTATION_EVIDENCE_VERSION,
        "baseline": baseline,
        "effect_state": str(evidence.get("effect_state") or "observed_window"),
        "flags": list(evidence.get("flags") or []),
    })
    status = str(existing.get("status") or STATUS_RUNNING)
    written = write_task_result(
        results_drive_root,
        task_id,
        status,
        mutation_evidence=evidence,
    )
    confirmed = written.get("mutation_evidence") if isinstance(written, dict) else None
    if not isinstance(confirmed, dict) or confirmed.get("baseline") != baseline:
        raise RuntimeError("mutation baseline was not durably confirmed")
    return dict(confirmed)


def mutation_evidence_projection(evidence: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return bounded host evidence suitable for acceptance and outcome gates."""
    if not isinstance(evidence, Mapping) or not evidence:
        return {}

    blockers: list[str] = []
    baseline = evidence.get("baseline")
    surfaces = baseline.get("surfaces") if isinstance(baseline, Mapping) else None
    if not isinstance(baseline, Mapping) or not isinstance(surfaces, list) or not surfaces:
        blockers.append("baseline_missing")
        surfaces = []
    baseline_hash = str(baseline.get("baseline_hash") or "") if isinstance(baseline, Mapping) else ""
    if surfaces and not baseline_hash:
        blockers.append("baseline_hash_missing")

    surface_counts: dict[str, int] = {}
    known_path_count = 0
    preexisting_dirty_count = 0
    for row in surfaces[:64]:
        if not isinstance(row, Mapping):
            blockers.append("baseline_surface_malformed")
            continue
        surface_type = str(row.get("surface_type") or "unknown")[:40]
        surface_counts[surface_type] = surface_counts.get(surface_type, 0) + 1
        known_path_count += len(row.get("known_paths") or [])
        git = row.get("git")
        if isinstance(git, Mapping):
            preexisting_dirty_count += len(git.get("dirty_paths") or [])
    if len(surfaces) > 64:
        blockers.append("baseline_surface_limit_exceeded")

    effect_state = str(evidence.get("effect_state") or "")[:80]
    if effect_state not in _OBSERVED_EFFECT_STATES:
        blockers.append(effect_state or "effect_state_unknown")
    # ``flags`` is a reserved evidence field: no runtime writer exists in
    # v6.66.0, but a populated flag (from older evidence or a future writer)
    # must still surface as a blocker rather than vanish.
    flags = sorted(dict.fromkeys(
        str(row.get("flag") or "")[:80]
        for row in evidence.get("flags") or []
        if isinstance(row, Mapping) and str(row.get("flag") or "").strip()
    ))[:32]
    blockers.extend(flags)

    terminal = evidence.get("terminal_candidate_snapshot")
    terminal_candidate_count = 0
    terminal_excluded_count = 0
    if terminal is None and effect_state == "quiescent":
        blockers.append("terminal_snapshot_missing")
    elif terminal is not None:
        if not isinstance(terminal, Mapping):
            blockers.append("terminal_snapshot_malformed")
        else:
            terminal_rows = terminal.get("surfaces")
            if not isinstance(terminal_rows, list):
                blockers.append("terminal_snapshot_malformed")
            else:
                for row in terminal_rows[:64]:
                    if not isinstance(row, Mapping):
                        blockers.append("terminal_snapshot_malformed")
                        continue
                    terminal_candidate_count += len(row.get("candidates") or [])
                    excluded = len(row.get("excluded_preexisting_dirty") or [])
                    terminal_excluded_count += excluded
                    if excluded:
                        blockers.append("preexisting_dirty_changed")
                    blockers.extend(
                        str(item)[:80]
                        for item in row.get("blockers") or []
                        if str(item).strip()
                    )
                if len(terminal_rows) > 64:
                    blockers.append("terminal_surface_limit_exceeded")

    blockers = sorted(dict.fromkeys(item for item in blockers if item))
    return {
        "version": MUTATION_EVIDENCE_VERSION,
        "present": True,
        "baseline_hash": baseline_hash,
        "effect_state": effect_state,
        "flags": flags,
        "surface_counts": dict(sorted(surface_counts.items())),
        "known_path_count": known_path_count,
        "preexisting_dirty_count": preexisting_dirty_count,
        "terminal_snapshot_present": isinstance(terminal, Mapping),
        "terminal_candidate_count": terminal_candidate_count,
        "terminal_excluded_count": terminal_excluded_count,
        "blockers": blockers,
        "clean_eligible": not blockers,
    }


def load_mutation_evidence_projection(
    results_drive_root: Any,
    task_id: str,
) -> dict[str, Any]:
    """Load the compact projection without exposing roots, paths, or fingerprints."""
    try:
        result = load_task_result(results_drive_root, str(task_id or "")) or {}
    except Exception:
        return {
            "version": MUTATION_EVIDENCE_VERSION,
            "present": False,
            "effect_state": "unavailable",
            "flags": [],
            "surface_counts": {},
            "blockers": ["mutation_evidence_unavailable"],
            "clean_eligible": False,
        }
    return mutation_evidence_projection(result.get("mutation_evidence"))


def attributed_git_candidates(
    results_drive_root: Any,
    task_id: str,
    repo_root: Any,
) -> dict[str, Any]:
    """Compute the exact clean-at-baseline Git candidate set for one root."""
    root = _canonical_root(repo_root)
    result = load_task_result(results_drive_root, task_id) or {}
    evidence = result.get("mutation_evidence")
    blockers: list[str] = []
    if not isinstance(evidence, dict):
        return {
            "candidates": [],
            "excluded_preexisting_dirty": [],
            "blockers": ["baseline_missing"],
            "baseline_hash": "",
        }
    baseline = evidence.get("baseline")
    if not isinstance(baseline, dict):
        blockers.append("baseline_missing")
        surfaces: list[dict[str, Any]] = []
    else:
        surfaces = [row for row in baseline.get("surfaces") or [] if isinstance(row, dict)]
    matching = [
        row for row in surfaces
        if str(row.get("canonical_root") or "") == str(root)
        and isinstance(row.get("git"), dict)
    ]
    if len(matching) != 1:
        blockers.append("baseline_surface_missing" if not matching else "baseline_surface_ambiguous")
        return {
            "candidates": [],
            "excluded_preexisting_dirty": [],
            "blockers": blockers,
            "baseline_hash": str((baseline or {}).get("baseline_hash") or ""),
        }
    git = matching[0]["git"]
    if git.get("dirty_overflow"):
        return {
            "candidates": [],
            "excluded_preexisting_dirty": [],
            "blockers": ["baseline_dirty_overflow"],
            "baseline_hash": str(baseline.get("baseline_hash") or ""),
        }
    try:
        current_head = _run_git(root, "rev-parse", "HEAD").strip()
        changed = _git_status_paths(root)
    except Exception:
        blockers.append("candidate_scan_failed")
        current_head = ""
        changed = []
    if current_head != str(git.get("base_commit") or ""):
        blockers.append("baseline_stale")
    dirty = {str(path) for path in git.get("dirty_paths") or []}
    fingerprints = git.get("dirty_fingerprints") or {}
    excluded = sorted(path for path in changed if path in dirty)
    candidates = sorted(path for path in changed if path not in dirty)
    # A pre-existing dirty path is excluded evidence either way; it becomes a
    # blocker only when its content actually CHANGED during the observed window
    # (unchanged owner WIP merely persisting must not wedge the task's commits).
    if any(_path_fingerprint(root / path) != fingerprints.get(path) for path in excluded):
        blockers.append("preexisting_dirty_changed")
    effect_state = str(evidence.get("effect_state") or "")
    if effect_state not in _OBSERVED_EFFECT_STATES:
        blockers.append(effect_state or "effect_state_unknown")
    for flag_row in evidence.get("flags") or []:
        if isinstance(flag_row, dict) and str(flag_row.get("flag") or ""):
            blockers.append(str(flag_row["flag"]))
    return {
        "candidates": candidates,
        "excluded_preexisting_dirty": excluded,
        "blockers": sorted(dict.fromkeys(blockers)),
        "baseline_hash": str(baseline.get("baseline_hash") or ""),
        "base_commit": str(git.get("base_commit") or ""),
        "base_tree": str(git.get("base_tree") or ""),
        "canonical_root": str(root),
    }


def resolve_attributed_git_paths(
    results_drive_root: Any,
    task_id: str,
    repo_root: Any,
    explicit_paths: Sequence[Any] | None,
) -> tuple[list[str], dict[str, Any], str]:
    """Resolve omitted/explicit staging paths against one attributed candidate set."""
    evidence = attributed_git_candidates(results_drive_root, task_id, repo_root)
    blockers = [str(item) for item in evidence.get("blockers") or [] if str(item)]
    if blockers == ["baseline_stale"] and try_reanchor_stale_baseline(
        results_drive_root, task_id, repo_root,
    ):
        evidence = attributed_git_candidates(results_drive_root, task_id, repo_root)
        blockers = [str(item) for item in evidence.get("blockers") or [] if str(item)]
    if blockers:
        return [], evidence, (
            "⚠️ GIT_ATTRIBUTION_BLOCKED: clean task attribution is unavailable "
            f"({', '.join(blockers)}). Automatic staging is disabled; preserve the "
            "unattributed changes and resolve the evidence conflict before review."
        )
    candidates = [str(item) for item in evidence.get("candidates") or [] if str(item)]
    if explicit_paths is None:
        selected = candidates
    else:
        try:
            selected = sorted(dict.fromkeys(
                safe_relpath(str(path)) for path in explicit_paths if str(path or "").strip()
            ))
        except ValueError as exc:
            return [], evidence, f"⚠️ PATH_ERROR: {exc}"
        outside = sorted(set(selected) - set(candidates))
        if outside:
            return [], evidence, (
                "⚠️ GIT_ATTRIBUTION_BLOCKED: explicit paths must be a subset of "
                f"the task-attributed candidates. Outside set: {', '.join(outside)}"
            )
    if not selected:
        return [], evidence, (
            "⚠️ GIT_NO_ATTRIBUTED_CHANGES: no clean-at-baseline task-owned paths "
            "are available to stage."
        )
    return selected, evidence, ""


def record_terminal_mutation_candidates(
    results_drive_root: Any,
    task_id: str,
) -> dict[str, Any]:
    """Persist one quiescent projection from the existing baseline authority."""
    current = load_task_result(results_drive_root, task_id) or {}
    evidence = dict(current.get("mutation_evidence") or {})
    # The final task-result write publishes terminal candidates and the
    # ``quiescent`` effect state atomically.  Until it succeeds the durable
    # evidence keeps its earlier state (``observed_window``), so a claimed
    # quiescence without a snapshot stays detectable.
    evidence["effect_state"] = "quiescent"
    baseline = evidence.get("baseline") if isinstance(evidence.get("baseline"), dict) else {}
    rows: list[dict[str, Any]] = []
    for surface in baseline.get("surfaces") or []:
        if not isinstance(surface, dict):
            continue
        root_text = str(surface.get("canonical_root") or "")
        row: dict[str, Any] = {
            "surface_type": str(surface.get("surface_type") or ""),
            "canonical_root": root_text,
        }
        if isinstance(surface.get("git"), dict):
            git = surface["git"]
            blockers: list[str] = []
            if git.get("dirty_overflow"):
                row.update({
                    "candidates": [],
                    "excluded_preexisting_dirty": [],
                    "blockers": ["baseline_dirty_overflow"],
                })
                rows.append(row)
                continue
            try:
                root = _canonical_root(root_text)
                current_head = _run_git(root, "rev-parse", "HEAD").strip()
                changed = sorted(dict.fromkeys(
                    _git_status_paths(root)
                    + _git_committed_paths(
                        root,
                        str(git.get("base_commit") or ""),
                        current_head,
                    )
                ))
            except Exception:
                current_head = ""
                changed = []
                blockers.append("candidate_scan_failed")
            dirty = {str(path) for path in git.get("dirty_paths") or []}
            fingerprints = git.get("dirty_fingerprints") or {}
            excluded = sorted(path for path in changed if path in dirty)
            if any(
                _path_fingerprint(root / path) != fingerprints.get(path)
                for path in excluded
            ):
                blockers.append("preexisting_dirty_changed")
            effect_state = str(evidence.get("effect_state") or "")
            if effect_state != "quiescent":
                blockers.append(effect_state or "effect_state_unknown")
            blockers.extend(
                str(flag_row.get("flag") or "")
                for flag_row in evidence.get("flags") or []
                if isinstance(flag_row, dict) and str(flag_row.get("flag") or "")
            )
            row.update({
                "candidates": sorted(path for path in changed if path not in dirty),
                "excluded_preexisting_dirty": excluded,
                "blockers": sorted(dict.fromkeys(blockers)),
                "head_advanced": bool(
                    current_head
                    and current_head != str(git.get("base_commit") or "")
                ),
            })
        else:
            root = _canonical_root(root_text)
            prior = surface.get("known_path_fingerprints") or {}
            changed: list[str] = []
            for path in surface.get("known_paths") or []:
                normalized = str(path or "")
                if normalized and _path_fingerprint(root / normalized) != prior.get(normalized):
                    changed.append(normalized)
            row.update({"candidates": sorted(changed), "blockers": []})
        rows.append(row)
    evidence["terminal_candidate_snapshot"] = {
        "captured_at": utc_now_iso(),
        "baseline_hash": str(baseline.get("baseline_hash") or ""),
        "surfaces": rows,
    }
    status = str(current.get("status") or STATUS_RUNNING)
    written = write_task_result(
        results_drive_root,
        task_id,
        status,
        mutation_evidence=evidence,
    )
    confirmed = written.get("mutation_evidence") if isinstance(written, dict) else None
    if (
        not isinstance(confirmed, dict)
        or str(confirmed.get("effect_state") or "") != "quiescent"
        or confirmed.get("terminal_candidate_snapshot")
        != evidence["terminal_candidate_snapshot"]
    ):
        raise RuntimeError("terminal mutation evidence was not durably confirmed")
    return dict(confirmed)


def try_reanchor_stale_baseline(
    results_drive_root: Any,
    task_id: str,
    repo_root: Any,
) -> bool:
    """Heal a stale baseline when HEAD moved past it harmlessly.

    A foreign fast-forward commit that touches NONE of the currently dirty
    paths cannot change what this task may attribute, so wedging every later
    ``commit_reviewed`` on ``baseline_stale`` would punish the task for someone
    else's unrelated work. Conditions are strict: the baseline commit must be
    an ancestor of HEAD and the committed interval must be disjoint from every
    currently dirty path; anything else keeps the fail-closed stale blocker.
    The re-anchor is auditable as a ``foreign_ff_reanchor`` epoch row.
    """
    root = _canonical_root(repo_root)
    result = load_task_result(results_drive_root, str(task_id or "")) or {}
    evidence = result.get("mutation_evidence")
    baseline = evidence.get("baseline") if isinstance(evidence, dict) else None
    if not isinstance(baseline, dict):
        return False
    git_row = next(
        (
            row.get("git")
            for row in baseline.get("surfaces") or []
            if isinstance(row, dict)
            and str(row.get("canonical_root") or "") == str(root)
            and isinstance(row.get("git"), dict)
        ),
        None,
    )
    if not isinstance(git_row, dict):
        return False
    base_commit = str(git_row.get("base_commit") or "")
    try:
        current_head = _run_git(root, "rev-parse", "HEAD").strip()
        if not base_commit or base_commit == current_head:
            return False
        ancestor_probe = subprocess.run(
            ["git", "merge-base", "--is-ancestor", base_commit, current_head],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=False,
        )
        if ancestor_probe.returncode != 0:
            return False
        interval = set(_git_committed_paths(root, base_commit, current_head))
        current_dirty = set(_git_status_paths(root))
    except Exception:
        return False
    if interval & current_dirty:
        return False
    try:
        advance_mutation_baseline(
            results_drive_root, task_id, root, reason="foreign_ff_reanchor",
        )
    except Exception:
        return False
    return True


def advance_mutation_baseline(
    results_drive_root: Any,
    task_id: str,
    repo_root: Any,
    *,
    reason: str = "attributed_commit",
) -> dict[str, Any]:
    """Open a new baseline epoch after the task's own attributed commit.

    The committed content is now part of HEAD, so a fresh capture re-marks the
    surviving foreign dirt as pre-existing and the next attributed staging
    window opens from the new commit instead of failing ``baseline_stale``.
    An unexplained HEAD move (no advance) still surfaces as a stale baseline.
    """
    root = _canonical_root(repo_root)
    current = load_task_result(results_drive_root, str(task_id or "")) or {}
    evidence = dict(current.get("mutation_evidence") or {})
    baseline = evidence.get("baseline")
    if not isinstance(baseline, dict):
        raise RuntimeError("cannot advance a missing mutation baseline")
    surfaces = [dict(row) for row in baseline.get("surfaces") or [] if isinstance(row, dict)]
    replaced = False
    for idx, row in enumerate(surfaces):
        if str(row.get("canonical_root") or "") != str(root) or not isinstance(row.get("git"), dict):
            continue
        # The new epoch inherits the INTERSECTION of the original pre-existing
        # dirty set with what is still dirty now. A blind re-capture would
        # re-mark the task's own not-yet-committed leftovers as foreign
        # pre-existing dirt and wedge their follow-up commit.
        prior_dirty = {str(path) for path in row["git"].get("dirty_paths") or []}
        still_dirty = sorted(prior_dirty & set(_git_status_paths(root)))
        new_row = dict(row)
        new_row["captured_at"] = utc_now_iso()
        new_row["git"] = {
            "base_commit": _run_git(root, "rev-parse", "HEAD").strip(),
            "base_tree": _run_git(root, "rev-parse", "HEAD^{tree}").strip(),
            "dirty_paths": still_dirty,
            "dirty_fingerprints": {
                path: _path_fingerprint(root / path) for path in still_dirty
            },
        }
        surfaces[idx] = new_row
        replaced = True
    if not replaced:
        raise RuntimeError("no git baseline surface matches the committed root")
    epochs = [dict(r) for r in baseline.get("epochs") or [] if isinstance(r, dict)]
    epochs.append({"advanced_at": utc_now_iso(), "reason": str(reason or "attributed_commit")})
    baseline = dict(baseline)
    baseline.update({"surfaces": surfaces, "epochs": epochs})
    baseline.pop("baseline_hash", None)
    baseline["baseline_hash"] = _stable_hash(baseline)
    evidence.update({
        "version": MUTATION_EVIDENCE_VERSION,
        "baseline": baseline,
        "effect_state": "observed_window",
    })
    # A terminal snapshot from the previous epoch must not linger as current.
    evidence.pop("terminal_candidate_snapshot", None)
    status = str(current.get("status") or STATUS_RUNNING)
    written = write_task_result(
        results_drive_root,
        task_id,
        status,
        mutation_evidence=evidence,
    )
    confirmed = written.get("mutation_evidence") if isinstance(written, dict) else None
    if not isinstance(confirmed, dict) or confirmed.get("baseline") != baseline:
        raise RuntimeError("advanced mutation baseline was not durably confirmed")
    return dict(confirmed)


__all__ = [
    "MUTATION_EVIDENCE_VERSION",
    "advance_mutation_baseline",
    "attribution_task_id",
    "attributed_git_candidates",
    "capture_mutation_baseline",
    "load_mutation_evidence_projection",
    "mutation_evidence_projection",
    "record_terminal_mutation_candidates",
    "resolve_attributed_git_paths",
    "try_reanchor_stale_baseline",
]
