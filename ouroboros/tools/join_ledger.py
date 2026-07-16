"""D#7 soft join-ledger tools (extracted from control.py to keep it under the module
size gate). The parent's explicit, structured (P5 — not parsed-from-prose) controls for
not orphaning spawned subagent children:

  - peek_task: inspect a child's status / latest beacons / result tail (a PURE READ —
    makes no finalization decision and does not alter the change-based handoff reminder).
  - discard_child_result: explicitly abandon a child's result (stamps a durable
    parent_decision the pre-finalization reminder honors), lineage-gated to OWN children.

The shared lineage/ledger helpers (_status_drive_root, _is_own_child,
_record_child_decision_beacon) and the cancel_task handler (moved here from control.py,
upgraded with a recorded reason + lineage gate) live here too.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterable

from ouroboros.task_results import (
    STATUS_CANCELLED,
    load_task_result,
    task_result_path,
    validate_task_id,
    write_task_result,
)
from ouroboros.task_status import effective_task_result, load_effective_task_result
from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import update_json_locked, utc_now_iso

log = logging.getLogger("ouroboros.tools.join_ledger")

CHILD_RESULT_DISPOSITIONS = frozenset({"integrated", "irrelevant", "deferred"})
CHILD_RESULT_DISPOSITION_TYPE = "child_result_disposition"
_CHILD_RESULT_DISPOSITION_FIELDS = frozenset({
    "type",
    "child_task_id",
    "disposition",
    "child_result_sha256",
})
_CHILD_DISPOSITION_BEACON_STATE_FIELD = "child_result_disposition_beacon_state"
_CHILD_DISPOSITION_BEACON_SHA_FIELD = "child_result_disposition_beacon_sha256"
_CHILD_DISPOSITION_BEACON_PENDING = "pending"
_CHILD_DISPOSITION_BEACON_CONFIRMED = "confirmed"
_CHILD_DISPOSITION_CONTROL_FIELDS = (
    "child_result_disposition",
    "child_result_disposition_sha256",
    "child_result_disposition_reason",
    "child_result_disposition_source",
    _CHILD_DISPOSITION_BEACON_STATE_FIELD,
    _CHILD_DISPOSITION_BEACON_SHA_FIELD,
    "parent_decision",
    "parent_decision_reason",
    "parent_decision_child_result_sha256",
)
_ARTIFACT_IDENTITY_FIELDS = (
    "id",
    "artifact_id",
    "kind",
    "name",
    "relpath",
    "sha256",
    "status",
)
_TERMINAL_CHILD_RESULT_SNAPSHOT_FIELD = "terminal_child_result_snapshot"
_TERMINAL_CHILD_RESULT_SNAPSHOT_FIELDS = frozenset({
    "status",
    "result",
    "trace_summary",
    "artifact_status",
    "artifacts",
})
# A late cancellation result is already represented by the authoritative
# top-level lifecycle.  Only a different terminal semantic outcome contains new
# work that the parent still has to disposition.
_TERMINAL_CHILD_RESULT_STATUSES = frozenset({
    "completed",
    "failed",
    "rejected_duplicate",
})


def _stable_artifact_identities(result: Dict[str, Any]) -> list[Dict[str, Any]]:
    """Return stable artifact identities without volatile paths/timestamps."""

    candidates: list[Any] = []
    if isinstance(result.get("artifacts"), list):
        candidates.extend(result.get("artifacts") or [])
    bundle = result.get("artifact_bundle") if isinstance(result.get("artifact_bundle"), dict) else {}
    if isinstance(bundle.get("artifacts"), list):
        candidates.extend(bundle.get("artifacts") or [])
    identities: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, str) and item.strip():
            item = {"path": item}
        if not isinstance(item, dict):
            continue
        identity = {
            key: item.get(key)
            for key in _ARTIFACT_IDENTITY_FIELDS
            if item.get(key) not in (None, "")
        }
        raw_path = str(item.get("path") or item.get("abs_path") or "").strip()
        if raw_path:
            artifact_path = Path(raw_path)
            if not artifact_path.is_absolute():
                identity.setdefault("relpath", artifact_path.as_posix())
            if "name" not in identity:
                identity["name"] = Path(raw_path).name
        if not identity:
            continue
        encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str)
        if encoded in seen:
            continue
        seen.add(encoded)
        identities.append(identity)
    return sorted(
        identities,
        key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True, default=str),
    )


def _terminal_child_result_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    """Return a validated fixed-field late child-result snapshot, if present.

    The durable task lifecycle remains ``cancelled``.  This nested projection is
    intentionally limited to the semantic fields a parent disposition hashes;
    accounting, timestamps, queue diagnostics, and parent decisions never enter
    it.  The full result and trace are preserved rather than silently clipped.
    """

    raw = result.get(_TERMINAL_CHILD_RESULT_SNAPSHOT_FIELD)
    if not isinstance(raw, dict) or set(raw) != _TERMINAL_CHILD_RESULT_SNAPSHOT_FIELDS:
        return {}
    status = str(raw.get("status") or "").strip().lower()
    if status not in _TERMINAL_CHILD_RESULT_STATUSES:
        return {}
    artifacts = raw.get("artifacts")
    if not isinstance(artifacts, list):
        return {}
    return {
        "status": status,
        "result": raw.get("result"),
        "trace_summary": raw.get("trace_summary"),
        "artifact_status": str(raw.get("artifact_status") or ""),
        "artifacts": _stable_artifact_identities({"artifacts": artifacts}),
    }


def _build_terminal_child_result_snapshot(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the fixed semantic snapshot from a terminal child-drive result."""

    status = str(result.get("status") or "").strip().lower()
    if status not in _TERMINAL_CHILD_RESULT_STATUSES:
        return {}
    bundle = result.get("artifact_bundle") if isinstance(result.get("artifact_bundle"), dict) else {}
    snapshot = {
        "status": status,
        "result": result.get("result"),
        "trace_summary": result.get("trace_summary"),
        "artifact_status": str(result.get("artifact_status") or bundle.get("status") or ""),
        "artifacts": _stable_artifact_identities(result),
    }
    return _terminal_child_result_snapshot({_TERMINAL_CHILD_RESULT_SNAPSHOT_FIELD: snapshot})


