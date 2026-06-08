"""Post-task self-evolution (V4 envelope + V5 promotion) — owner-gated, LLM-first.

After a qualifying task completes, OPTIONALLY promote one concrete, high-value
self-improvement into the EXISTING gated evolution campaign. The worker writes a
durable request file on the canonical drive; the supervisor's idle tick applies
it via ``start_evolution_campaign`` + enabling evolution, after which the normal
``enqueue_evolution_task_if_needed`` runs the cycle through EVERY safety gate
(idle, restart-verify, 3-fail breaker, budget reserve, advanced/pro,
owner_chat_id).

Invariants (red-team guards — keep intact):
- The worker NEVER enqueues or enables evolution itself; it only writes a durable
  signal that the gated supervisor tick applies (R1.1).
- Promotion never fires from evolution/deep_self_review/subagent tasks (R1.2
  loop guard), nor on a non-canonical (child) dual-run pass.
- The promotion DECISION is a structured LLM judgment, never keyword/threshold
  (R1.3 / BIBLE P5).
- A promoted item that requires plan review carries that obligation into the
  objective (R4.1 advisory->reviewed boundary).
- Default OFF; only the owner enables the envelope.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)

_REQUEST_REL = "state/post_task_evolution_request.json"
_COUNTER_REL = "state/post_task_evolution_counter.json"
_SKIP_TYPES = frozenset({"evolution", "deep_self_review"})


def _resolve(value: Any) -> Optional[pathlib.Path]:
    try:
        return pathlib.Path(str(value)).resolve(strict=False)
    except Exception:
        return None


def _is_canonical_run(env: Any, task: Dict[str, Any]) -> bool:
    """True when ``env.drive_root`` is the canonical drive (a shared task, or the
    parent dual-run pass for forked/empty/workspace). Prevents double promotion
    and targets the canonical backlog/campaign."""
    bdr = str(task.get("budget_drive_root") or "").strip()
    if not bdr:
        return True
    a, b = _resolve(bdr), _resolve(getattr(env, "drive_root", ""))
    return bool(a and b and a == b)


def _eligible(task: Dict[str, Any]) -> bool:
    if str(task.get("type") or "") in _SKIP_TYPES:
        return False
    if str(task.get("delegation_role") or "") == "subagent":
        return False
    return True


def _parse_every_n(cadence: str) -> int:
    try:
        if ":" in cadence:
            return max(1, int(cadence.split(":", 1)[1].strip()))
    except (ValueError, TypeError):
        pass
    return 1


def _counter_due(drive_root: pathlib.Path, k: int) -> bool:
    path = drive_root / _COUNTER_REL
    n = 0
    try:
        if path.exists():
            n = int(json.loads(path.read_text(encoding="utf-8")).get("n") or 0)
    except Exception:
        n = 0
    n += 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"n": n}), encoding="utf-8")
    except Exception:
        pass
    return (n % max(1, k)) == 0


def _backlog_digest(drive_root: pathlib.Path) -> str:
    try:
        from ouroboros.improvement_backlog import format_backlog_digest

        return format_backlog_digest(drive_root, limit=8) or ""
    except Exception:
        return ""


def _loose_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        obj = json.loads(text[start:end + 1])
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


_DECISION_PROMPT = """You decide whether Ouroboros should run ONE reviewed self-improvement (evolution) cycle now, based on the task it just finished and its improvement backlog.

[JUST-FINISHED TASK REFLECTION]
{reflection}

[CURRENT IMPROVEMENT BACKLOG]
{backlog}

Return ONLY a JSON object:
{{"promote": true|false, "objective": "<one concrete, self-contained improvement to Ouroboros's own code/process; empty if not promoting>", "requires_plan_review": true|false, "backlog_id": "<id if this maps to a backlog item, else empty>"}}

