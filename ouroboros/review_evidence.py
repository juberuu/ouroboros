"""Structured review-evidence collection for summaries, reflections, and UX."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List


def collect_review_evidence(
    drive_root: Any,
    *,
    task_id: str = "",
    repo_dir: Any = None,
    max_attempts: int = 3,
    max_runs: int = 3,
    max_obligations: int | None = None,
    max_continuations: int = 3,
) -> Dict[str, Any]:
    from ouroboros.review_state import (
        _LEGACY_CURRENT_REPO_KEY,
        compute_snapshot_hash,
        load_state,
        make_repo_key,
    )
    from ouroboros.task_continuation import list_review_continuations

    drive_root_path = pathlib.Path(drive_root)
    repo_dir_path = pathlib.Path(repo_dir) if repo_dir else None
    repo_key = make_repo_key(repo_dir_path) if repo_dir_path else ""
    snapshot_hash = compute_snapshot_hash(repo_dir_path) if repo_dir_path else ""

    state = load_state(drive_root_path)
    all_runs = list(state.advisory_runs or [])
    all_attempts = list(state.attempts or [])

    if repo_key:
        repo_runs = state.filter_advisory_runs(repo_key=repo_key)
    else:
        repo_runs = all_runs

    if task_id:
        scoped_attempts = state.filter_attempts(task_id=task_id)
    elif repo_key:
        scoped_attempts = state.filter_attempts(repo_key=repo_key)
    else:
        scoped_attempts = all_attempts

    current_run = None
    if snapshot_hash:
        current_run = state.find_by_hash(snapshot_hash, repo_key=repo_key or None)

    open_obligations = state.get_open_obligations(repo_key=repo_key or None)
    open_debts = state.get_open_commit_readiness_debts(repo_key=repo_key or None)
    continuations, corrupt = list_review_continuations(drive_root_path)
    if task_id:
        scoped_continuations = [item for item in continuations if item.task_id == task_id]
    elif repo_key:
        scoped_continuations = [
            item for item in continuations
            if item.repo_key in ("", repo_key, _LEGACY_CURRENT_REPO_KEY)
        ]
    else:
        scoped_continuations = continuations
    scoped_continuations.sort(key=lambda item: str(item.updated_ts or item.created_ts or ""), reverse=True)
    stale_matches_repo = not repo_key or state.last_stale_repo_key in ("", repo_key)

    evidence = {
        "task_id": task_id,
        "repo_key": repo_key,
        "current_repo": {
            "snapshot_hash": snapshot_hash[:12] if snapshot_hash else "",
            "advisory_status": str(getattr(current_run, "status", "") or "missing"),
            "repo_commit_ready": bool(
                current_run is not None
                and current_run.status in ("fresh", "bypassed", "skipped")
                and not open_obligations
                and not open_debts
            ),
            "bypass_reason": str(getattr(current_run, "bypass_reason", "") or ""),
            "stale_reason": str(getattr(state, "last_stale_reason", "") or "") if stale_matches_repo else "",
            "stale_ts": str(getattr(state, "last_stale_from_edit_ts", "") or "") if stale_matches_repo else "",
        },
        "recent_attempts": [_attempt_to_dict(item) for item in (scoped_attempts[-max_attempts:] if max_attempts > 0 else [])],
        "omitted_attempts": max(0, len(scoped_attempts) - max_attempts) if max_attempts > 0 else len(scoped_attempts),
        "recent_advisory_runs": [_run_to_dict(item) for item in (repo_runs[-max_runs:] if max_runs > 0 else [])],
        "omitted_advisory_runs": max(0, len(repo_runs) - max_runs) if max_runs > 0 else len(repo_runs),
        "open_obligations": [_obligation_to_dict(item) for item in (open_obligations[:max_obligations] if max_obligations is not None else open_obligations)],
        "omitted_obligations": max(0, len(open_obligations) - max_obligations) if max_obligations is not None else 0,
        "commit_readiness_debts": [_debt_to_dict(item) for item in open_debts],
        "continuations": [_continuation_to_dict(item) for item in scoped_continuations[:max_continuations]],
        "omitted_continuations": max(0, len(scoped_continuations) - max_continuations),
        "corrupt_continuations": [str(item) for item in corrupt[:3]],
        "omitted_corrupt": max(0, len(corrupt) - 3),
    }
    evidence["has_evidence"] = any([
        evidence["recent_attempts"],
        evidence["recent_advisory_runs"],
        evidence["open_obligations"],
        evidence["commit_readiness_debts"],
        evidence["continuations"],
        evidence["corrupt_continuations"],
        evidence["current_repo"]["advisory_status"] not in ("", "missing"),
        # Omission counters signal truncated evidence even when visible lists are empty
        evidence["omitted_attempts"] > 0,
        evidence["omitted_advisory_runs"] > 0,
        evidence["omitted_obligations"] > 0,
        evidence["omitted_continuations"] > 0,
        evidence["omitted_corrupt"] > 0,
    ])
    return evidence


def format_review_evidence_for_prompt(
    evidence: Dict[str, Any],
    *,
    max_chars: int = 0,
    **_kwargs,
) -> str:
    """Format review evidence as JSON for prompt injection.

    When *max_chars* is 0 (default) the full JSON is returned — no truncation.
    Callers that inject evidence into bounded prompts (summaries, reflections)
    can pass a positive *max_chars* to get an explicit omission note instead
    of silent clipping.
    """
    if not evidence or not evidence.get("has_evidence"):
        return "(no structured review evidence)"
    full = json.dumps(evidence, ensure_ascii=False, indent=2)
    if max_chars > 0 and len(full) > max_chars:
        return full[:max_chars] + f"\n⚠️ OMISSION NOTE: review evidence truncated at {max_chars} chars; original length {len(full)}"
    return full


def _attempt_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "ts": str(getattr(item, "ts", "") or ""),
        "tool_name": str(getattr(item, "tool_name", "") or ""),
        "attempt": int(getattr(item, "attempt", 0) or 0),
        "status": str(getattr(item, "status", "") or ""),
        "phase": str(getattr(item, "phase", "") or ""),
        "block_reason": str(getattr(item, "block_reason", "") or ""),
        "late_result_pending": bool(getattr(item, "late_result_pending", False)),
        "critical_findings": list(getattr(item, "critical_findings", []) or []),
        "advisory_findings": list(getattr(item, "advisory_findings", []) or []),
        "readiness_warnings": [str(x) for x in (getattr(item, "readiness_warnings", []) or [])],
        "obligation_ids": [str(x) for x in (getattr(item, "obligation_ids", []) or [])],
        "degraded_reasons": [str(x) for x in (getattr(item, "degraded_reasons", []) or [])],
        "triad_models": [str(x) for x in (getattr(item, "triad_models", []) or [])],
        "scope_model": str(getattr(item, "scope_model", "") or ""),
        "duration_sec": float(getattr(item, "duration_sec", 0.0) or 0.0),
        "triad_raw_results": list(getattr(item, "triad_raw_results", []) or []),
        "scope_raw_result": dict(getattr(item, "scope_raw_result", {}) or {}),
    }


_RESPONDED_STATUSES = frozenset({"fresh", "stale"})


def _run_to_dict(item: Any) -> Dict[str, Any]:
    """Serialise an AdvisoryRunRecord with status-aware shape.

    Different statuses carry different evidential weight:
    - ``responded_clean`` — reviewer ran AND produced zero FAILs (a real PASS)
    - ``responded_with_findings`` — reviewer ran AND found issues (listed in findings)
    - ``bypassed`` — advisory gate was explicitly skipped with an audit reason
    - ``skipped`` — advisory was skipped because there was nothing to review
    - ``parse_failure`` — reviewer responded but output couldn't be parsed
    - ``error`` — transport/infrastructure failure
    - ``stale`` — was fresh but is now outdated (edits after run)

    ``status_summary`` collapses this into a single token so downstream
    consumers (task reflections, prompt injection) can distinguish
    responded-clean from skipped without re-deriving it from raw fields.
    ``raw_result_present`` flags whether the canonical raw text is still on
    disk (used to decide whether a verbose ``review_status`` call would
    actually surface anything new).
    """
    fail_items: List[Dict[str, Any]] = []
    total_items = 0
    for entry in list(getattr(item, "items", []) or []):
        if not isinstance(entry, dict):
            continue
        total_items += 1
        if str(entry.get("verdict", "")).upper() != "FAIL":
            continue
        fail_items.append({
            "severity": str(entry.get("severity", "") or "advisory"),
            "item": str(entry.get("item", "") or ""),
            "reason": str(entry.get("reason", "") or ""),
        })

    status = str(getattr(item, "status", "") or "")
    bypass_reason = str(getattr(item, "bypass_reason", "") or "")
    raw_result_text = str(getattr(item, "raw_result", "") or "")

    if status == "bypassed":
        status_summary = "bypassed"
    elif status == "skipped":
        status_summary = "skipped"
    elif status == "parse_failure":
        status_summary = "parse_failure"
    elif status == "error":
        status_summary = "error"
    elif status in _RESPONDED_STATUSES and fail_items:
        status_summary = "responded_with_findings"
    elif status in _RESPONDED_STATUSES and total_items > 0 and not fail_items:
        status_summary = "responded_clean"
    elif status in _RESPONDED_STATUSES:
        # Responded but no items at all — distinct from "clean" (zero FAILs)
        status_summary = "responded_empty"
    else:
        status_summary = status or "unknown"

    return {
        "ts": str(getattr(item, "ts", "") or ""),
        "status": status,
        "status_summary": status_summary,
        "repo_key": str(getattr(item, "repo_key", "") or ""),
        "bypass_reason": bypass_reason,
        "snapshot_summary": str(getattr(item, "snapshot_summary", "") or ""),
        "findings": fail_items,
        "total_items": total_items,
        "raw_result_present": bool(raw_result_text),
        "readiness_warnings": [str(x) for x in (getattr(item, "readiness_warnings", []) or [])],
        "prompt_chars": int(getattr(item, "prompt_chars", 0) or 0),
        "model_used": str(getattr(item, "model_used", "") or ""),
        "duration_sec": float(getattr(item, "duration_sec", 0.0) or 0.0),
    }


def _obligation_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "obligation_id": str(getattr(item, "obligation_id", "") or ""),
        "fingerprint": str(getattr(item, "fingerprint", "") or ""),
        "item": str(getattr(item, "item", "") or ""),
        "severity": str(getattr(item, "severity", "") or ""),
        "reason": str(getattr(item, "reason", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "created_ts": str(getattr(item, "created_ts", "") or ""),
        "updated_ts": str(getattr(item, "updated_ts", "") or ""),
    }


def _continuation_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "task_id": str(getattr(item, "task_id", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "stage": str(getattr(item, "stage", "") or ""),
        "tool_name": str(getattr(item, "tool_name", "") or ""),
        "attempt": int(getattr(item, "attempt", 0) or 0),
        "block_reason": str(getattr(item, "block_reason", "") or ""),
        "critical_findings": list(getattr(item, "critical_findings", []) or []),
        "advisory_findings": list(getattr(item, "advisory_findings", []) or []),
        "readiness_warnings": [str(x) for x in (getattr(item, "readiness_warnings", []) or [])],
        "updated_ts": str(getattr(item, "updated_ts", "") or ""),
    }


def _debt_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "debt_id": str(getattr(item, "debt_id", "") or ""),
        "category": str(getattr(item, "category", "") or ""),
        "title": str(getattr(item, "title", "") or ""),
        "summary": str(getattr(item, "summary", "") or ""),
        "status": str(getattr(item, "status", "") or ""),
        "severity": str(getattr(item, "severity", "") or ""),
        "source": str(getattr(item, "source", "") or ""),
        "repo_key": str(getattr(item, "repo_key", "") or ""),
        "source_obligation_ids": [str(x) for x in (getattr(item, "source_obligation_ids", []) or [])],
        "evidence": [str(x) for x in (getattr(item, "evidence", []) or [])],
        "updated_at": str(getattr(item, "updated_at", "") or ""),
    }
