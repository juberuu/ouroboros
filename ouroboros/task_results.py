"""Helpers for durable task result/status files."""

from __future__ import annotations

import copy
import json
import logging
import pathlib
import re
from typing import Any, Callable, Dict, List, Optional

from ouroboros.utils import atomic_write_json, read_json_dict, update_json_locked, utc_now_iso

log = logging.getLogger(__name__)

STATUS_REQUESTED = "requested"
STATUS_SCHEDULED = "scheduled"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_REJECTED_DUPLICATE = "rejected_duplicate"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"
# Intent latch: the agent/owner asked to cancel, but the supervisor has not yet
# torn the task down. Ranks above running so a late running/scheduled mirror
# cannot resurrect it, but below the truly-terminal statuses so the eventual
# STATUS_CANCELLED write still lands.
STATUS_CANCEL_REQUESTED = "cancel_requested"

# Monotonic lifecycle ordering. A write that would move a task *backwards* past
# the cancel-intent latch or a terminal status is ignored, so a stale
# scheduled/running mirror can never clobber a cancel/terminal outcome
# (the "ghost subagent" class). Unknown statuses are unranked and never block.
_TRULY_TERMINAL_STATUSES = frozenset({
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_REJECTED_DUPLICATE,
})
_STATUS_RANK = {
    STATUS_REQUESTED: 0,
    STATUS_SCHEDULED: 1,
    STATUS_RUNNING: 2,
    STATUS_INTERRUPTED: 2,
    STATUS_CANCEL_REQUESTED: 3,
    STATUS_COMPLETED: 4,
    STATUS_FAILED: 4,
    STATUS_CANCELLED: 4,
    STATUS_REJECTED_DUPLICATE: 4,
}
# Regressions are only blocked once a task reaches the cancel-intent latch or a
# terminal state; normal forward progress (requested->scheduled->running) and
# unknown statuses are always allowed.
_REGRESSION_GUARD_FLOOR = _STATUS_RANK[STATUS_CANCEL_REQUESTED]

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

PLAN_REVIEW_STATE_KEY = "plan_review_state"
_PLAN_REVIEW_STATE_VERSION = 1
_PLAN_REVIEW_MAX_WAVES = 32
_PLAN_REVIEW_MAX_SCOUTS = 16
_PLAN_REVIEW_STATE_MAX_BYTES = 1_000_000
_PLAN_REVIEW_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_PLAN_REVIEW_REASON_MAX_CHARS = 2_000
_PLAN_REVIEW_PHASES = {
    "scheduling",
    "waiting",
    "evidence_ready",
    "evidence_pending",
    "reviewed",
}

# A completed payload that loses an explicit cancellation race is not a child
# result anymore.  Keep lifecycle identity, lineage, metadata, and settled cost,
# but do not leave the discarded answer/evaluation visible through the raw or
# public task-result projection.  Cancellation callers may supply replacement
# fields (for example a cancelled outcome axis or missing-artifact bundle).
_COMPLETED_PAYLOAD_FIELDS = frozenset({
    "result",
    "final_answer",
    "trace_summary",
    "trace_refs",
    "loop_outcome",
    "outcome_axes",
    "reason_code",
    "failure",
    "review_status",
    "review_evidence",
    "review_projection",
    "verification_ledger",
    "artifact_bundle",
    "artifacts",
    "artifact_status",
    "artifact_error",
    "subagent_envelope",
    "root_phase_checkpoint",
    "swarm_efficiency",
})


def cancellation_blocks_child_result(result: Any) -> bool:
    """Return whether canonical cancellation forbids child-drive promotion.

    The budget-drive lifecycle is authoritative as soon as cancel intent is
    latched. Readers and copy-back paths must consult this before touching a
    possibly late child result or its artifacts, including a custom child root
    that survived cleanup.
    """

    if not isinstance(result, dict):
        return False
    return str(result.get("status") or "").strip().lower() in {
        STATUS_CANCEL_REQUESTED,
        STATUS_CANCELLED,
    }


