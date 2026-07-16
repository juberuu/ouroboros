"""Private receipt parsing and reconciliation helpers for typed outcomes."""

from __future__ import annotations

import json
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ReviewRunSelection:
    """Current review authority plus retained superseded audit evidence."""

    all_runs: List[Dict[str, Any]]
    current_runs: List[Dict[str, Any]]
    superseded_only_acceptance_gap: bool
    superseded_aggregate_signals: List[str]
    current_candidate_unaccepted: bool
    has_replacement: bool


def select_current_review_runs(
    review_runs: Any,
    *,
    delivery_candidate: Any,
    review_decision: Any,
) -> ReviewRunSelection:
    """Select current review runs without erasing conservative stale failures."""
    all_runs = [
        run for run in (review_runs or [])
        if isinstance(run, dict) and run.get("authority") != "agent_advisory"
    ]
    current_runs = [run for run in all_runs if not run.get("superseded_by_revision")]
    candidate = delivery_candidate if isinstance(delivery_candidate, dict) else {}
    binding = candidate.get("acceptance_binding")
    binding = binding if isinstance(binding, dict) else {}
    decision = review_decision if isinstance(review_decision, dict) else {}
    candidate_unaccepted = bool(candidate) and binding.get("authoritative") is False
    superseded_signals = [
        str(run.get("aggregate_signal") or "").upper() for run in all_runs
    ]
    acceptance_gap = bool(
        all_runs
        and not current_runs
        and candidate_unaccepted
        and not (decision.get("panel_id") and decision.get("binding_hash"))
        and "FAIL" not in superseded_signals
        and "DEGRADED" not in superseded_signals
    )
    selected = [] if acceptance_gap else (current_runs if current_runs else all_runs)
    return ReviewRunSelection(
        all_runs=all_runs,
        current_runs=selected,
        superseded_only_acceptance_gap=acceptance_gap,
        superseded_aggregate_signals=superseded_signals,
        current_candidate_unaccepted=candidate_unaccepted,
        has_replacement=bool(current_runs),
    )


def review_run_ledger_status(
    run: Dict[str, Any],
    selection: ReviewRunSelection,
) -> tuple[str, bool]:
    """Project one acceptance run into the verification ledger."""
    signal = str(run.get("aggregate_signal") or "").upper()
    superseded = bool(run.get("superseded_by_revision")) and (
        selection.has_replacement
        or (selection.current_candidate_unaccepted and signal == "PASS")
    )
    failed = run.get("aggregate_signal") in {"FAIL", "DEGRADED"} or bool(run.get("degraded"))
    return ("superseded" if superseded else ("failed" if failed else "ok")), superseded


def read_receipts(path: Path) -> List[Dict[str, Any]]:
    """Read valid object rows from an append-only verification receipt file."""
    out: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def latest_unreconciled_failed(
    receipts: List[Dict[str, Any]],
    reconciling_statuses: Collection[str],
) -> Optional[Dict[str, Any]]:
    """Return the latest failed receipt unless later grounding reconciles it."""
    latest_fail: Optional[Dict[str, Any]] = None
    reconciled = False
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        status = str(receipt.get("status") or "")
        if status == "fail":
            latest_fail, reconciled = receipt, False
        elif latest_fail is not None and status in reconciling_statuses:
            reconciled = True
    return None if (latest_fail is None or reconciled) else latest_fail


def latest_unreconciled_masked(
    receipts: List[Dict[str, Any]],
    reconciling_statuses: Collection[str],
) -> Optional[Dict[str, Any]]:
    """Return the latest masked pass unless later clean grounding reconciles it."""
    latest_masked: Optional[Dict[str, Any]] = None
    reconciled = False
    for receipt in receipts:
        if not isinstance(receipt, dict):
            continue
        status = str(receipt.get("status") or "")
        masked = bool(receipt.get("check_exit_masking"))
        if status == "pass" and masked:
            latest_masked, reconciled = receipt, False
        elif latest_masked is not None and status in reconciling_statuses and not masked:
            reconciled = True
    return None if (latest_masked is None or reconciled) else latest_masked


def latest_agent_defined(receipts: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the latest ungrounded agent-defined passing criterion, if any."""
    for receipt in reversed(receipts):
        if not isinstance(receipt, dict):
            continue
        if str(receipt.get("status") or "") not in ("pass", "observed"):
            continue
        if str(receipt.get("criterion_source") or "") != "agent_defined":
            return None
        if str(receipt.get("criterion_basis") or "").strip():
            return None
        return receipt
    return None