Rules: set promote=true ONLY when there is a concrete, high-value, self-contained code/process improvement worth a reviewed cycle right now. Prefer items already in the backlog. If nothing is clearly worthwhile, return promote=false. {force_note}"""


def _decide_promotion(env: Any, task: Dict[str, Any], reflection_entry: Optional[Dict[str, Any]],
                      llm_client: Any, *, force: bool) -> Optional[Dict[str, Any]]:
    from ouroboros.utils import truncate_review_artifact

    drive_root = pathlib.Path(str(env.drive_root))
    # No silent slicing of cognitive artifacts (BIBLE P1 / DEVELOPMENT.md): use the
    # omission-note truncation helper so the model sees that content was capped.
    reflection = truncate_review_artifact(str((reflection_entry or {}).get("reflection") or ""), 1500)
    backlog = truncate_review_artifact(_backlog_digest(drive_root), 3000)
    force_note = (
        "The cadence already decided WHEN to evolve; choose the single most valuable "
        "objective and set promote=true unless the backlog is empty/irrelevant."
        if force else ""
    )
    prompt = _DECISION_PROMPT.format(
        reflection=reflection or "(none)", backlog=backlog or "(empty)", force_note=force_note,
    )
    try:
        from ouroboros.config import get_light_model
        from ouroboros.llm import LLMClient
        from ouroboros.llm_observability import chat_observed

        client = llm_client or LLMClient()
        resp, usage = chat_observed(
            client,
            drive_root=drive_root,
            task_id=str(task.get("id") or "post_task_evolution"),
            call_type="post_task_evolution_decision",
            messages=[{"role": "user", "content": prompt}],
            model=get_light_model(),
            reasoning_effort="low",
            max_tokens=2048,
        )
        if usage:
            try:
                from supervisor.state import update_budget_from_usage

                update_budget_from_usage(usage)
            except Exception:
                pass
        obj = _loose_json((resp.get("content") or "").strip())
        if not obj:
            return None
        return {
            "promote": bool(obj.get("promote")),
            "objective": str(obj.get("objective") or "").strip(),
            # Default to requiring plan review (preserve the advisory->reviewed boundary).
            "requires_plan_review": bool(obj.get("requires_plan_review", True)),
            "backlog_id": str(obj.get("backlog_id") or "").strip(),
        }
    except Exception:
        log.debug("post_task_evolution: decision LLM call failed", exc_info=True)
        return None


def _write_request(drive_root: pathlib.Path, decision: Dict[str, Any], task: Dict[str, Any]) -> None:
    from ouroboros.utils import utc_now_iso

    req = {
        "schema_version": 1,
        "ts": utc_now_iso(),
        "objective": decision["objective"],
        "requires_plan_review": bool(decision.get("requires_plan_review", True)),
        "backlog_id": decision.get("backlog_id") or "",
        "source": "post_task",
        "origin_task_id": str(task.get("id") or ""),
    }
    path = drive_root / _REQUEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic publish: the supervisor polls every tick, so a partial write must
    # never be observable (else it could parse-fail and drop the signal).
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(req, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def maybe_promote(env: Any, task: Dict[str, Any], reflection_entry: Optional[Dict[str, Any]],
                  llm_client: Any = None) -> Optional[Dict[str, Any]]:
    """Worker-side: write a durable promotion signal if the envelope is on and a
    qualifying task surfaced a worthwhile self-improvement. Returns the decision
    or None. NEVER enqueues/enables evolution (that is the supervisor's job)."""
    try:
        from ouroboros.config import (
            get_post_task_evolution_cadence,
            get_post_task_evolution_enabled,
            get_runtime_mode,
        )

        if not get_post_task_evolution_enabled():
            return None
        if get_runtime_mode() == "light":
            return None
        if not _eligible(task) or not _is_canonical_run(env, task):
            return None
        cadence = get_post_task_evolution_cadence()
        if cadence == "off":
            return None
        drive_root = pathlib.Path(str(env.drive_root))
        force = not cadence.startswith("llm")
        if cadence.startswith("every_n") and not _counter_due(drive_root, _parse_every_n(cadence)):
            return None
        decision = _decide_promotion(env, task, reflection_entry, llm_client, force=force)
        if not decision or not decision.get("promote") or not decision.get("objective"):
            return None
        _write_request(drive_root, decision, task)
        log.info("post_task_evolution: durable promotion signal written (origin task=%s)",
                 str(task.get("id") or ""))
        return decision
    except Exception:
        log.debug("post_task_evolution.maybe_promote failed", exc_info=True)
        return None


def _safe_unlink(path: pathlib.Path) -> None:
    try:
        path.unlink()
    except Exception:
        pass


def apply_pending_request(drive_root: Any) -> bool:
    """Supervisor-side (idle tick): apply a pending durable promotion through the
    gated campaign machinery. Sets the campaign objective + enables evolution so
    the normal idle-tick ``enqueue_evolution_task_if_needed`` runs the cycle under
    all safety gates. Marks ``post_task_autostop`` so the absorbed cycle disables
    evolution again (one-shot). Returns True if a promotion was applied."""
    try:
        from ouroboros.config import get_post_task_evolution_enabled

        if not get_post_task_evolution_enabled():
            return False
        path = pathlib.Path(str(drive_root)) / _REQUEST_REL
        if not path.exists():
            return False
        try:
            req = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            # A transient/partial read should not drop the durable signal; leave
            # the file for the next tick (atomic writes make corruption unlikely).
            return False
        objective = str((req or {}).get("objective") or "").strip()
        if not objective:
            _safe_unlink(path)
            return False

        from supervisor.queue import evolution_block_reason, start_evolution_campaign
        from supervisor.state import load_state, save_state

        if evolution_block_reason():  # light runtime mode, etc.
            _safe_unlink(path)
            return False
        st = load_state()
        if not st.get("owner_chat_id"):
            # Evolution requires an owner-bound chat; without it the cycle could
            # never run. Drop the stale request rather than leaking it.
            _safe_unlink(path)
            return False
        # Per-window budget floor (V4 envelope): if configured, do not start a
        # post-task cycle unless at least that much budget remains.
        from ouroboros.config import get_post_task_evolution_budget_usd

        budget_floor = get_post_task_evolution_budget_usd()
        if budget_floor > 0:
            from supervisor.state import budget_remaining

            if budget_remaining(st) < budget_floor:
                _safe_unlink(path)
                return False
        if bool(req.get("requires_plan_review", True)):
            objective += (
                "\n\n(The source backlog item requires plan review: run plan_task "
                "before implementing any code.)"
            )
        start_evolution_campaign(objective, source="post_task")
        # Link the promoted backlog id to the campaign so close-on-commit (Phase 2 C)
        # can mark it done when the cycle is absorbed. Validate it against the OPEN
        # backlog first: never link (and later close) a hallucinated or stale id.
        backlog_id = str(req.get("backlog_id") or "").strip()
        if backlog_id:
            try:
                from ouroboros.improvement_backlog import load_backlog_items

                open_ids = {
                    str(i.get("id"))
                    for i in load_backlog_items(drive_root)
                    if str(i.get("status") or "open").lower() != "done"
                }
                if backlog_id not in open_ids:
                    backlog_id = ""
            except Exception:
                backlog_id = ""
        if backlog_id:
            try:
                from supervisor.queue import _read_evolution_campaign, _write_evolution_campaign

                camp = _read_evolution_campaign()
                camp["post_task_backlog_id"] = backlog_id
                _write_evolution_campaign(camp)
            except Exception:
                pass
        st = load_state()
        st["evolution_mode_enabled"] = True
        st["evolution_consecutive_failures"] = 0
        st["post_task_autostop"] = True
        save_state(st)
        _safe_unlink(path)
        log.info("post_task_evolution: promotion applied -> gated evolution campaign activated")
        return True
    except Exception:
        log.debug("post_task_evolution.apply_pending_request failed", exc_info=True)
        return False