def _read_terminal_child_result_snapshot(
    task_id: str,
    records: Iterable[Dict[str, Any]],
    child_drive_roots: Iterable[Path] = (),
) -> tuple[str, Dict[str, Any]]:
    """Read one stable terminal semantic result from bounded child-drive paths."""

    candidates: list[Path] = [Path(root) for root in child_drive_roots]
    for record in records:
        if not isinstance(record, dict):
            continue
        metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
        for source in (record, metadata):
            for key in ("drive_root", "child_drive_root", "headless_child_drive_root"):
                raw = str(source.get(key) or "").strip()
                if raw:
                    candidates.append(Path(raw))

    snapshots: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for child_drive in candidates:
        identity = str(child_drive.resolve(strict=False))
        if identity in seen:
            continue
        seen.add(identity)
        result_path = child_drive / "task_results" / f"{task_id}.json"
        if not result_path.exists():
            continue
        child_result = load_task_result(child_drive, task_id)
        if not isinstance(child_result, dict):
            return "unavailable", {}
        snapshot = _build_terminal_child_result_snapshot(child_result)
        if snapshot:
            snapshots.append(snapshot)
    if not snapshots:
        return "absent", {}
    if any(snapshot != snapshots[0] for snapshot in snapshots[1:]):
        return "unavailable", {}
    return "terminal", snapshots[0]


def _preserve_cancelled_child_terminal_snapshot(
    parent_drive_root: Path,
    task_id: str,
    task: Dict[str, Any] | None = None,
    *,
    child_drive_roots: Iterable[Path] = (),
) -> str:
    """Strictly preserve late child semantics while lifecycle stays cancelled."""

    try:
        tid = validate_task_id(task_id)
        canonical = load_task_result(parent_drive_root, tid) or {}
        supplied = task if isinstance(task, dict) else {}
        read_state, snapshot = _read_terminal_child_result_snapshot(
            tid,
            (canonical, supplied),
            child_drive_roots,
        )
        if read_state == "absent":
            return "absent"
        if read_state != "terminal" or not snapshot:
            return "failed"
        metadata = canonical.get("metadata") if isinstance(canonical.get("metadata"), dict) else {}
        supplied_meta = supplied.get("metadata") if isinstance(supplied.get("metadata"), dict) else {}
        role = str(
            canonical.get("delegation_role")
            or metadata.get("delegation_role")
            or supplied.get("delegation_role")
            or supplied_meta.get("delegation_role")
            or ""
        )
        if role != "subagent":
            return "not_applicable"
        if str(canonical.get("status") or "") != STATUS_CANCELLED:
            return "failed"
        write_task_result(
            parent_drive_root,
            tid,
            STATUS_CANCELLED,
            **{_TERMINAL_CHILD_RESULT_SNAPSHOT_FIELD: snapshot},
        )
        durable = load_task_result(parent_drive_root, tid) or {}
        if (
            str(durable.get("status") or "") == STATUS_CANCELLED
            and _terminal_child_result_snapshot(durable) == snapshot
        ):
            return "persisted"
    except Exception:
        log.debug("Failed to preserve late terminal child snapshot for %s", task_id, exc_info=True)
    return "failed"


def _cancelled_child_drive_removal_allowed(
    parent_drive_root: Path,
    task_id: str,
    child_drive_roots: Iterable[Path],
) -> bool:
    """Allow deletion only after no late result exists or its snapshot is durable."""

    state = _preserve_cancelled_child_terminal_snapshot(
        parent_drive_root,
        task_id,
        {"delegation_role": "subagent"},
        child_drive_roots=child_drive_roots,
    )
    if state in {"absent", "persisted"}:
        return True
    log.error("Retaining child drive for cancelled task %s: snapshot=%s", task_id, state)
    return False


