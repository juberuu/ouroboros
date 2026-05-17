"""Structured review-evidence collection for summaries, reflections, and UX."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.utils import truncate_review_artifact


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


def build_review_projection(
    drive_root: Any,
    *,
    repo_dir: Any = None,
    repo_key: str = "",
    tool_name: str = "",
    task_id: str = "",
    attempt: int | None = None,
    snapshot_hash_fn: Any = None,
) -> Dict[str, Any]:
    """Build the semantic read-model shared by review_status-style renderers."""
    from ouroboros.review_state import (
        compute_snapshot_hash,
        load_state,
        make_repo_key,
    )

    drive_root_path = pathlib.Path(drive_root)
    repo_dir_path = pathlib.Path(repo_dir) if repo_dir else None
    state = load_state(drive_root_path)
    repo_filter = repo_key or (make_repo_key(repo_dir_path) if repo_dir_path is not None else None)
    tool_filter = tool_name or None
    task_filter = task_id or None
    runs = state.filter_advisory_runs(
        repo_key=repo_filter,
        tool_name=tool_filter,
        task_id=task_filter,
        attempt=attempt,
    )
    attempts = state.filter_attempts(
        repo_key=repo_filter,
        tool_name=tool_filter,
        task_id=task_filter,
        attempt=attempt,
    )
    latest = runs[-1] if runs else None
    selected_attempt = attempts[-1] if attempts else (
        None if (repo_filter or tool_filter or task_filter or attempt is not None) else state.latest_attempt()
    )
    try:
        if repo_dir_path is None:
            raise ValueError("repo_dir unavailable")
        hasher = snapshot_hash_fn or compute_snapshot_hash
        current_hash = hasher(repo_dir_path, "", paths=latest.snapshot_paths if latest else None)
        hash_mismatch = bool(
            latest
            and latest.status in {"fresh", "bypassed", "skipped", "parse_failure", "preflight_blocked", "tests_preflight_blocked"}
            and latest.snapshot_hash != current_hash
        )
    except Exception:
        current_hash = ""
        hash_mismatch = False
    matching_run = state.find_by_hash(current_hash, repo_key=repo_filter) if current_hash else None
    effective_is_fresh = bool(state.is_fresh(current_hash, repo_key=repo_filter) if current_hash else False)
    stale_matches_repo = state.last_stale_repo_key in ("", repo_filter)
    stale_from_edit = bool(hash_mismatch or (state.last_stale_from_edit_ts and stale_matches_repo))
    effective_status = matching_run.status if matching_run else ("stale" if latest else "none")
    open_obligations = state.get_open_obligations(repo_key=repo_filter)
    open_debts = state.get_open_commit_readiness_debts(repo_key=repo_filter)
    return {
        "state": state,
        "filters": {
            "repo_key": repo_filter,
            "tool_name": tool_filter,
            "task_id": task_filter,
            "attempt": attempt,
        },
        "runs": runs,
        "attempts": attempts,
        "latest_run": latest,
        "matching_run": matching_run,
        "guidance_run": matching_run or latest,
        "selected_attempt": selected_attempt,
        "current_hash": current_hash,
        "effective_status": effective_status,
        "effective_hash": matching_run.snapshot_hash[:12] if matching_run and matching_run.snapshot_hash else None,
        "effective_is_fresh": effective_is_fresh,
        "stale_from_edit": stale_from_edit,
        "stale_from_edit_ts": (
            state.last_stale_from_edit_ts if state.last_stale_from_edit_ts and stale_matches_repo
            else ("now (hash mismatch)" if hash_mismatch else None)
        ),
        "stale_reason": (
            state.last_stale_reason if stale_matches_repo else ""
        ) or ("Current snapshot hash no longer matches the latest advisory run." if hash_mismatch else None),
        "open_obligations": open_obligations,
        "open_debts": open_debts,
        "repo_commit_ready": bool(effective_is_fresh and not open_obligations and not open_debts),
        "retry_anchor": "commit_readiness_debt" if open_debts else None,
    }


def build_review_status_payload(projection: Dict[str, Any], *, next_step: str, include_raw: bool = False) -> Dict[str, Any]:
    selected_attempt = projection.get("selected_attempt")
    open_obligations = list(projection.get("open_obligations") or [])
    open_debts = list(projection.get("open_debts") or [])
    payload: Dict[str, Any] = {
        "latest_advisory_status": projection["effective_status"],
        "latest_advisory_hash": projection["effective_hash"],
        "stale_from_edit": projection["stale_from_edit"],
        "stale_from_edit_ts": projection["stale_from_edit_ts"],
        "stale_reason": projection["stale_reason"],
        "filters": projection["filters"],
        "advisory_runs": [_review_status_run_to_dict(run) for run in reversed(projection.get("runs") or [])],
        "attempts": [_review_status_attempt_to_dict(item) for item in reversed(projection.get("attempts") or [])],
        "selected_commit_attempt": _review_status_attempt_payload(selected_attempt),
        "open_obligations": [_review_status_obligation_to_dict(item) for item in open_obligations],
        "open_obligations_count": len(open_obligations),
        "commit_readiness_debts": [_review_status_debt_to_dict(item) for item in open_debts],
        "commit_readiness_debts_count": len(open_debts),
        "repo_commit_ready": projection["repo_commit_ready"],
        "retry_anchor": projection["retry_anchor"],
        "status_summary": _review_status_message(projection),
        "next_step": next_step,
    }
    payload["message"] = payload["status_summary"]
    if include_raw and selected_attempt is not None:
        payload["raw_evidence"] = {
            "attempt_ts": selected_attempt.ts,
            "attempt_number": int(selected_attempt.attempt or 0) or None,
            "tool_name": selected_attempt.tool_name or None,
            "triad_raw_results": list(selected_attempt.triad_raw_results or []),
            "scope_raw_result": dict(selected_attempt.scope_raw_result or {}),
        }
    return payload


def _review_status_run_to_dict(run: Any) -> Dict[str, Any]:
    findings = [
        item for item in (getattr(run, "items", []) or [])
        if isinstance(item, dict) and str(item.get("verdict", "")).upper() == "FAIL"
    ]
    return {
        "snapshot_hash": str(getattr(run, "snapshot_hash", ""))[:12],
        "commit_message": str(getattr(run, "commit_message", "") or ""),
        "status": str(getattr(run, "status", "") or ""),
        "ts": str(getattr(run, "ts", "") or ""),
        "critical_findings": sum(1 for item in findings if str(item.get("severity", "")).lower() == "critical"),
        "total_findings": len(findings),
        "snapshot_summary": str(getattr(run, "snapshot_summary", "") or ""),
        "bypass_reason": str(getattr(run, "bypass_reason", "") or "") or None,
        "repo_key": str(getattr(run, "repo_key", "") or "") or None,
        "tool_name": str(getattr(run, "tool_name", "") or "") or None,
        "task_id": str(getattr(run, "task_id", "") or "") or None,
        "attempt": int(getattr(run, "attempt", 0) or 0) or None,
    }


def _review_status_attempt_payload(ca: Any) -> Dict[str, Any] | None:
    if ca is None:
        return None
    return {
        "status": ca.status,
        "commit_message": ca.commit_message,
        "ts": ca.ts,
        "duration_sec": round(ca.duration_sec, 1),
        "block_reason": ca.block_reason or None,
        "block_details_preview": truncate_review_artifact(ca.block_details, limit=300) if ca.block_details else None,
        "repo_key": ca.repo_key or None,
        "tool_name": ca.tool_name or None,
        "task_id": ca.task_id or None,
        "attempt": int(ca.attempt or 0) or None,
        "phase": ca.phase or None,
        "blocked": bool(ca.blocked),
        "late_result_pending": bool(ca.late_result_pending),
        "critical_findings": len(ca.critical_findings or []),
        "advisory_findings": len(ca.advisory_findings or []),
        "obligation_ids": list(ca.obligation_ids or []),
        "readiness_warnings": list(ca.readiness_warnings or []),
        "pre_review_fingerprint": ca.pre_review_fingerprint[:12] or None,
        "post_review_fingerprint": ca.post_review_fingerprint[:12] or None,
        "fingerprint_status": ca.fingerprint_status or None,
        "degraded_reasons": list(ca.degraded_reasons or []),
        **_review_status_actor_summary(ca),
    }


def _review_status_attempt_to_dict(item: Any) -> Dict[str, Any]:
    data = _review_status_attempt_payload(item) or {}
    data.pop("commit_message", None)
    data.pop("block_details_preview", None)
    data["ts"] = item.ts
    return data


def _review_status_actor_summary(attempt: Any) -> Dict[str, Any]:
    scope_raw = getattr(attempt, "scope_raw_result", None) or {}
    return {
        "triad_actors": [
            {"model_id": r.get("model_id", "?"), "status": r.get("status", "?")}
            for r in (getattr(attempt, "triad_raw_results", None) or [])
        ],
        "scope_actor": (
            {"model_id": scope_raw.get("model_id", "?"), "status": scope_raw.get("status", "?")}
            if scope_raw.get("status") else None
        ),
    }


def _review_status_obligation_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "obligation_id": item.obligation_id,
        "fingerprint": getattr(item, "fingerprint", ""),
        "item": item.item,
        "severity": item.severity,
        "reason": truncate_review_artifact(item.reason, limit=200),
        "status": item.status,
        "source_ts": item.source_attempt_ts,
        "source_commit": item.source_attempt_msg,
    }


def _review_status_debt_to_dict(item: Any) -> Dict[str, Any]:
    return {
        "debt_id": item.debt_id,
        "category": item.category,
        "title": item.title,
        "summary": truncate_review_artifact(item.summary, limit=220),
        "status": item.status,
        "severity": item.severity,
        "source": item.source,
        "repo_key": item.repo_key or None,
        "source_obligation_ids": list(item.source_obligation_ids or []),
        "evidence": list(item.evidence or []),
        "updated_at": item.updated_at,
    }


def _review_status_message(projection: Dict[str, Any]) -> str:
    ca = projection.get("selected_attempt")
    current = f"Current advisory: {projection['effective_status']}"
    if ca and ca.status in ("blocked", "failed"):
        reason_map = {
            "no_advisory": "No fresh advisory pre-review found. Run advisory_pre_review first.",
            "critical_findings": "Reviewers found critical issues. Fix all issues listed, then re-run advisory.",
            "review_quorum": "Not enough review models responded. Retry — usually transient.",
            "parse_failure": "Review models could not produce parseable output. Retry the commit.",
            "infra_failure": "Infrastructure failure. Check block_details.",
            "scope_blocked": "Scope reviewer blocked the commit. Address scope review findings.",
            "preflight": "Preflight check failed. Stage all related files.",
            "revalidation_failed": "The staged diff changed after review. Re-run advisory and review.",
            "fingerprint_unavailable": "The staged diff could not be fingerprinted. Fix git diff and retry.",
            "overlap_guard": "Another reviewed attempt is still active. Wait or expire it before retrying.",
        }
        label = "BLOCKED" if ca.status == "blocked" else "FAILED"
        current = (
            f"Last commit {label} ({ca.block_reason or 'unclassified'}): "
            f"{reason_map.get(ca.block_reason, ca.block_reason or 'unknown')}"
            f"  |  {current}"
        )
    if projection.get("open_debts"):
        current = f"{current}  |  Commit-readiness debt: {len(projection['open_debts'])}"
    return current


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
    """Serialise AdvisoryRunRecord with responded/skipped/error status summary."""
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
