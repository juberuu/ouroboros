"""Typed task/loop outcome helpers.

Lifecycle status remains backward-compatible (`completed`, `failed`, ...).
Semantic result status lives beside it so provider/tool/artifact failures do
not masquerade as successful final text.
"""

from __future__ import annotations

import json
import pathlib
from hashlib import sha256
from typing import Any, Dict, List

from ouroboros.headless import (
    ARTIFACT_STATUS_FAILED,
    ARTIFACT_STATUS_FINALIZING,
    ARTIFACT_STATUS_PENDING,
    ARTIFACT_STATUS_READY,
)
from ouroboros.task_results import validate_task_id
from ouroboros.utils import atomic_write_json, utc_now_iso


RESULT_SUCCEEDED = "succeeded"
RESULT_FAILED = "failed"
RESULT_INFRA_FAILED = "infra_failed"
RESULT_PARTIAL = "partial"

REASON_FINAL_MESSAGE = "final_message"
REASON_EMPTY_FINAL_TEXT = "empty_final_text"
REASON_PROVIDER_FAILURE = "provider_failure"
REASON_ARTIFACT_FAILED = "artifact_failed"
REASON_ARTIFACT_PENDING = "artifact_pending"
REASON_TASK_EXCEPTION = "task_exception"
REASON_DEEP_SELF_REVIEW_UNAVAILABLE = "deep_self_review_unavailable"
REASON_DEEP_SELF_REVIEW_ERROR = "deep_self_review_error"