def _child_result_sha256(result: Dict[str, Any]) -> str:
    """Hash the exact semantic child result consumed by a parent decision.

    Cost, timestamps, queue diagnostics, and parent-decision fields are omitted by
    construction. A content/status/artifact change therefore invalidates a prior
    disposition, while accounting or coordination telemetry does not.
    """

    snapshot = _terminal_child_result_snapshot(result)
    semantic_result = snapshot or result
    bundle = (
        semantic_result.get("artifact_bundle")
        if isinstance(semantic_result.get("artifact_bundle"), dict)
        else {}
    )
    payload = {
        "status": str(semantic_result.get("status") or ""),
        "result": semantic_result.get("result"),
        "trace_summary": semantic_result.get("trace_summary"),
        "artifact_status": str(semantic_result.get("artifact_status") or bundle.get("status") or ""),
        "artifacts": _stable_artifact_identities(semantic_result),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _effective_child_result_for_cas(
    status_drive_root: Path,
    current: Dict[str, Any],
) -> Dict[str, Any]:
    """Project the exact locked task JSON through the normal semantic reader.

    Artifact identities can exist only in the bounded artifact store and are
    therefore added by ``effective_task_result`` rather than persisted in the
    raw task JSON.  Both disposition CAS phases must still compare the same
    artifact-inclusive hash shown to the parent, while lineage and the control
    tuple remain comparisons against the exact locked ``current`` mapping.
    """

    return effective_task_result(Path(status_drive_root), current)


def _child_disposition_beacon_payload(
    *,
    child_task_id: str,
    disposition: str,
    child_result_sha256: str,
) -> Dict[str, str]:
    return {
        "type": CHILD_RESULT_DISPOSITION_TYPE,
        "child_task_id": str(child_task_id),
        "disposition": str(disposition),
        "child_result_sha256": str(child_result_sha256),
    }


def _child_disposition_beacon_binding_sha256(
    *,
    root_task_id: str,
    parent_task_id: str,
    child_task_id: str,
    disposition: str,
    child_result_sha256: str,
    rationale: str,
) -> str:
    """Bind a confirmation receipt to the exact typed tree-ledger row."""

    identity = {
        "root_task_id": str(root_task_id),
        "parent_task_id": str(parent_task_id),
        "kind": "decision",
        "text": str(rationale),
        "payload": _child_disposition_beacon_payload(
            child_task_id=child_task_id,
            disposition=disposition,
            child_result_sha256=child_result_sha256,
        ),
    }
    encoded = json.dumps(
        identity,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _child_disposition_lineage(result: Dict[str, Any]) -> tuple[str, str, str]:
    metadata = result.get("metadata") if isinstance(result.get("metadata"), dict) else {}
    child_task_id = str(result.get("task_id") or result.get("id") or "").strip()
    parent_task_id = str(
        result.get("parent_task_id") or metadata.get("parent_task_id") or ""
    ).strip()
    root_task_id = str(
        result.get("root_task_id") or metadata.get("root_task_id") or parent_task_id
    ).strip()
    return root_task_id, parent_task_id, child_task_id


def _current_child_result_disposition(result: Dict[str, Any]) -> str:
    """Return a current disposition only after its exact beacon was confirmed.

    ``pending`` (including legacy half-states with no beacon state) never closes
    child absorption.  The confirmation digest binds the host receipt to the
    precise root, parent, child, rationale, disposition, and child-result hash.
    """

    disposition = str(
        result.get("child_result_disposition")
        or (
            result.get("parent_decision")
            if str(result.get("parent_decision") or "").strip().lower() in CHILD_RESULT_DISPOSITIONS
            else ""
        )
        or ""
    ).strip().lower()
    expected = str(
        result.get("child_result_disposition_sha256")
        or result.get("parent_decision_child_result_sha256")
        or ""
    ).strip().lower()
    if disposition not in CHILD_RESULT_DISPOSITIONS or len(expected) != 64:
        return ""
    if expected != _child_result_sha256(result):
        return ""
    if str(result.get(_CHILD_DISPOSITION_BEACON_STATE_FIELD) or "") != _CHILD_DISPOSITION_BEACON_CONFIRMED:
        return ""
    receipt_sha256 = str(result.get(_CHILD_DISPOSITION_BEACON_SHA_FIELD) or "").strip().lower()
    if len(receipt_sha256) != 64:
        return ""
    rationale = str(result.get("child_result_disposition_reason") or "")
    root_task_id, parent_task_id, child_task_id = _child_disposition_lineage(result)
    if not all((root_task_id, parent_task_id, child_task_id, rationale)):
        return ""
    expected_receipt = _child_disposition_beacon_binding_sha256(
        root_task_id=root_task_id,
        parent_task_id=parent_task_id,
        child_task_id=child_task_id,
        disposition=disposition,
        child_result_sha256=expected,
        rationale=rationale,
    )
    return disposition if receipt_sha256 == expected_receipt else ""


def _child_disposition_beacon_exists(
    ctx: ToolContext,
    *,
    child_task_id: str,
    disposition: str,
    child_result_sha256: str,
    rationale: str,
) -> bool:
    """Return True only for the exact typed row emitted by this parent."""

    try:
        from ouroboros.task_tree_ledger import tree_ledger_rows
        from ouroboros.tools.task_tree import tree_root_id

        rid = tree_root_id(ctx)
        parent_task_id = str(getattr(ctx, "task_id", "") or "").strip()
        if not rid or not parent_task_id:
            return False
        expected_payload = _child_disposition_beacon_payload(
            child_task_id=child_task_id,
            disposition=disposition,
            child_result_sha256=child_result_sha256,
        )
        return any(
            str(row.get("kind") or "") == "decision"
            and str(row.get("task_id") or "") == parent_task_id
            and str(row.get("text") or "") == rationale
            and row.get("payload") == expected_payload
            for row in tree_ledger_rows(rid)
            if isinstance(row, dict)
        )
    except Exception:
        log.debug(
            "Failed to inspect child disposition beacon for %s",
            child_task_id,
            exc_info=True,
        )
        return False


def _append_child_disposition_beacon(
    ctx: ToolContext,
    *,
    child_task_id: str,
    disposition: str,
    child_result_sha256: str,
    rationale: str,
) -> str:
    """Ensure the exact typed decision is durably observable, without duplicates."""

    try:
        from ouroboros.tools.task_tree import tree_root_id
        from ouroboros.task_tree_ledger import tree_ledger_append

        rid = tree_root_id(ctx)
        if not rid:
            return "⚠️ CHILD_RESULT_DISPOSITION_BEACON_FAILED: task-tree root is unavailable."
        if _child_disposition_beacon_exists(
            ctx,
            child_task_id=child_task_id,
            disposition=disposition,
            child_result_sha256=child_result_sha256,
            rationale=rationale,
        ):
            return "OK: exact child-result disposition beacon is already present."
        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        role = str(meta.get("role") or getattr(ctx, "role", "") or "")
        appended = tree_ledger_append(
            rid,
            "decision",
            rationale,
            task_id=str(getattr(ctx, "task_id", "") or ""),
            role=role,
            allow_child_result_disposition=True,
            payload=_child_disposition_beacon_payload(
                child_task_id=child_task_id,
                disposition=disposition,
                child_result_sha256=child_result_sha256,
            ),
        )
        if not appended.startswith("OK:"):
            return appended
        if not _child_disposition_beacon_exists(
            ctx,
            child_task_id=child_task_id,
            disposition=disposition,
            child_result_sha256=child_result_sha256,
            rationale=rationale,
        ):
            return (
                "⚠️ CHILD_RESULT_DISPOSITION_BEACON_FAILED: the exact tree-ledger "
                "decision was not observable after append."
            )
        return appended
    except Exception:
        log.debug("Failed to append child disposition beacon for %s", child_task_id, exc_info=True)
        return "⚠️ CHILD_RESULT_DISPOSITION_BEACON_FAILED: tree-ledger append raised."


def _child_disposition_control_tuple(result: Dict[str, Any]) -> tuple[tuple[str, bool, Any], ...]:
    """Exact pre-write control tuple, preserving absent-vs-null distinctions."""

    return tuple(
        (field, field in result, result.get(field))
        for field in _CHILD_DISPOSITION_CONTROL_FIELDS
    )


def _compare_and_prepare_child_disposition(
    status_drive_root: Path,
    child_task_id: str,
    *,
    root_task_id: str,
    parent_task_id: str,
    child_result_sha256: str,
    prior_control_tuple: tuple[tuple[str, bool, Any], ...],
    pending_fields: Dict[str, Any],
) -> tuple[bool, str, Dict[str, Any]]:
    """Strict-CAS the caller's exact prior control tuple into ``pending``."""

    state = {"reason": "control_tuple_replaced", "prepared": False}
    path = task_result_path(status_drive_root, child_task_id)

    def _prepare(current: Dict[str, Any]) -> Dict[str, Any] | None:
        if (
            _child_result_sha256(
                _effective_child_result_for_cas(status_drive_root, current)
            )
            != child_result_sha256
        ):
            state["reason"] = "child_result_changed"
            return None
        if _child_disposition_lineage(current) != (
            root_task_id,
            parent_task_id,
            child_task_id,
        ):
            state["reason"] = "lineage_changed"
            return None
        if _child_disposition_control_tuple(current) != prior_control_tuple:
            state["reason"] = "control_tuple_replaced"
            return None
        state["prepared"] = True
        state["reason"] = "pending"
        return {
            **current,
            **pending_fields,
            "updated_at": utc_now_iso(),
        }

    updated = update_json_locked(path, _prepare, strict_existing_dict=True)
    return bool(state["prepared"]), str(state["reason"]), updated


def _compare_and_set_child_disposition_beacon_state(
    status_drive_root: Path,
    child_task_id: str,
    *,
    root_task_id: str,
    parent_task_id: str,
    child_result_sha256: str,
    expected_fields: Dict[str, Any],
    target_state: str,
) -> tuple[bool, str, Dict[str, Any]]:
    """Atomically transition the exact disposition tuple under its existing lock.

    The beacon append happens outside this JSON lock. Promotion therefore acts
    as the commit point: a concurrent child result or competing parent-decision
    tuple makes the compare fail, and the stale caller cannot overwrite it with
    ``confirmed``.
    """

    state = {"reason": "pending_tuple_replaced", "promoted": False}
    path = task_result_path(status_drive_root, child_task_id)

    def _promote(current: Dict[str, Any]) -> Dict[str, Any] | None:
        if (
            _child_result_sha256(
                _effective_child_result_for_cas(status_drive_root, current)
            )
            != child_result_sha256
        ):
            state["reason"] = "child_result_changed"
            return None
        if _child_disposition_lineage(current) != (
            root_task_id,
            parent_task_id,
            child_task_id,
        ):
            state["reason"] = "lineage_changed"
            return None
        if any(current.get(key) != value for key, value in expected_fields.items()):
            state["reason"] = "pending_tuple_replaced"
            return None
        state["promoted"] = True
        state["reason"] = target_state
        return {
            **current,
            _CHILD_DISPOSITION_BEACON_STATE_FIELD: target_state,
            "updated_at": utc_now_iso(),
        }

    updated = update_json_locked(path, _promote, strict_existing_dict=True)
    return bool(state["promoted"]), str(state["reason"]), updated


def _record_child_result_disposition(
    ctx: ToolContext,
    payload: Dict[str, Any],
    rationale: str,
    *,
    source: str = "tree_note",
    legacy_parent_decision: str = "",
) -> str:
    """Validate lineage/hash and durably bind a parent disposition.

    This is the sole write authority for the typed disposition. Tagged malformed
    payloads fail before either the task result or blackboard can change.
    """

    if not isinstance(payload, dict) or set(payload) != _CHILD_RESULT_DISPOSITION_FIELDS:
        return (
            "⚠️ CHILD_RESULT_DISPOSITION_INVALID: payload must contain exactly "
            "type, child_task_id, disposition, and child_result_sha256."
        )
    if str(payload.get("type") or "") != CHILD_RESULT_DISPOSITION_TYPE:
        return "⚠️ CHILD_RESULT_DISPOSITION_INVALID: payload.type must be child_result_disposition."
    try:
        tid = validate_task_id(payload.get("child_task_id"))
    except ValueError as exc:
        return f"⚠️ CHILD_RESULT_DISPOSITION_INVALID: {exc}"
    disposition = str(payload.get("disposition") or "").strip().lower()
    if disposition not in CHILD_RESULT_DISPOSITIONS:
        return (
            "⚠️ CHILD_RESULT_DISPOSITION_INVALID: disposition must be one of "
            "integrated, irrelevant, or deferred."
        )
    expected = str(payload.get("child_result_sha256") or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        return "⚠️ CHILD_RESULT_DISPOSITION_INVALID: child_result_sha256 must be a full SHA-256 hex digest."
    reason_text = " ".join(str(rationale or "").split())
    if not reason_text:
        return "⚠️ CHILD_RESULT_DISPOSITION_INVALID: tree_note text is required as rationale."
    if len(reason_text) > 500:
        return (
            "⚠️ CHILD_RESULT_DISPOSITION_INVALID: tree_note rationale must be "
            "at most 500 characters."
        )

    status_drive_root = _status_drive_root(ctx)
    if not _is_own_child(ctx, status_drive_root, tid):
        return (
            f"⚠️ CHILD_RESULT_LINEAGE_FORBIDDEN: {tid} is not a direct child of this task; "
            "no disposition was recorded."
        )
    data = load_effective_task_result(status_drive_root, tid) or {}
    if not data:
        return f"⚠️ CHILD_RESULT_STALE: {tid} has no current result to bind."
    actual = _child_result_sha256(data)
    if actual != expected:
        return (
            f"⚠️ CHILD_RESULT_STALE: {tid} changed (expected {expected[:12]}, current "
            f"{actual[:12]}); inspect it again and submit the current hash."
        )

    from ouroboros.tools.task_tree import tree_root_id

    root_task_id = tree_root_id(ctx)
    parent_task_id = str(getattr(ctx, "task_id", "") or "").strip()
    data_root_id, data_parent_id, data_child_id = _child_disposition_lineage(data)
    if (
        not root_task_id
        or not parent_task_id
        or (data_root_id, data_parent_id, data_child_id)
        != (root_task_id, parent_task_id, tid)
    ):
        return (
            f"⚠️ CHILD_RESULT_LINEAGE_FORBIDDEN: {tid} does not carry the exact "
            "root/parent lineage required for a tree-ledger disposition."
        )
    beacon_sha256 = _child_disposition_beacon_binding_sha256(
        root_task_id=root_task_id,
        parent_task_id=parent_task_id,
        child_task_id=tid,
        disposition=disposition,
        child_result_sha256=expected,
        rationale=reason_text,
    )
    exact_existing = (
        _current_child_result_disposition(data) == disposition
        and str(data.get("child_result_disposition_reason") or "") == reason_text
        and str(data.get(_CHILD_DISPOSITION_BEACON_SHA_FIELD) or "") == beacon_sha256
        and (
            not legacy_parent_decision
            or str(data.get("parent_decision") or "") == str(legacy_parent_decision)
        )
    )
    if exact_existing:
        return (
            f"OK: child {tid} is already marked {disposition} for result {expected[:12]} "
            "(idempotent; exact confirmed receipt present)."
        )
    prior_control_tuple = _child_disposition_control_tuple(data)
    fields: Dict[str, Any] = {
        "child_result_disposition": disposition,
        "child_result_disposition_sha256": expected,
        "child_result_disposition_reason": reason_text,
        "child_result_disposition_source": str(source or "tree_note"),
        _CHILD_DISPOSITION_BEACON_STATE_FIELD: _CHILD_DISPOSITION_BEACON_PENDING,
        _CHILD_DISPOSITION_BEACON_SHA_FIELD: beacon_sha256,
    }
    if legacy_parent_decision:
        fields.update(
            parent_decision=str(legacy_parent_decision),
            parent_decision_reason=reason_text,
        )
    try:
        prepared, prepare_reason, current = _compare_and_prepare_child_disposition(
            status_drive_root,
            tid,
            root_task_id=root_task_id,
            parent_task_id=parent_task_id,
            child_result_sha256=expected,
            prior_control_tuple=prior_control_tuple,
            pending_fields=fields,
        )
    except Exception:
        log.debug("Failed to persist child disposition for %s", tid, exc_info=True)
        return f"⚠️ CHILD_RESULT_DISPOSITION_WRITE_FAILED: failed to record disposition for {tid}."
    if not prepared:
        if prepare_reason == "child_result_changed":
            return (
                f"⚠️ CHILD_RESULT_STALE: {tid} changed before its disposition could "
                "enter the pending phase."
            )
        if prepare_reason == "lineage_changed":
            return (
                f"⚠️ CHILD_RESULT_LINEAGE_FORBIDDEN: {tid} lineage changed before "
                "its disposition could enter the pending phase."
            )
        return (
            f"⚠️ CHILD_RESULT_DISPOSITION_WRITE_FAILED: {tid} received a competing "
            "disposition before prepare; the stale caller did not overwrite it."
        )

    # Phase 1 is intentionally non-authoritative. A crash or append failure after
    # this write leaves ``pending`` on disk, which cannot close child absorption.
    current = _effective_child_result_for_cas(status_drive_root, current)
    if _child_result_sha256(current) != expected:
        return (
            f"⚠️ CHILD_RESULT_STALE: {tid} changed while the disposition was being recorded; "
            "the stored decision is stale and does not close the new result."
        )
    if (
        str(current.get("child_result_disposition") or "") != disposition
        or str(current.get("child_result_disposition_sha256") or "") != expected
        or str(current.get("child_result_disposition_reason") or "") != reason_text
        or str(current.get(_CHILD_DISPOSITION_BEACON_STATE_FIELD) or "")
        != _CHILD_DISPOSITION_BEACON_PENDING
        or str(current.get(_CHILD_DISPOSITION_BEACON_SHA_FIELD) or "") != beacon_sha256
    ):
        return (
            f"⚠️ CHILD_RESULT_DISPOSITION_WRITE_FAILED: the pending exact disposition "
            f"for {tid} was not present after persistence."
        )
    ledger_result = _append_child_disposition_beacon(
        ctx,
        child_task_id=tid,
        disposition=disposition,
        child_result_sha256=expected,
        rationale=reason_text,
    )
    if not ledger_result.startswith("OK:"):
        return ledger_result

    # Re-check semantic freshness after the append. An old beacon may remain as
    # audit evidence, but it cannot confirm a result that changed concurrently.
    current = load_effective_task_result(status_drive_root, tid) or {}
    if _child_result_sha256(current) != expected:
        return (
            f"⚠️ CHILD_RESULT_STALE: {tid} changed while its tree beacon was recorded; "
            "the pending disposition does not close the new result."
        )
    try:
        promoted, promote_reason, current = _compare_and_set_child_disposition_beacon_state(
            status_drive_root,
            tid,
            root_task_id=root_task_id,
            parent_task_id=parent_task_id,
            child_result_sha256=expected,
            expected_fields=fields,
            target_state=_CHILD_DISPOSITION_BEACON_CONFIRMED,
        )
    except Exception:
        log.debug("Failed to confirm child disposition for %s", tid, exc_info=True)
        return (
            f"⚠️ CHILD_RESULT_DISPOSITION_WRITE_FAILED: the tree beacon exists, "
            f"but confirmation for {tid} was not persisted."
        )
    if not promoted:
        if promote_reason == "child_result_changed":
            return (
                f"⚠️ CHILD_RESULT_STALE: {tid} changed before its exact pending "
                "disposition could be confirmed."
            )
        if promote_reason == "lineage_changed":
            return (
                f"⚠️ CHILD_RESULT_LINEAGE_FORBIDDEN: {tid} lineage changed before "
                "its exact pending disposition could be confirmed."
            )
        return (
            f"⚠️ CHILD_RESULT_DISPOSITION_WRITE_FAILED: the exact pending tuple for "
            f"{tid} was replaced before confirmation; the stale caller did not overwrite it."
        )
    current = _effective_child_result_for_cas(status_drive_root, current)
    if _child_result_sha256(current) != expected:
        return (
            f"⚠️ CHILD_RESULT_STALE: {tid} changed while its disposition was confirmed; "
            "the stored decision is stale and does not close the new result."
        )
    beacon_observable = _child_disposition_beacon_exists(
        ctx,
        child_task_id=tid,
        disposition=disposition,
        child_result_sha256=expected,
        rationale=reason_text,
    )
    if _current_child_result_disposition(current) != disposition or not beacon_observable:
        if not beacon_observable:
            try:
                _compare_and_set_child_disposition_beacon_state(
                    status_drive_root,
                    tid,
                    root_task_id=root_task_id,
                    parent_task_id=parent_task_id,
                    child_result_sha256=expected,
                    expected_fields={
                        **fields,
                        _CHILD_DISPOSITION_BEACON_STATE_FIELD: _CHILD_DISPOSITION_BEACON_CONFIRMED,
                    },
                    target_state=_CHILD_DISPOSITION_BEACON_PENDING,
                )
            except Exception:
                log.debug("Failed to demote unobservable child beacon for %s", tid, exc_info=True)
        return (
            f"⚠️ CHILD_RESULT_DISPOSITION_WRITE_FAILED: the exact confirmed "
            f"disposition and tree beacon for {tid} were not both observable."
        )
    return f"OK: child {tid} marked {disposition} for result {expected[:12]}."


def _record_current_child_result_disposition(
    ctx: ToolContext,
    child_task_id: str,
    disposition: str,
    rationale: str,
    *,
    source: str,
    legacy_parent_decision: str = "",
) -> str:
    """Bind an operation that just genuinely consumed/rejected the current result."""

    try:
        tid = validate_task_id(child_task_id)
    except ValueError as exc:
        return f"⚠️ CHILD_RESULT_DISPOSITION_INVALID: {exc}"
    data = load_effective_task_result(_status_drive_root(ctx), tid) or {}
    if not data:
        return f"⚠️ CHILD_RESULT_STALE: {tid} has no current result to bind."
    return _record_child_result_disposition(
        ctx,
        {
            "type": CHILD_RESULT_DISPOSITION_TYPE,
            "child_task_id": tid,
            "disposition": disposition,
            "child_result_sha256": _child_result_sha256(data),
        },
        rationale,
        source=source,
        legacy_parent_decision=legacy_parent_decision,
    )


def _record_child_decision_beacon(ctx: ToolContext, task_id: str, text: str) -> None:
    """Record a parent coordination decision about a child on the task-tree ledger
    (D#7) so the decision is durable + visible across the tree. Best-effort."""
    try:
        from ouroboros.tools.task_tree import tree_root_id

        rid = tree_root_id(ctx)
        if not rid:
            return
        from ouroboros.task_tree_ledger import tree_ledger_append

        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        role = str(meta.get("role") or getattr(ctx, "role", "") or "")
        tree_ledger_append(rid, "decision", text, task_id=str(task_id or ""), role=role)
    except Exception:
        log.debug("Failed to record child decision beacon for %s", task_id, exc_info=True)


def _status_drive_root(ctx: ToolContext) -> Path:
    metadata = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
    return Path(str(metadata.get("budget_drive_root") or getattr(ctx, "budget_drive_root", "") or ctx.drive_root))


def _is_own_child(ctx: ToolContext, status_drive_root: Path, tid: str) -> bool:
    """True if ``tid`` is a DIRECT child of the CURRENT task (D#7 safety): a parent
    decision (discard/cancel parent_decision stamp) may only touch the caller's OWN
    children, never an unrelated parent's join ledger. Fail-CLOSED — any error returns
    False so the stamp is withheld rather than wrongly applied."""
    try:
        from ouroboros.task_status import find_child_tasks

        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        my_id = str(getattr(ctx, "task_id", "") or meta.get("task_id") or "")
        if not my_id or not tid:
            return False
        children = find_child_tasks(
            Path(status_drive_root), parent_task_id=my_id, root_task_id="", exclude_task_id=my_id
        )
        return any(str(c.get("task_id") or c.get("id") or "") == tid for c in children)
    except Exception:
        return False


def _clip(text: object, limit: int, *, tail: bool = False) -> str:
    """Truncate to ``limit`` chars with an EXPLICIT omission marker so a peek never
    silently drops cognitive content (P1 — no silent horizon cut; the agent can then
    get_task_result the full body if it needs the omitted part)."""
    s = str(text or "")
    if len(s) <= limit:
        return s
    omitted = len(s) - limit
    if tail:
        return f"…(+{omitted} earlier chars omitted — get_task_result for the full body)\n{s[-limit:]}"
    return f"{s[:limit]}…(+{omitted} more chars omitted)"


def _peek_task(ctx: ToolContext, task_id: str, view: str = "summary") -> str:
    """Read a child's CURRENT status + latest coordination beacons + result tail (D#7 — the
    parent's 'see intermediate findings' right). A PURE READ: it changes no state. The
    pre-finalization handoff reminder is CHANGE-BASED (it re-surfaces whenever a child's
    status/result changes and is suppressed only by an explicit discard_child_result /
    cancel_task, or by being unchanged since last shown) — peeking neither suppresses nor
    re-triggers it. view: summary | partials | tail."""
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (peek_task): {exc}"
    v = str(view or "summary").strip().lower()
    status_drive_root = _status_drive_root(ctx)
    data = load_effective_task_result(status_drive_root, tid) or {}
    status = str(data.get("status") or "unknown")
    cost = data.get("cost_usd", 0) or 0
    parts = [
        f"Task {tid} [{status}] cost=${float(cost):.2f} (peek — NOT absorbed)",
        f"child_result_sha256={_child_result_sha256(data)}",
    ]
    # Latest beacons this child posted to the shared ledger (partial_finding / blocker /
    # question / milestone), newest last.
    try:
        from ouroboros.tools.task_tree import tree_root_id
        from ouroboros.task_tree_ledger import tree_ledger_rows

        rid = tree_root_id(ctx)
        if rid:
            rows = [r for r in tree_ledger_rows(rid) if str(r.get("task_id") or "") == tid]
            if v in ("partials", "summary"):
                beacons = [r for r in rows if str(r.get("kind")) in ("partial_finding", "blocker", "question", "milestone", "interface_contract")]
                if len(beacons) > 8:
                    parts.append(f"  …(+{len(beacons) - 8} older beacon(s) omitted; showing newest 8)")
                for r in beacons[-8:]:
                    parts.append(f"  • [{r.get('kind')}] {_clip(r.get('text'), 400)}")
    except Exception:
        log.debug("peek_task ledger read failed for %s", tid, exc_info=True)
    if v in ("tail", "summary"):
        result = str(data.get("result") or "")
        if result:
            parts.append(f"[PEEK_RESULT_TAIL]\n{_clip(result, 1200, tail=True)}\n[/PEEK_RESULT_TAIL]")
    trace = str(data.get("trace_summary") or "")
    if trace and v == "tail":
        parts.append(f"[PEEK_TRACE]\n{_clip(trace, 800)}\n[/PEEK_TRACE]")
    return "\n".join(parts)


def _discard_child_result(ctx: ToolContext, task_id: str, reason: str) -> str:
    """Explicitly decide to ABANDON a child's result (D#7). This is the EXPLICIT,
    structured signal (P5 — not a parsed-from-prose phrase) that lets the parent finalize
    without that child: it stamps ``parent_decision="discarded"`` on the child result so
    the pre-finalization handoff reminder stops surfacing it, and records the reason on the
    shared task-tree ledger. A reason is REQUIRED so the abandon is an auditable judgment,
    not a silent loss. Lineage-gated to the caller's OWN children."""
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (discard_child_result): {exc}"
    reason_text = _clip(" ".join(str(reason or "").split()), 500)
    if not reason_text:
        return "⚠️ TOOL_ARG_ERROR (discard_child_result): a non-empty reason is required."
    status_drive_root = _status_drive_root(ctx)
    # D#7 safety: a parent may abandon only its OWN child's result — never stamp
    # parent_decision on an unrelated task and hide it from its real parent's reminder.
    if not _is_own_child(ctx, status_drive_root, tid):
        return f"⚠️ discard_child_result: {tid} is not a child of this task — refusing to discard."
    recorded = _record_current_child_result_disposition(
        ctx,
        tid,
        "irrelevant",
        reason_text,
        source="discard_child_result",
        # Preserve the existing public tool's legacy marker for old readers while
        # the exact-hash disposition is the new authority.
        legacy_parent_decision="discarded",
    )
    if not recorded.startswith("OK:"):
        return recorded.replace("CHILD_RESULT_DISPOSITION", "discard_child_result")
    return f"Discarded child result {tid} (reason: {reason_text}). It will not block finalization."


def _override_delegation_constraint(ctx: ToolContext, constraint_id: str, reason: str) -> str:
    """Explicitly override an unresolved delegation constraint in this task tree."""

    cid = " ".join(str(constraint_id or "").split())
    if not cid:
        return "⚠️ TOOL_ARG_ERROR (override_delegation_constraint): constraint_id is required."
    reason_text = _clip(" ".join(str(reason or "").split()), 500)
    if not reason_text:
        return "⚠️ TOOL_ARG_ERROR (override_delegation_constraint): a non-empty reason is required."
    try:
        from ouroboros.tools.task_tree import tree_root_id
        from ouroboros.task_tree_ledger import open_delegation_constraints, tree_ledger_append

        rid = tree_root_id(ctx)
        if not rid:
            return "⚠️ override_delegation_constraint: no task-tree scope."
        open_rows = open_delegation_constraints(rid)
        target_row = next((
            row for row in open_rows
            if isinstance(row.get("payload"), dict)
            and str(row["payload"].get("constraint_id") or "") == cid
        ), None)
        if target_row is None:
            return f"⚠️ override_delegation_constraint: constraint {cid!r} is not open in this task tree."
        emitter_task_id = str(target_row.get("task_id") or "").strip()
        if emitter_task_id:
            status_drive_root = _status_drive_root(ctx)
            if not _is_own_child(ctx, status_drive_root, emitter_task_id):
                return (
                    "⚠️ override_delegation_constraint: only the parent of the task that raised "
                    f"constraint {cid!r} may override it."
                )
        meta = getattr(ctx, "task_metadata", {}) if isinstance(getattr(ctx, "task_metadata", {}), dict) else {}
        role = str(meta.get("role") or getattr(ctx, "role", "") or "")
        return tree_ledger_append(
            rid,
            "decision",
            f"overrode delegation constraint {cid}: {reason_text}",
            task_id=str(getattr(ctx, "task_id", "") or ""),
            role=role,
            allow_constraint_override=True,
            payload={
                "constraint_id": cid,
                "decision": "overridden",
                "reason": reason_text,
                "parent_task_id": str(getattr(ctx, "task_id", "") or ""),
            },
        )
    except Exception:
        log.debug("Failed to override delegation constraint %s", cid, exc_info=True)
        return f"⚠️ override_delegation_constraint: failed to record override for {cid}."


def _cancel_task(ctx: ToolContext, task_id: str, reason: str = "") -> str:
    try:
        tid = validate_task_id(task_id)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (cancel_task): {exc}"
    reason_text = _clip(" ".join(str(reason or "").split()), 500)
    status_drive_root = _status_drive_root(ctx)
    # Only stamp the join-ledger parent_decision (+ post to the tree ledger) when the
    # target is THIS task's own child — a cancel must not rewrite an unrelated task's
    # parent_decision and hide it from its real parent's reminder (D#7 safety).
    own = _is_own_child(ctx, status_drive_root, tid)
    # Subagent isolation: a CONSTRAINED caller (workspace/subagent task that can schedule
    # children) may cancel ONLY its own children — never an arbitrary task id. The
    # owner-level orchestrator (self_modification / operator_control) keeps general cancel.
    if not own:
        try:
            from ouroboros.tool_access import active_tool_profile

            if active_tool_profile(ctx) in (
                "workspace_task", "external_workspace_task", "acting_subagent", "local_readonly_subagent",
            ):
                return f"⚠️ cancel_task: {tid} is not a child of this task — a constrained task may only cancel its own children."
        except Exception:
            log.debug("cancel_task lineage profile check failed for %s", tid, exc_info=True)
    # Latch a cancel-intent status so the parent's find_child_tasks view treats the child
    # as terminal immediately (stops the handoff reminder re-injecting "still scheduled").
    try:
        from ouroboros.task_results import STATUS_CANCEL_REQUESTED

        fields: Dict[str, Any] = {
            "result": f"Cancellation requested by agent; awaiting supervisor teardown.{(' Reason: ' + reason_text) if reason_text else ''}",
        }
        if own:
            fields["parent_decision"] = "cancelled"
            fields["parent_decision_reason"] = reason_text
        write_task_result(status_drive_root, tid, STATUS_CANCEL_REQUESTED, **fields)
    except Exception:
        log.debug("Failed to latch cancel_requested status for %s", tid, exc_info=True)
    if own:
        _record_child_decision_beacon(ctx, tid, f"cancelled child {tid}" + (f": {reason_text}" if reason_text else ""))
    # Emit live so the supervisor processes the cancellation within one loop tick.
    from ouroboros.tools.control import _emit_control_event

    emitted = _emit_control_event(ctx, {"type": "cancel_task", "task_id": tid, "reason": reason_text, "ts": utc_now_iso()})
    note = " (live)" if emitted == "live" else " (deferred to round end)"
    return f"Cancel requested: {tid}{(' — ' + reason_text) if reason_text else ''}{note}"


def get_tools() -> list[ToolEntry]:
    return [
        ToolEntry("cancel_task", {
            "name": "cancel_task",
            "description": "Stop a running/scheduled child task by ID. Give a short reason — it is "
                           "recorded on the shared task-tree ledger and the child's result, so a "
                           "stopped child is an auditable decision, not a silent disappearance.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "default": "", "description": "Why you are stopping it (recorded for the tree + review)."},
            }, "required": ["task_id"]},
        }, _cancel_task),
        ToolEntry("peek_task", {
            "name": "peek_task",
            "description": "Look at a child task's CURRENT status, its latest coordination beacons "
                           "(partial_finding/blocker/question/milestone) and a tail of its result — "
                           "a PURE READ. Use this to check intermediate findings or decide whether to keep "
                           "waiting / steer / cancel, without committing to a finalization decision. It "
                           "changes no state: the pre-finalization reminder is change-based and is cleared "
                           "only by discard_child_result / cancel_task, not by reading.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "view": {"type": "string", "enum": ["summary", "partials", "tail"], "default": "summary",
                         "description": "summary = status+beacons+result tail; partials = beacons only; tail = result+trace tail."},
            }, "required": ["task_id"]},
        }, _peek_task),
        ToolEntry("discard_child_result", {
            "name": "discard_child_result",
            "description": "Explicitly decide to finalize WITHOUT a child's result (abandon it on purpose). "
                           "Requires a reason. This is the structured way to drop a child you no longer need "
                           "so it stops being flagged before you finalize — use it instead of just ignoring "
                           "the child, so the abandon is a logged judgment rather than a silent loss.",
            "parameters": {"type": "object", "properties": {
                "task_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why this child's result is not needed."},
            }, "required": ["task_id", "reason"]},
        }, _discard_child_result),
        ToolEntry("override_delegation_constraint", {
            "name": "override_delegation_constraint",
            "description": "Explicitly override an unresolved delegation_constraint in this task tree. Requires a reason; records an append-only decision row so a future schedule_subagent call may proceed audibly.",
            "parameters": {"type": "object", "properties": {
                "constraint_id": {"type": "string"},
                "reason": {"type": "string", "description": "Why overriding this constraint is correct."},
            }, "required": ["constraint_id", "reason"]},
        }, _override_delegation_constraint),
    ]