def resolve_task_lineage(
    task_id: Any,
    *,
    metadata: Any = None,
    root_task_id: Any = None,
    parent_task_id: Any = None,
    delegation_role: Any = None,
    original_task_id: Any = None,
    timeout_retry_from: Any = None,
) -> Dict[str, Any]:
    """Return one typed lineage projection for root-owned lifecycle gates.

    ``root_task_id`` is the logical subtree/budget authority and intentionally
    survives a top-level hard-timeout retry that receives a fresh physical
    ``task_id``.  Such a retry is a root *attempt* only when the two independent
    host-written retry markers agree.  This keeps malformed lineage fail-closed
    without splitting budget, fence, task-tree, or cost authorities.
    """

    meta = metadata if isinstance(metadata, dict) else {}

    def _field(explicit: Any, key: str) -> str:
        # ``None`` means the canonical carrier is absent.  An explicit empty
        # parent is meaningful and must override stale copied metadata.
        value = explicit if explicit is not None else meta.get(key)
        return str(value or "").strip()

    resolved_task_id = str(task_id or "").strip()
    resolved_root_id = _field(root_task_id, "root_task_id") or resolved_task_id
    resolved_parent_id = _field(parent_task_id, "parent_task_id")
    resolved_role = _field(delegation_role, "delegation_role").lower()
    resolved_original_id = _field(original_task_id, "original_task_id")
    resolved_retry_from = _field(timeout_retry_from, "timeout_retry_from")
    is_regular_root = bool(
        resolved_task_id
        and resolved_root_id == resolved_task_id
        and not resolved_parent_id
        and resolved_role != "subagent"
    )
    is_retry_root = bool(
        resolved_task_id
        and resolved_root_id
        and resolved_root_id != resolved_task_id
        and not resolved_parent_id
        and resolved_role == "root"
        and resolved_original_id
        and resolved_original_id == resolved_retry_from
        and resolved_original_id != resolved_task_id
    )
    return {
        "task_id": resolved_task_id,
        "root_task_id": resolved_root_id,
        "parent_task_id": resolved_parent_id,
        "delegation_role": resolved_role,
        "original_task_id": resolved_original_id,
        "timeout_retry_from": resolved_retry_from,
        "is_retry_root_attempt": is_retry_root,
        "is_root_task": bool(is_regular_root or is_retry_root),
    }


def _is_status_regression(existing_status: str, new_status: str) -> bool:
    """Return True when writing *new_status* over *existing_status* would
    regress or corrupt a task that has already reached cancel-intent or a
    terminal state.

    Rules:
      - Unknown statuses never block (forward-compatible).
      - Truly-terminal is sticky: once completed/failed/cancelled/rejected, only
        a same-status rewrite is allowed (result/trace enrichment). Switching to
        a *different* terminal status (e.g. cancelled -> completed) is blocked.
      - cancel-intent (cancel_requested) blocks regress to running/scheduled but
        still allows the supervisor's eventual terminal write (rank 3 -> 4).
    """
    existing = str(existing_status or "")
    new = str(new_status or "")
    # Sticky terminal FIRST — independent of whether the new status is ranked, so
    # a typo/unknown/future status can never overwrite a terminal one. Only an
    # identical-status rewrite (result/trace enrichment) is allowed.
    if existing in _TRULY_TERMINAL_STATUSES:
        return new != existing
    if existing == STATUS_CANCEL_REQUESTED:
        # Once cancellation is requested, never let a late success/duplicate (or
        # an unknown/unranked status) mask it: a worker finishing right after the
        # cancel latch must not flip the task to "completed". Allow only the real
        # teardown outcomes (cancelled/failed) or a same-status rewrite.
        return new not in (STATUS_CANCEL_REQUESTED, STATUS_CANCELLED, STATUS_FAILED)
    existing_rank = _STATUS_RANK.get(existing)
    new_rank = _STATUS_RANK.get(new)
    if existing_rank is None or new_rank is None:
        return False
    if existing_rank >= _REGRESSION_GUARD_FLOOR:
        return new_rank < existing_rank
    return False