def derive_loop_outcome(final_text: str, usage: Dict[str, Any], llm_trace: Dict[str, Any]) -> Dict[str, Any]:
    """Return a typed LoopOutcome-compatible dict."""

    usage_status = str(usage.get("result_status") or "").strip()
    usage_reason = str(usage.get("reason_code") or "").strip()
    text = str(final_text or "")
    failure: Dict[str, Any] | None = None
    result_status = RESULT_SUCCEEDED
    reason_code = REASON_FINAL_MESSAGE

    if usage_status == RESULT_INFRA_FAILED:
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_PROVIDER_FAILURE
        failure = {"kind": "provider", "reason_code": reason_code}
    elif usage_status == RESULT_FAILED:
        result_status = RESULT_FAILED
        reason_code = usage_reason or REASON_EMPTY_FINAL_TEXT
        failure = {"kind": "agent", "reason_code": reason_code}
    elif not text.strip():
        result_status = RESULT_FAILED
        reason_code = REASON_EMPTY_FINAL_TEXT
        failure = {"kind": "agent", "reason_code": reason_code}
    elif text.lstrip().startswith("⚠️ Failed to get a response") or text.lstrip().startswith("⚠️ All models are down"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_PROVIDER_FAILURE
        failure = {"kind": "provider", "reason_code": reason_code}
    elif text.lstrip().startswith("⚠️ Error during processing:"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_TASK_EXCEPTION
        failure = {"kind": "runtime", "reason_code": reason_code}
    elif text.lstrip().startswith("❌ Deep self-review unavailable:"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_DEEP_SELF_REVIEW_UNAVAILABLE
        failure = {"kind": "runtime", "reason_code": reason_code}
    elif text.lstrip().startswith("⚠️ Deep self-review error:") or text.lstrip().startswith("❌ Deep self-review failed:"):
        result_status = RESULT_INFRA_FAILED
        reason_code = usage_reason or REASON_DEEP_SELF_REVIEW_ERROR
        failure = {"kind": "runtime", "reason_code": reason_code}

    return {
        "schema_version": 1,
        "result_status": result_status,
        "finish_reason": reason_code,
        "reason_code": reason_code,
        "final_text": text,
        "failure": failure,
        "usage": {
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_rounds": int(usage.get("rounds") or 0),
        },
        "trace_refs": collect_trace_refs(usage, llm_trace),
    }


def collect_trace_refs(usage: Dict[str, Any], llm_trace: Dict[str, Any]) -> Dict[str, Any]:
    refs: Dict[str, Any] = {}
    execution_id = str(usage.get("execution_id") or "").strip()
    if execution_id:
        refs["execution_id"] = execution_id
    llm_refs = []
    for item in usage.get("llm_call_refs") or []:
        if not isinstance(item, dict):
            continue
        llm_refs.append({
            "llm_call_id": item.get("llm_call_id"),
            "execution_id": item.get("execution_id"),
            "round_id": item.get("round_id"),
            "round": item.get("round"),
            "request_ref": item.get("request_ref"),
            "response_ref": item.get("response_ref"),
            "model": item.get("model"),
            "resolved_model": item.get("resolved_model"),
            "provider": item.get("provider"),
        })
    if llm_refs:
        refs["llm_call_refs"] = llm_refs
    tool_refs = []
    for item in llm_trace.get("tool_calls") or []:
        if isinstance(item, dict) and item.get("trace_ref"):
            trace = item.get("trace_ref") if isinstance(item.get("trace_ref"), dict) else {}
            tool_refs.append({
                "call_id": trace.get("call_id"),
                "manifest_ref": trace.get("manifest_ref"),
                "redacted_projection_ref": trace.get("redacted_projection_ref"),
                "redaction": trace.get("redaction"),
            })
    if tool_refs:
        refs["tool_call_refs"] = tool_refs
    return refs


def artifact_bundle_from_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return v2 ArtifactBundle while preserving old artifact fields."""

    artifacts = list(result.get("artifacts") or []) if isinstance(result.get("artifacts"), list) else []
    old_status = str(result.get("artifact_status") or "").strip()
    if old_status in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING, ARTIFACT_STATUS_READY, ARTIFACT_STATUS_FAILED}:
        status = old_status
    elif artifacts:
        status = ARTIFACT_STATUS_READY
    else:
        status = "not_applicable"
    records: List[Dict[str, Any]] = []
    for item in artifacts:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "")
        explicit_status = str(item.get("status") or "").strip()
        if explicit_status:
            artifact_status = explicit_status
        elif path and pathlib.Path(path).exists():
            artifact_status = ARTIFACT_STATUS_READY
        elif status in {ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
            artifact_status = status
        else:
            artifact_status = ARTIFACT_STATUS_READY
        record = {
            "kind": str(item.get("kind") or ""),
            "name": str(item.get("name") or pathlib.Path(path).name),
            "path": path,
            "size": int(item.get("size") or 0),
            "sha256": str(item.get("sha256") or ""),
            "status": artifact_status,
            "errors": list(item.get("errors") or []) if isinstance(item.get("errors"), list) else [],
        }
        records.append(record)
    errors = []
    if result.get("artifact_error"):
        errors.append(str(result.get("artifact_error")))
    return {
        "schema_version": 1,
        "status": status,
        "artifacts": records,
        "errors": errors,
    }


def refresh_verification_ledger_artifacts(
    ledger: Dict[str, Any] | None,
    artifact_bundle: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Return ``ledger`` with artifact status synchronized after finalization."""

    if not isinstance(ledger, dict):
        return ledger
    entries = [
        item for item in (ledger.get("entries") or [])
        if not (isinstance(item, dict) and item.get("kind") == "artifact_bundle")
    ]
    artifact_status = str((artifact_bundle or {}).get("status") or "")
    if artifact_status in {ARTIFACT_STATUS_FAILED, ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
        entries.append({
            "kind": "artifact_bundle",
            "status": artifact_status,
            "errors": (artifact_bundle or {}).get("errors") or [],
        })
    updated = dict(ledger)
    updated["entries"] = entries
    updated["summary"] = {
        "entry_count": len(entries),
        "has_failures": any(str(item.get("status") or "").lower() not in {"", "ok", RESULT_SUCCEEDED} for item in entries if isinstance(item, dict)),
    }
    return updated


def build_verification_ledger(
    *,
    task: Dict[str, Any],
    loop_outcome: Dict[str, Any],
    llm_trace: Dict[str, Any],
    artifact_bundle: Dict[str, Any],
    review_evidence: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Build a task-scoped verification ledger from authoritative runtime facts."""

    entries: List[Dict[str, Any]] = []
    if loop_outcome.get("result_status") != RESULT_SUCCEEDED:
        entries.append({
            "kind": "loop_outcome",
            "status": loop_outcome.get("result_status"),
            "reason_code": loop_outcome.get("reason_code"),
        })

    for idx, call in enumerate(llm_trace.get("tool_calls") or [], start=1):
        if not isinstance(call, dict):
            continue
        status = str(call.get("status") or ("error" if call.get("is_error") else "ok"))
        if call.get("is_error") or status not in {"ok", ""}:
            entries.append({
                "kind": "tool_call",
                "index": idx,
                "tool": call.get("tool"),
                "status": status,
                "exit_code": call.get("exit_code"),
                "signal": call.get("signal"),
                "trace_ref": call.get("trace_ref"),
            })

    for event in llm_trace.get("verification_events") or []:
        if isinstance(event, dict):
            entries.append({"kind": "runtime_event", **event})

    for run in llm_trace.get("review_runs") or []:
        if isinstance(run, dict):
            failed = run.get("aggregate_signal") in {"FAIL", "DEGRADED"} or bool(run.get("degraded"))
            entries.append({
                "kind": "task_acceptance_review",
                "status": "failed" if failed else "ok",
                "aggregate_signal": run.get("aggregate_signal"),
                "degraded": run.get("degraded"),
                "finding_count": len(run.get("parsed_findings") or []),
            })

    artifact_status = str(artifact_bundle.get("status") or "")
    if artifact_status in {ARTIFACT_STATUS_FAILED, ARTIFACT_STATUS_PENDING, ARTIFACT_STATUS_FINALIZING}:
        entries.append({
            "kind": "artifact_bundle",
            "status": artifact_status,
            "errors": artifact_bundle.get("errors") or [],
        })

    review = review_evidence or {}
    for key in ("critical_findings", "advisory_findings", "open_obligations"):
        items = review.get(key)
        if isinstance(items, list) and items:
            status = "failed" if key in {"critical_findings", "open_obligations"} else "partial"
            entries.append({
                "kind": "review",
                "category": key,
                "status": status,
                "count": len(items),
                "items": items[:10],
                "omitted": max(0, len(items) - 10),
            })

    return {
        "schema_version": 1,
        "created_at": utc_now_iso(),
        "task_id": str(task.get("id") or task.get("task_id") or ""),
        "entries": entries,
        "summary": {
            "entry_count": len(entries),
            "has_failures": any(str(item.get("status") or "").lower() not in {"", "ok", RESULT_SUCCEEDED} for item in entries),
        },
    }


def maybe_write_verification_artifact(
    drive_root: pathlib.Path,
    task_id: str,
    ledger: Dict[str, Any],
    *,
    threshold_chars: int = 12_000,
) -> Dict[str, Any]:
    """Inline small ledgers; write large ledgers as task artifacts."""

    raw = json.dumps(ledger, ensure_ascii=False, sort_keys=True, default=str)
    if len(raw) <= threshold_chars:
        return {"inline": ledger, "artifact": None}
    safe_task = validate_task_id(task_id)
    artifact_dir = pathlib.Path(drive_root) / "task_results" / "artifacts" / safe_task
    artifact_dir.mkdir(parents=True, exist_ok=True)
    path = artifact_dir / "verification_ledger.json"
    atomic_write_json(path, ledger, trailing_newline=True)
    data = path.read_bytes()
    return {
        "inline": {
            "schema_version": 1,
            "created_at": ledger.get("created_at"),
            "task_id": ledger.get("task_id"),
            "summary": ledger.get("summary") or {},
            "omitted_to_artifact": True,
        },
        "artifact": {
            "kind": "verification_ledger",
            "name": "verification_ledger.json",
            "path": str(path),
            "size": len(data),
            "sha256": sha256(data).hexdigest(),
            "status": ARTIFACT_STATUS_READY,
            "errors": [],
        },
    }
