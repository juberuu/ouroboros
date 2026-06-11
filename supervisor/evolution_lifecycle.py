"""Helpers for evolution campaign transaction lifecycle."""

from __future__ import annotations

import os
import pathlib
from typing import Any, Dict

from ouroboros.utils import atomic_write_json, utc_now_iso


def build_evolution_task_text(cycle: int) -> str:
    """Build the next evolution-campaign task prompt. Lazy imports for the
    queue-private campaign reader avoid a module-load cycle with supervisor.queue."""
    from supervisor.queue import _read_evolution_campaign
    from ouroboros.outcomes import normalize_outcome_axes
    from ouroboros.config import get_evolution_persistent_objective

    campaign = _read_evolution_campaign()
    if campaign.get("status") != "active":
        return f"EVOLUTION #{cycle}"
    parts = [
        f"EVOLUTION CAMPAIGN {campaign.get('id') or 'active'} — CYCLE #{cycle}",
        "",
        "## Objective",
        str(campaign.get("objective") or "Autonomously improve Ouroboros."),
    ]
    steer = get_evolution_persistent_objective()
    if steer:
        parts.extend([
            "",
            "## Owner Standing Steer (optional bias — does NOT override the Objective above)",
            steer,
        ])
    progress = str(campaign.get("progress_notes") or "").strip()
    if progress:
        parts.extend(["", "## Progress So Far", progress])
    history = list(campaign.get("history") or [])[-3:]
    if history:
        parts.extend(["", "## Recent Campaign Cycles"])
        for row in history:
            axes = normalize_outcome_axes(row)
            execution_status = str((axes.get("execution") or {}).get("status") or "unknown")
            objective_status = str((axes.get("objective") or {}).get("status") or "not_evaluated")
            parts.append(
                f"- {row.get('task_id')}: execution={execution_status}, objective={objective_status}; "
                f"rounds={row.get('rounds', 0)}; cost=${float(row.get('cost_usd') or 0):.4f}"
            )
    parts.extend([
        "",
        "## Execution Contract",
        "- Work as a normal Ouroboros self-improvement task.",
        "- Use standard tests and the normal advisory + triad + scope review flow before committing code.",
        "- Land at most ONE reviewed self-modification commit in this cycle. Fold reviewer fixes into that commit before committing; do not churn follow-up commits.",
        "- After a reviewed commit lands, call request_restart once and stop. Restart verification is the absorption boundary for the cycle.",
        "- An honest no-op is a legitimate outcome when the objective is unsafe, already solved, too broad, or needs owner input; do not commit just to make a cycle non-empty.",
        "- If the best next step is memory/identity/backlog rather than code, update those durable artifacts with provenance, but do not treat that as an absorbed self-evolution cycle.",
        "- A true absorbed self-evolution cycle requires one reviewed self-modification commit followed by successful restart verification before the next campaign cycle.",
        "- The review enforcement mode (advisory vs blocking) is the owner's setting. Do NOT hardcode review findings to always block (or always pass) regardless of that mode: forcing per-finding blocks under an owner-chosen advisory mode is forbidden self-modification (BIBLE P3), not a hardening. If advisory pass-through of a critical finding feels wrong, surface it to the owner — never patch the enforcement gate to override their choice.",
        "- If the objective is complete or needs owner input, say so clearly in the final result.",
    ])
    return "\n".join(parts)


def notify_owner_cycle_outcome(campaign: Dict[str, Any], tx: Dict[str, Any]) -> None:
    """WS-13.5 (e5=ux_absorb_report): owner-facing chat note for a finished
    self-evolution cycle. Absorbed -> short what/why; abandoned -> honest
    warning; no_op / waiting -> quiet (the lifecycle event already records it).
    No web/UI edits; chat only, budget-gated. Lazy imports avoid an import
    cycle with supervisor.state / supervisor.message_bus.
    """
    outcome = str(tx.get("cycle_outcome") or "")
    if outcome not in ("absorbed", "abandoned"):
        return  # no_op / waiting_for_restart: event-only, stay quiet
    from supervisor.state import load_state
    from supervisor.message_bus import send_with_budget
    owner_chat_id = int(load_state().get("owner_chat_id") or 0)
    if not owner_chat_id:
        return
    objective = str(campaign.get("objective") or "").strip()
    obj_short = (objective[:160] + "…") if len(objective) > 160 else objective
    if outcome == "absorbed":
        commit_sha = str(tx.get("commit_sha") or "").strip()[:12]
        msg = (
            f"🧬 Evolution cycle absorbed (commit {commit_sha}).\n"
            f"Objective: {obj_short or 'autonomous self-improvement'}\n"
            "The reviewed self-modification is now live (restart verified). Reply if you want it reverted."
        )
    else:
        reason = str(tx.get("abandoned_reason") or "unspecified")
        msg = (
            f"⚠️ Evolution cycle abandoned (reason: {reason}).\n"
            f"Objective: {obj_short or 'autonomous self-improvement'}\n"
            "No change was absorbed; the transaction was rolled back/closed to unblock the next cycle."
        )
    send_with_budget(owner_chat_id, msg)


def append_unique_transaction(campaign: Dict[str, Any], tx: Dict[str, Any]) -> None:
    tx_history = list(campaign.get("transaction_history") or [])
    tx_id = str(tx.get("transaction_id") or "")
    if tx_id and any(isinstance(item, dict) and str(item.get("transaction_id") or "") == tx_id for item in tx_history):
        campaign["transaction_history"] = tx_history[-50:]
        return
    tx_history.append(dict(tx))
    campaign["transaction_history"] = tx_history[-50:]


def request_evolution_restart(drive_root: pathlib.Path, tx: Dict[str, Any], log: Any = None) -> None:
    if str(os.environ.get("OUROBOROS_EVOLUTION_AUTO_RESTART", "true") or "true").lower() in {"0", "false", "no", "off"}:
        return
    commit_sha = str(tx.get("commit_sha") or "").strip()
    if not commit_sha:
        return
    try:
        atomic_write_json(
            pathlib.Path(drive_root) / "state" / "pending_restart_verify.json",
            {
                "ts": utc_now_iso(),
                "expected_sha": commit_sha,
                "expected_branch": str(tx.get("base_branch") or ""),
                "reason": "supervisor_auto_evolution_restart",
            },
            trailing_newline=True,
        )
        from supervisor import workers

        workers.get_event_q().put({
            "type": "restart_request",
            "reason": "supervisor_auto_evolution_restart",
            "ts": utc_now_iso(),
        })
    except Exception:
        if log is not None:
            log.debug("Failed to request automatic evolution restart", exc_info=True)