def validate_task_id(task_id: Any) -> str:
    text = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(text):
        raise ValueError("task_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
    return text


def task_results_dir(drive_root: Any, *, create: bool = True) -> pathlib.Path:
    """Resolve ``<drive_root>/task_results``.

    ``create`` controls the mkdir side effect: WRITE callers leave it True so the
    directory exists before the write; READ/LIST callers pass ``create=False`` so a
    scan of a never-provisioned (or stubbed) root returns nothing instead of
    MATERIALISING the directory. The latter previously let an unguarded scan with a
    MagicMock-derived root create a stray ``MagicMock/.../task_results`` tree in cwd.
    """
    path = pathlib.Path(drive_root) / "task_results"
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def task_result_path(drive_root: Any, task_id: str, *, create: bool = True) -> pathlib.Path:
    return task_results_dir(drive_root, create=create) / f"{validate_task_id(task_id)}.json"


def load_task_result(drive_root: Any, task_id: str) -> Optional[Dict[str, Any]]:
    try:
        path = task_result_path(drive_root, task_id, create=False)
    except ValueError:
        return None
    return read_json_dict(path)


def list_task_results(
    drive_root: Any,
    *,
    statuses: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    wanted = {str(item) for item in list(statuses or []) if str(item).strip()}
    results: List[Dict[str, Any]] = []
    for path in sorted(task_results_dir(drive_root, create=False).glob("*.json")):
        data = read_json_dict(path)
        if data is None:
            continue
        if wanted and str(data.get("status") or "") not in wanted:
            continue
        results.append(data)
    return results


def write_task_result(
    results_drive_root: Any,
    task_id: str,
    status: str,
    *,
    _explicit_cancellation: bool = False,
    **fields: Any,
) -> Dict[str, Any]:
    """Merge-write a task result under a per-file lock.

    Worker processes, the supervisor thread, and gateway handlers all
    read-modify-write the same ``task_results/<id>.json``; the lock makes the
    monotonic-status guard evaluate the CURRENT on-disk status (closing the
    "cancel_requested latch erased by a concurrent completed write" window).
    ``_explicit_cancellation`` is the sole narrow override: an explicit cancel
    may replace a racing ``completed`` result, but no other terminal transition.
    """
    path = task_result_path(results_drive_root, task_id)
    explicit_ts = str(fields.pop("ts", "") or "")

    def _merge(existing: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # Monotonic lifecycle: never let a stale scheduled/running mirror
        # overwrite a cancel-intent latch or a terminal outcome. This is the
        # structural guard against "ghost" tasks that keep reporting
        # scheduled/running after they were cancelled or finished.
        existing_status = str(existing.get("status") or "")
        cancellation_wins_completed = bool(
            _explicit_cancellation
            and existing_status == STATUS_COMPLETED
            and status in {STATUS_CANCEL_REQUESTED, STATUS_CANCELLED}
        )
        if (
            existing
            and _is_status_regression(existing_status, status)
            and not cancellation_wins_completed
        ):
            # Surface the blocked transition: when debugging a "stuck" task this
            # is the only signal that a stale/late write was intentionally dropped.
            log.debug("Blocked status regression %s -> %s for task %s",
                      existing.get("status"), status, task_id)
            return None
        base = dict(existing)
        if cancellation_wins_completed:
            for field in _COMPLETED_PAYLOAD_FIELDS:
                base.pop(field, None)
        now = utc_now_iso()
        return {
            **base,
            **fields,
            "task_id": task_id,
            "status": status,
            "ts": explicit_ts or str(existing.get("ts") or now),
            "updated_at": now,
        }

    try:
        return update_json_locked(path, _merge)
    except TimeoutError:
        # Last-resort visibility: a wedged sibling holding the lock must not
        # silently drop a (possibly terminal) result. Log loudly and fall back
        # to the previous unlocked merge so the durable record still lands.
        log.error("task_results lock timeout for %s; falling back to unlocked merge", task_id)
        existing = load_task_result(results_drive_root, task_id) or {}
        merged = _merge(dict(existing))
        if merged is None:
            return existing
        atomic_write_json(path, merged)
        return merged


def persist_plan_review_handoffs(
    results_drive_root: Any,
    task_id: str,
    handoffs: Dict[str, Any],
) -> Dict[str, str]:
    """Atomically write the non-authoritative plan handoff audit projection."""
    try:
        artifact_dir = (
            task_results_dir(results_drive_root)
            / "artifacts"
            / validate_task_id(task_id or "plan_review")
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        path = artifact_dir / "plan_task_handoffs.json"
        incoming = {
            **copy.deepcopy(handoffs),
            "audit_only": True,
            "authoritative": False,
        }

        def _merge(existing: Dict[str, Any]) -> Dict[str, Any]:
            incoming_wait = incoming.get("wait")
            prior_wait = existing.get("wait")
            prior_tasks = prior_wait.get("tasks") if isinstance(prior_wait, dict) else None
            incoming_task_ids = incoming.get("task_ids")
            host_task_ids = {
                str(item) for item in incoming_task_ids
            } if isinstance(incoming_task_ids, list) else set()
            preserve_prior_wait = (
                isinstance(incoming_wait, dict)
                and not incoming_wait
                and incoming.get("schema_version") == existing.get("schema_version") == 1
                and bool(incoming.get("request_fingerprint"))
                and incoming.get("request_fingerprint") == existing.get("request_fingerprint")
                and existing.get("audit_only") is True
                and existing.get("authoritative") is False
                and isinstance(prior_wait, dict)
                and isinstance(prior_tasks, dict)
                and set(prior_tasks) == host_task_ids
                and all(isinstance(row, dict) for row in prior_tasks.values())
            )
            merged = copy.deepcopy(incoming)
            if preserve_prior_wait:
                merged["wait"] = copy.deepcopy(prior_wait)
            return merged

        update_json_locked(path, _merge)
        return {
            "kind": "plan_task_handoffs",
            "name": "plan_task_handoffs.json",
            "path": str(path),
        }
    except Exception as exc:
        log.debug("Failed to persist plan_task handoffs", exc_info=True)
        return {
            "kind": "plan_task_handoffs",
            "error": f"{type(exc).__name__}: {exc}",
        }


def _empty_plan_review_state() -> Dict[str, Any]:
    return {
        "schema_version": _PLAN_REVIEW_STATE_VERSION,
        "latest_review_fingerprint": "",
        "waves": [],
    }


def _validated_plan_review_state(value: Any) -> Dict[str, Any]:
    """Return a private copy of the bounded host-owned planning state."""
    if value in (None, {}):
        return _empty_plan_review_state()
    if not isinstance(value, dict) or value.get("schema_version") != _PLAN_REVIEW_STATE_VERSION:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: unsupported or malformed schema")
    waves = value.get("waves")
    if not isinstance(waves, list) or len(waves) > _PLAN_REVIEW_MAX_WAVES:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: waves must be a bounded list")
    seen: set[str] = set()
    reviewed: set[str] = set()
    for wave in waves:
        if not isinstance(wave, dict):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: wave must be an object")
        fingerprint = str(wave.get("request_fingerprint") or "")
        plan_text_hash = str(wave.get("plan_text_hash") or "")
        phase = str(wave.get("phase") or "")
        attempts = wave.get("intended_scouts")
        if not _PLAN_REVIEW_HASH_RE.fullmatch(fingerprint) or fingerprint in seen:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: wave fingerprints must be unique")
        if not _PLAN_REVIEW_HASH_RE.fullmatch(plan_text_hash):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: plan_text_hash must be SHA-256")
        if not str(wave.get("scout_cutoff_at") or "").strip():
            raise ValueError("PLAN_REVIEW_STATE_INVALID: scout_cutoff_at is required")
        if phase not in _PLAN_REVIEW_PHASES:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: invalid wave phase")
        if not isinstance(attempts, list) or len(attempts) > _PLAN_REVIEW_MAX_SCOUTS:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: intended_scouts must be a bounded list")
        roles: set[str] = set()
        issued_ids: set[str] = set()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: scout intent must be an object")
            role = str(attempt.get("role") or "").strip()
            status = str(attempt.get("schedule_status") or "")
            task_ids = attempt.get("task_ids")
            reason = str(attempt.get("schedule_reason") or "")
            if not role or role in roles:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: scout roles must be non-empty and unique")
            if status not in {"pending", "started", "failed", "unknown"}:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: invalid scout schedule status")
            if not isinstance(task_ids, list) or len(task_ids) > 1:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: one scout intent may own at most one task id")
            normalized_ids = [validate_task_id(item) for item in task_ids]
            if len(normalized_ids) != len(set(normalized_ids)) or issued_ids.intersection(normalized_ids):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: scout task ids must be unique")
            if (status == "started") != bool(normalized_ids):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: only a started scout may own task ids")
            if len(reason) > _PLAN_REVIEW_REASON_MAX_CHARS:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: scout schedule reason is too large")
            roles.add(role)
            issued_ids.update(normalized_ids)
        included = wave.get("included_task_ids")
        consumed = wave.get("consumed_task_ids")
        omissions = wave.get("omissions")
        disposition_warnings = wave.get("disposition_warnings", [])
        reviewed_result_hashes = wave.get("reviewed_result_hashes", {})
        evidence_status = str(wave.get("review_evidence_status") or "")
        if not isinstance(included, list) or not isinstance(consumed, list):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: included/consumed ids must be lists")
        included_ids = [validate_task_id(item) for item in included]
        consumed_ids = [validate_task_id(item) for item in consumed]
        if len(included_ids) != len(set(included_ids)) or not set(included_ids).issubset(issued_ids):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: included ids must be unique host-issued ids")
        if len(consumed_ids) != len(set(consumed_ids)) or not set(consumed_ids).issubset(set(included_ids)):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: consumed ids must be unique included ids")
        if not isinstance(reviewed_result_hashes, dict):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: reviewed result hashes must be an object")
        normalized_result_hashes = {
            validate_task_id(key): str(value or "")
            for key, value in reviewed_result_hashes.items()
        }
        if not set(normalized_result_hashes).issubset(set(included_ids)):
            raise ValueError(
                "PLAN_REVIEW_STATE_INVALID: reviewed result hashes must name included evidence"
            )
        if any(
            not _PLAN_REVIEW_HASH_RE.fullmatch(value)
            for value in normalized_result_hashes.values()
        ):
            raise ValueError(
                "PLAN_REVIEW_STATE_INVALID: reviewed result hashes must be SHA-256"
            )
        if evidence_status not in {"", "pending", "integrated"}:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: invalid review evidence status")
        if not isinstance(omissions, list) or len(omissions) > len(attempts):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: omissions must be bounded by scout intents")
        if any(not isinstance(item, dict) for item in omissions):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: each omission must be an object")
        if (
            not isinstance(disposition_warnings, list)
            or len(disposition_warnings) > len(attempts)
        ):
            raise ValueError(
                "PLAN_REVIEW_STATE_INVALID: disposition warnings must be bounded by scout intents"
            )
        for warning in disposition_warnings:
            if not isinstance(warning, dict) or set(warning) != {"task_id", "code", "detail"}:
                raise ValueError(
                    "PLAN_REVIEW_STATE_INVALID: disposition warning shape is invalid"
                )
            warning_task_id = validate_task_id(warning.get("task_id"))
            if warning_task_id not in included_ids:
                raise ValueError(
                    "PLAN_REVIEW_STATE_INVALID: disposition warning must name included evidence"
                )
            if str(warning.get("code") or "") != "CHILD_RESULT_STALE":
                raise ValueError(
                    "PLAN_REVIEW_STATE_INVALID: disposition warning code is invalid"
                )
            detail = str(warning.get("detail") or "")
            if not detail or len(detail) > _PLAN_REVIEW_REASON_MAX_CHARS:
                raise ValueError(
                    "PLAN_REVIEW_STATE_INVALID: disposition warning detail is invalid"
                )
        if phase in {"evidence_ready", "evidence_pending", "reviewed"}:
            if any(str(item.get("schedule_status") or "") == "pending" for item in attempts):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: collected wave has unresolved scout intent")
            included_roles = {
                str(attempt.get("role") or "")
                for attempt in attempts
                if any(task_id in included_ids for task_id in (attempt.get("task_ids") or []))
            }
            omission_roles = [str(item.get("role") or "") for item in omissions]
            expected_omissions = roles - included_roles
            if (
                len(omission_roles) != len(set(omission_roles))
                or set(omission_roles) != expected_omissions
            ):
                raise ValueError(
                    "PLAN_REVIEW_STATE_INVALID: collection must have one omission per unfulfilled scout intent"
                )
        review = wave.get("review")
        if review is not None:
            if not isinstance(review, dict):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: review must be an object")
            aggregate = str(review.get("aggregate_signal") or "")
            if str(review.get("request_fingerprint") or "") != fingerprint:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: review fingerprint does not match its wave")
            if str(review.get("plan_text_hash") or "") != plan_text_hash:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: review plan hash does not match its wave")
            if aggregate not in {"GREEN", "REVIEW_REQUIRED", "REVISE_PLAN"}:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: invalid review aggregate")
            if not isinstance(review.get("closed"), bool):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: review closed must be boolean")
            if aggregate == "GREEN" and not review["closed"]:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: GREEN review must be closed")
            if aggregate == "REVISE_PLAN" and review["closed"]:
                raise ValueError("PLAN_REVIEW_STATE_INVALID: REVISE_PLAN review cannot be closed")
            if not isinstance(review.get("findings"), list):
                raise ValueError("PLAN_REVIEW_STATE_INVALID: review findings must be a list")
            effective_evidence_status = evidence_status or "integrated"
            if effective_evidence_status == "pending":
                if phase != "evidence_pending" or not included_ids or consumed_ids:
                    raise ValueError(
                        "PLAN_REVIEW_STATE_INVALID: pending review evidence state is inconsistent"
                    )
                if set(normalized_result_hashes) != set(included_ids):
                    raise ValueError(
                        "PLAN_REVIEW_STATE_INVALID: pending review must bind every included result"
                    )
            else:
                if phase != "reviewed" or set(consumed_ids) != set(included_ids):
                    raise ValueError(
                        "PLAN_REVIEW_STATE_INVALID: reviewed evidence was not fully consumed"
                    )
                if normalized_result_hashes and set(normalized_result_hashes) != set(included_ids):
                    raise ValueError(
                        "PLAN_REVIEW_STATE_INVALID: integrated review hashes are incomplete"
                    )
                reviewed.add(fingerprint)
        elif evidence_status or reviewed_result_hashes:
            raise ValueError(
                "PLAN_REVIEW_STATE_INVALID: review evidence metadata requires a paid review"
            )
        seen.add(fingerprint)
    latest = str(value.get("latest_review_fingerprint") or "")
    if latest and latest not in reviewed:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: latest review fingerprint is unknown")
    copied = copy.deepcopy(value)
    if len(json.dumps(copied, ensure_ascii=False, default=str).encode("utf-8")) > _PLAN_REVIEW_STATE_MAX_BYTES:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: state exceeds the bounded size limit")
    return copied


def load_plan_review_state(results_drive_root: Any, task_id: str) -> Dict[str, Any]:
    path = task_result_path(results_drive_root, task_id, create=False)
    if not path.is_file():
        return _empty_plan_review_state()
    result = read_json_dict(path)
    if result is None:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: parent task result JSON is malformed")
    return _validated_plan_review_state(result.get(PLAN_REVIEW_STATE_KEY))


def _update_plan_review_state(
    results_drive_root: Any,
    task_id: str,
    mutator: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Dict[str, Any]:
    """Strict locked update; unlike lifecycle writes, planning authority has no unlocked fallback."""
    path = task_result_path(results_drive_root, task_id)

    def _merge(existing: Dict[str, Any]) -> Dict[str, Any]:
        state = _validated_plan_review_state(existing.get(PLAN_REVIEW_STATE_KEY))
        updated_state = _validated_plan_review_state(mutator(state))
        now = utc_now_iso()
        return {
            **existing,
            PLAN_REVIEW_STATE_KEY: updated_state,
            "task_id": task_id,
            "status": str(existing.get("status") or STATUS_RUNNING),
            "ts": str(existing.get("ts") or now),
            "updated_at": now,
        }

    try:
        updated = update_json_locked(path, _merge, strict_existing_dict=True)
    except ValueError as exc:
        if str(exc).startswith("update_json_locked:"):
            raise ValueError(
                "PLAN_REVIEW_STATE_INVALID: parent task result JSON is malformed"
            ) from exc
        raise
    return _validated_plan_review_state(updated.get(PLAN_REVIEW_STATE_KEY))


def plan_review_wave(state: Dict[str, Any], fingerprint: str) -> Optional[Dict[str, Any]]:
    for wave in state.get("waves") or []:
        if str(wave.get("request_fingerprint") or "") == fingerprint:
            return copy.deepcopy(wave)
    return None


def reserve_plan_review_wave(
    results_drive_root: Any,
    task_id: str,
    *,
    fingerprint: str,
    plan_text_hash: str,
    scout_roles: List[str],
    cutoff_at: str,
) -> tuple[Dict[str, Any], bool]:
    created = False

    def _reserve(state: Dict[str, Any]) -> Dict[str, Any]:
        nonlocal created
        if plan_review_wave(state, fingerprint) is not None:
            return state
        if len(state["waves"]) >= _PLAN_REVIEW_MAX_WAVES:
            raise ValueError("PLAN_REVIEW_STATE_CAPACITY_REACHED: fingerprint history is full")
        created = True
        state["waves"].append({
            "request_fingerprint": fingerprint,
            "plan_text_hash": plan_text_hash,
            "created_at": utc_now_iso(),
            "scout_cutoff_at": cutoff_at,
            "phase": "scheduling",
            "intended_scouts": [
                {"role": str(role), "schedule_status": "pending", "task_ids": [], "schedule_reason": ""}
                for role in scout_roles
            ],
            "included_task_ids": [],
            "omissions": [],
            "consumed_task_ids": [],
            "disposition_warnings": [],
        })
        return state

    state = _update_plan_review_state(results_drive_root, task_id, _reserve)
    wave = plan_review_wave(state, fingerprint)
    if wave is None:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: reserved wave is missing")
    return wave, created


def record_plan_review_scout(
    results_drive_root: Any,
    task_id: str,
    *,
    fingerprint: str,
    role: str,
    schedule_status: str,
    task_ids: List[str],
    reason: str,
) -> Dict[str, Any]:
    if schedule_status not in {"started", "failed", "unknown"}:
        raise ValueError("PLAN_REVIEW_STATE_INVALID: invalid scout schedule status")

    def _record(state: Dict[str, Any]) -> Dict[str, Any]:
        wave = next((item for item in state["waves"] if item["request_fingerprint"] == fingerprint), None)
        if wave is None:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: scout wave is missing")
        attempt = next((item for item in wave["intended_scouts"] if item.get("role") == role), None)
        if attempt is None or str(attempt.get("schedule_status") or "") != "pending":
            raise ValueError("PLAN_REVIEW_STATE_INVALID: scout attempt is missing or already resolved")
        attempt.update({
            "schedule_status": schedule_status,
            "task_ids": list(dict.fromkeys(str(item) for item in task_ids if str(item))),
            "schedule_reason": str(reason or ""),
            "scheduled_at": utc_now_iso(),
        })
        wave["phase"] = "waiting"
        return state

    state = _update_plan_review_state(results_drive_root, task_id, _record)
    return plan_review_wave(state, fingerprint) or {}


def record_plan_review_collection(
    results_drive_root: Any,
    task_id: str,
    *,
    fingerprint: str,
    included_task_ids: List[str],
    omissions: List[Dict[str, Any]],
    stop_reason: str,
) -> Dict[str, Any]:
    def _record(state: Dict[str, Any]) -> Dict[str, Any]:
        wave = next((item for item in state["waves"] if item["request_fingerprint"] == fingerprint), None)
        if wave is None:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: scout wave is missing")
        known_ids = {
            str(task_id)
            for attempt in wave["intended_scouts"]
            for task_id in (attempt.get("task_ids") or [])
            if str(task_id)
        }
        included = list(dict.fromkeys(str(item) for item in included_task_ids if str(item)))
        if not set(included).issubset(known_ids):
            raise ValueError("PLAN_REVIEW_STATE_INVALID: included scout id was not host-issued")
        wave.update({
            "phase": "evidence_ready",
            "included_task_ids": included,
            "omissions": copy.deepcopy(list(omissions)),
            "wait_stop_reason": str(stop_reason or ""),
            "collected_at": utc_now_iso(),
        })
        return state

    state = _update_plan_review_state(results_drive_root, task_id, _record)
    return plan_review_wave(state, fingerprint) or {}


def record_plan_review_consumed(
    results_drive_root: Any,
    task_id: str,
    *,
    fingerprint: str,
    consumed_task_ids: List[str],
    disposition_warnings: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    def _record(state: Dict[str, Any]) -> Dict[str, Any]:
        wave = next((item for item in state["waves"] if item["request_fingerprint"] == fingerprint), None)
        if wave is None:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: scout wave is missing")
        included = {str(item) for item in wave.get("included_task_ids") or []}
        consumed = list(dict.fromkeys(str(item) for item in consumed_task_ids if str(item)))
        if set(consumed) != included:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: consumed scouts must exactly match reviewer evidence")
        wave["consumed_task_ids"] = consumed
        wave["disposition_warnings"] = copy.deepcopy(list(disposition_warnings or []))
        wave["consumed_at"] = utc_now_iso()
        if (
            isinstance(wave.get("review"), dict)
            and str(wave.get("review_evidence_status") or "") == "pending"
        ):
            wave["review_evidence_status"] = "integrated"
            wave["phase"] = "reviewed"
            state["latest_review_fingerprint"] = fingerprint
        return state

    state = _update_plan_review_state(results_drive_root, task_id, _record)
    return plan_review_wave(state, fingerprint) or {}


def record_plan_review_result(
    results_drive_root: Any,
    task_id: str,
    *,
    fingerprint: str,
    review: Dict[str, Any],
    require_latest: bool = False,
    reviewed_result_hashes: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    def _record(state: Dict[str, Any]) -> Dict[str, Any]:
        wave = next((item for item in state["waves"] if item["request_fingerprint"] == fingerprint), None)
        if wave is None:
            raise ValueError("PLAN_REVIEW_STATE_INVALID: reviewed wave is missing")
        if require_latest and str(state.get("latest_review_fingerprint") or "") != fingerprint:
            raise ValueError("PLAN_REVIEW_DISPOSITION_STALE: review is not immediately preceding")
        existing_review = wave.get("review") if isinstance(wave.get("review"), dict) else {}
        if existing_review.get("closed"):
            existing_comparable = copy.deepcopy(existing_review)
            incoming_comparable = copy.deepcopy(review)
            for comparable in (existing_comparable, incoming_comparable):
                disposition = comparable.get("disposition")
                if isinstance(disposition, dict):
                    disposition.pop("recorded_at", None)
            if existing_comparable != incoming_comparable:
                raise ValueError(
                    "PLAN_REVIEW_DISPOSITION_IMMUTABLE: a closed review cannot be changed"
                )
            if reviewed_result_hashes is not None and dict(
                wave.get("reviewed_result_hashes") or {}
            ) != dict(reviewed_result_hashes):
                raise ValueError(
                    "PLAN_REVIEW_DISPOSITION_IMMUTABLE: reviewed evidence hashes cannot change"
                )
            return state
        wave["review"] = copy.deepcopy(review)
        if reviewed_result_hashes is not None:
            included = {str(item) for item in wave.get("included_task_ids") or []}
            normalized_hashes = {
                str(key): str(value or "")
                for key, value in reviewed_result_hashes.items()
            }
            if set(normalized_hashes) != included:
                raise ValueError(
                    "PLAN_REVIEW_STATE_INVALID: paid review hashes must exactly match included evidence"
                )
            wave["reviewed_result_hashes"] = normalized_hashes
            if included:
                wave["review_evidence_status"] = "pending"
                wave["phase"] = "evidence_pending"
            else:
                wave["review_evidence_status"] = "integrated"
                wave["phase"] = "reviewed"
                state["latest_review_fingerprint"] = fingerprint
        else:
            wave["review_evidence_status"] = "integrated"
            wave["phase"] = "reviewed"
        if not require_latest and reviewed_result_hashes is None:
            state["latest_review_fingerprint"] = fingerprint
        return state

    state = _update_plan_review_state(results_drive_root, task_id, _record)
    return plan_review_wave(state, fingerprint) or {}


def represent_plan_review(
    results_drive_root: Any,
    task_id: str,
    *,
    fingerprint: str,
) -> Dict[str, Any]:
    """Make an older open REVIEW_REQUIRED result the immediately preceding review."""

    def _represent(state: Dict[str, Any]) -> Dict[str, Any]:
        wave = next((item for item in state["waves"] if item["request_fingerprint"] == fingerprint), None)
        review = wave.get("review") if isinstance((wave or {}).get("review"), dict) else {}
        if (
            not review
            or str(review.get("aggregate_signal") or "") != "REVIEW_REQUIRED"
            or bool(review.get("closed"))
            or str((wave or {}).get("review_evidence_status") or "") == "pending"
        ):
            raise ValueError(
                "PLAN_REVIEW_REPRESENT_INVALID: only an open REVIEW_REQUIRED review can be represented"
            )
        state["latest_review_fingerprint"] = fingerprint
        return state

    state = _update_plan_review_state(results_drive_root, task_id, _represent)
    return plan_review_wave(state, fingerprint) or {}


def plan_review_wave_task_ids(wave: Dict[str, Any]) -> List[str]:
    return list(dict.fromkeys(
        str(task_id)
        for attempt in wave.get("intended_scouts") or []
        for task_id in (attempt.get("task_ids") or [])
        if str(task_id)
    ))


def plan_review_audit_only_task_ids(state: Dict[str, Any]) -> List[str]:
    """Return every scout id whose exact plan review is already authoritative."""
    task_ids: List[str] = []
    for wave in state.get("waves") or []:
        if not isinstance(wave.get("review"), dict):
            continue
        for task_id in plan_review_wave_task_ids(wave):
            if task_id not in task_ids:
                task_ids.append(task_id)
    return task_ids


def plan_review_wave_handoffs(wave: Dict[str, Any]) -> Dict[str, Any]:
    """Build the public audit projection only from host-owned wave state."""
    return {
        "schema_version": 1,
        "ts": str(wave.get("collected_at") or wave.get("created_at") or utc_now_iso()),
        "request_fingerprint": str(wave.get("request_fingerprint") or ""),
        "task_ids": plan_review_wave_task_ids(wave),
        "schedule_outputs": [str(item.get("schedule_reason") or "") for item in wave.get("intended_scouts") or []],
        "scout_cutoff_at": str(wave.get("scout_cutoff_at") or ""),
        "wait": {},
        "wait_stop_reason": str(wave.get("wait_stop_reason") or ""),
        "included_task_ids": list(wave.get("included_task_ids") or []),
        "omissions": copy.deepcopy(list(wave.get("omissions") or [])),
        "consumed_task_ids": list(wave.get("consumed_task_ids") or []),
        "disposition_warnings": copy.deepcopy(list(wave.get("disposition_warnings") or [])),
        **({"review": copy.deepcopy(wave["review"])} if isinstance(wave.get("review"), dict) else {}),
    }


def fail_tasks(results_drive_root: Any, tasks: Any, *, reason_code: str, result: str) -> int:
    """Terminally FAIL a batch of queued tasks (e.g. on budget exhaustion) so their
    waiters get an observable result instead of hanging. Returns the count written."""
    written = 0
    for task in tasks or []:
        tid = str((task or {}).get("id") or "")
        if not tid:
            continue
        # Write to the task's CANONICAL status root: forked/workspace/subagent children
        # use budget_drive_root, so the waiter reading THAT root sees the result (a child
        # outside results_drive_root would otherwise keep hanging — the bug this fixes).
        root = (task or {}).get("budget_drive_root") or results_drive_root
        try:
            # Honor a pending cancel request: terminalize as CANCELLED (the right reason),
            # not as budget_exhausted — the budget drain must not relabel a cancellation.
            existing = load_task_result(root, tid) or {}
            if str(existing.get("status") or "") == STATUS_CANCEL_REQUESTED:
                write_task_result(root, tid, STATUS_CANCELLED, result="Cancelled before start.")
            else:
                write_task_result(root, tid, STATUS_FAILED, reason_code=reason_code, result=result)
            written += 1
        except Exception:
            log.debug("fail_tasks: could not fail %s", tid, exc_info=True)
    return written
