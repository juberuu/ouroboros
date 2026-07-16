from __future__ import annotations

import json
from types import SimpleNamespace

from tests._delivery_candidate_shared import write_child as _write_child


def _parent_ctx(tmp_path, task_id: str = "parent1") -> SimpleNamespace:
    return SimpleNamespace(
        drive_root=str(tmp_path),
        budget_drive_root=str(tmp_path),
        task_metadata={"budget_drive_root": str(tmp_path), "root_task_id": task_id},
        task_id=task_id,
        role="orchestrator",
    )



def test_child_result_hash_has_exact_semantic_boundary():
    from ouroboros.tools.join_ledger import _child_result_sha256

    base = {
        "status": "completed",
        "result": "answer",
        "trace_summary": "trace",
        "artifact_status": "ready",
        "artifacts": [{"kind": "report", "name": "a.md", "sha256": "a" * 64, "path": "/tmp/one"}],
    }
    reference = _child_result_sha256(base)
    assert _child_result_sha256({
        **base,
        "cost_usd": 9.9,
        "updated_at": "tomorrow",
        "queue_reconciliation_warning": "diagnostic",
        "parent_decision": "cancelled",
        "child_result_disposition": "deferred",
        "child_result_disposition_sha256": "0" * 64,
    }) == reference
    assert _child_result_sha256({**base, "result": "changed"}) != reference
    assert _child_result_sha256({**base, "status": "failed"}) != reference
    assert _child_result_sha256({**base, "trace_summary": "changed"}) != reference
    assert _child_result_sha256({
        **base,
        "artifacts": [{"kind": "report", "name": "a.md", "sha256": "b" * 64}],
    }) != reference
    assert _child_result_sha256({
        **base,
        "artifacts": [{"abs_path": "/tmp/other.md"}],
    }) != reference
    assert _child_result_sha256({
        **base,
        "artifacts": ["reports/summary.md"],
    }) != _child_result_sha256({
        **base,
        "artifacts": ["archive/summary.md"],
    })
    assert _child_result_sha256({
        **base,
        "artifacts": [{"path": "reports/summary.md"}],
    }) != _child_result_sha256({
        **base,
        "artifacts": [{"path": "archive/summary.md"}],
    })


def test_tree_note_disposition_accepts_visible_hash_then_old_hash_is_stale(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.control import _get_task_result, _wait_for_task, _wait_for_tasks
    from ouroboros.tools.join_ledger import _child_result_sha256, _current_child_result_disposition, _peek_task
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    ctx = _parent_ctx(tmp_path)
    current = load_effective_task_result(tmp_path, "child1")
    shown_hash = _child_result_sha256(current)
    assert f"child_result_sha256={shown_hash}" in _peek_task(ctx, "child1")
    assert f"child_result_sha256={shown_hash}" in _get_task_result(ctx, "child1")
    assert f"child_result_sha256={shown_hash}" in _wait_for_task(
        ctx, "child1", timeout_sec=0,
    )
    waited = json.loads(_wait_for_tasks(
        ctx, ["child1"], timeout_sec=0, mode="all_terminal",
    ))
    assert waited["tasks"]["child1"]["child_result_sha256"] == shown_hash

    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "integrated",
        "child_result_sha256": shown_hash,
    }
    out = _tree_note(ctx, "decision", "used the complete child analysis", payload=payload)
    assert out.startswith("OK:")
    stored = load_task_result(tmp_path, "child1") or {}
    assert stored["child_result_disposition"] == "integrated"
    assert stored["child_result_disposition_sha256"] == shown_hash
    assert _current_child_result_disposition(load_effective_task_result(tmp_path, "child1")) == "integrated"
    rows_before = ledger.tree_ledger_rows("parent1")
    assert rows_before[-1]["payload"] == payload
    assert rows_before[-1]["needs_parent_attention"] is False
    assert ledger.tree_ledger_attention_after("parent1", "") == []
    assert ledger.open_delegation_constraints("parent1") == []

    write_task_result(tmp_path, "child1", "completed", result="new child result")
    changed = load_effective_task_result(tmp_path, "child1")
    assert _child_result_sha256(changed) != shown_hash
    assert _current_child_result_disposition(changed) == ""
    stale = _tree_note(ctx, "decision", "still integrated", payload=payload)
    assert "CHILD_RESULT_STALE" in stale
    assert ledger.tree_ledger_rows("parent1") == rows_before


def test_disposition_cas_uses_artifact_inclusive_effective_projection(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.artifacts import task_artifact_dir_path
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import (
        _child_result_sha256,
        _current_child_result_disposition,
    )
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    child_id = "artifact-child"
    write_task_result(
        tmp_path,
        child_id,
        "completed",
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        result="artifact-backed result",
    )
    artifact_dir = task_artifact_dir_path(tmp_path, child_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "report.md"
    artifact_path.write_text("version one\n", encoding="utf-8")

    raw = load_task_result(tmp_path, child_id) or {}
    effective = load_effective_task_result(tmp_path, child_id)
    shown_hash = _child_result_sha256(effective)
    assert effective.get("artifacts")
    assert _child_result_sha256(raw) != shown_hash

    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "integrated the artifact-backed result",
        payload={
            "type": "child_result_disposition",
            "child_task_id": child_id,
            "disposition": "integrated",
            "child_result_sha256": shown_hash,
        },
    )
    assert out.startswith("OK:")
    stored = load_task_result(tmp_path, child_id) or {}
    assert stored["child_result_disposition_beacon_state"] == "confirmed"
    confirmed = load_effective_task_result(tmp_path, child_id)
    assert _current_child_result_disposition(confirmed) == "integrated"

    artifact_path.write_text("version two\n", encoding="utf-8")
    changed = load_effective_task_result(tmp_path, child_id)
    assert _child_result_sha256(changed) != shown_hash
    assert _current_child_result_disposition(changed) == ""


def test_beacon_append_failure_stays_pending_then_same_payload_repairs(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.loop import _child_disposition_state
    from ouroboros.task_results import load_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import (
        _child_result_sha256,
        _current_child_result_disposition,
    )
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    real_append = ledger.append_jsonl
    calls = {"count": 0}

    def fail_once(path, row):
        calls["count"] += 1
        if calls["count"] == 1:
            return False
        return real_append(path, row)

    monkeypatch.setattr(ledger, "append_jsonl", fail_once)
    _write_child(tmp_path)
    ctx = _parent_ctx(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "integrated",
        "child_result_sha256": _child_result_sha256(child),
    }

    failed = _tree_note(ctx, "decision", "absorbed exact child", payload=payload)
    assert "TREE_LEDGER_WRITE_FAILED" in failed
    pending = load_effective_task_result(tmp_path, "child1")
    assert (load_task_result(tmp_path, "child1") or {})[
        "child_result_disposition_beacon_state"
    ] == "pending"
    assert _current_child_result_disposition(pending) == ""
    assert _child_disposition_state(pending) == ""
    assert ledger.tree_ledger_rows("parent1") == []

    repaired = _tree_note(ctx, "decision", "absorbed exact child", payload=payload)
    assert repaired.startswith("OK:")
    confirmed = load_effective_task_result(tmp_path, "child1")
    assert confirmed["child_result_disposition_beacon_state"] == "confirmed"
    assert _current_child_result_disposition(confirmed) == "integrated"
    assert _child_disposition_state(confirmed) == "integrated"
    assert len(ledger.tree_ledger_rows("parent1")) == 1


def test_preexisting_task_result_half_state_is_repaired_before_closure(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.loop import _child_disposition_state
    from ouroboros.task_results import write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import (
        _child_result_sha256,
        _current_child_result_disposition,
    )
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    shown_hash = _child_result_sha256(child)
    rationale = "legacy half-state needs its typed beacon"
    write_task_result(
        tmp_path,
        "child1",
        str(child.get("status") or "completed"),
        child_result_disposition="integrated",
        child_result_disposition_sha256=shown_hash,
        child_result_disposition_reason=rationale,
    )
    half_state = load_effective_task_result(tmp_path, "child1")
    assert _current_child_result_disposition(half_state) == ""
    assert _child_disposition_state(half_state) == ""

    repaired = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        rationale,
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": shown_hash,
        },
    )
    assert repaired.startswith("OK:")
    confirmed = load_effective_task_result(tmp_path, "child1")
    assert confirmed["child_result_disposition_beacon_state"] == "confirmed"
    assert _current_child_result_disposition(confirmed) == "integrated"
    assert len(ledger.tree_ledger_rows("parent1")) == 1


def test_idempotent_confirmed_retry_does_not_duplicate_exact_beacon(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    shown_hash = _child_result_sha256(load_effective_task_result(tmp_path, "child1"))
    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "irrelevant",
        "child_result_sha256": shown_hash,
    }
    ctx = _parent_ctx(tmp_path)
    first = _tree_note(ctx, "decision", "not relevant to final synthesis", payload=payload)
    rows_after_first = ledger.tree_ledger_rows("parent1")
    second = _tree_note(ctx, "decision", "not relevant to final synthesis", payload=payload)

    assert first.startswith("OK:")
    assert "idempotent" in second
    assert ledger.tree_ledger_rows("parent1") == rows_after_first
    assert len(rows_after_first) == 1


def test_confirmed_exact_retry_survives_ephemeral_ledger_gc_and_full_replacement(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import load_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import (
        _child_result_sha256,
        _current_child_result_disposition,
    )
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    shown_hash = _child_result_sha256(load_effective_task_result(tmp_path, "child1"))
    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "integrated",
        "child_result_sha256": shown_hash,
    }
    rationale = "confirmed before ephemeral ledger collection"
    ctx = _parent_ctx(tmp_path)
    first = _tree_note(ctx, "decision", rationale, payload=payload)
    assert first.startswith("OK:")
    confirmed_before = load_task_result(tmp_path, "child1") or {}
    assert confirmed_before["child_result_disposition_beacon_state"] == "confirmed"

    ledger_path = ledger.tree_ledger_path("parent1")
    ledger_path.unlink()
    ledger_path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    assert ledger.tree_ledger_rows("parent1") == []

    retry = _tree_note(ctx, "decision", rationale, payload=payload)
    confirmed_after = load_task_result(tmp_path, "child1") or {}
    assert retry.startswith("OK:")
    assert "idempotent" in retry
    assert confirmed_after == confirmed_before
    assert confirmed_after["child_result_disposition_beacon_state"] == "confirmed"
    assert _current_child_result_disposition(
        load_effective_task_result(tmp_path, "child1")
    ) == "integrated"


def test_beacon_before_confirm_failure_repairs_without_duplicate(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    import ouroboros.tools.join_ledger as join
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "integrated",
        "child_result_sha256": join._child_result_sha256(child),
    }
    real_transition = join._compare_and_set_child_disposition_beacon_state
    failed = {"done": False}

    def fail_first_confirm(*args, **kwargs):
        if kwargs.get("target_state") == "confirmed" and not failed["done"]:
            failed["done"] = True
            raise OSError("simulated crash before confirmation")
        return real_transition(*args, **kwargs)

    monkeypatch.setattr(
        join,
        "_compare_and_set_child_disposition_beacon_state",
        fail_first_confirm,
    )
    ctx = _parent_ctx(tmp_path)
    first = _tree_note(ctx, "decision", "absorbed before crash", payload=payload)
    after_first = load_effective_task_result(tmp_path, "child1")
    assert "WRITE_FAILED" in first
    assert after_first["child_result_disposition_beacon_state"] == "pending"
    assert join._current_child_result_disposition(after_first) == ""
    assert len(ledger.tree_ledger_rows("parent1")) == 1

    second = _tree_note(ctx, "decision", "absorbed before crash", payload=payload)
    after_second = load_effective_task_result(tmp_path, "child1")
    assert second.startswith("OK:")
    assert join._current_child_result_disposition(after_second) == "integrated"
    assert len(ledger.tree_ledger_rows("parent1")) == 1


def test_exact_existing_beacon_repairs_even_when_ledger_is_full(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256, _current_child_result_disposition
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    shown_hash = _child_result_sha256(child)
    rationale = "beacon survived before task-result confirmation"
    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "integrated",
        "child_result_sha256": shown_hash,
    }
    assert ledger.tree_ledger_append(
        "parent1",
        "decision",
        rationale,
        task_id="parent1",
        role="orchestrator",
        payload=payload,
        allow_child_result_disposition=True,
    ).startswith("OK:")
    ledger_path = ledger.tree_ledger_path("parent1")
    with ledger_path.open("ab") as handle:
        handle.write(b"x" * (2 * 1024 * 1024 + 64) + b"\n")
    assert ledger_path.stat().st_size > 2 * 1024 * 1024

    monkeypatch.setattr(
        ledger,
        "tree_ledger_append",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must dedupe before cap")),
    )
    repaired = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        rationale,
        payload=payload,
    )
    assert repaired.startswith("OK:")
    assert _current_child_result_disposition(
        load_effective_task_result(tmp_path, "child1")
    ) == "integrated"


def test_full_ledger_without_exact_beacon_stays_pending(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.loop import _child_disposition_state
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    path = ledger.tree_ledger_path("parent1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "cannot fit missing exact beacon",
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": _child_result_sha256(child),
        },
    )
    pending = load_effective_task_result(tmp_path, "child1")
    assert not out.startswith("OK:")
    assert "ledger is full" in out
    assert pending["child_result_disposition_beacon_state"] == "pending"
    assert _child_disposition_state(pending) == ""


def test_competing_control_tuple_blocks_stale_prepare(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    import ouroboros.tools.join_ledger as join
    from ouroboros.task_results import write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    shown_hash = join._child_result_sha256(child)
    real_prepare = join._compare_and_prepare_child_disposition
    injected = {"done": False}

    def inject_competing_tuple(*args, **kwargs):
        if not injected["done"]:
            injected["done"] = True
            rationale = "newer competing parent decision"
            receipt = join._child_disposition_beacon_binding_sha256(
                root_task_id="parent1",
                parent_task_id="parent1",
                child_task_id="child1",
                disposition="irrelevant",
                child_result_sha256=shown_hash,
                rationale=rationale,
            )
            write_task_result(
                tmp_path,
                "child1",
                "completed",
                child_result_disposition="irrelevant",
                child_result_disposition_sha256=shown_hash,
                child_result_disposition_reason=rationale,
                child_result_disposition_source="concurrent_parent_decision",
                child_result_disposition_beacon_state="pending",
                child_result_disposition_beacon_sha256=receipt,
            )
        return real_prepare(*args, **kwargs)

    monkeypatch.setattr(join, "_compare_and_prepare_child_disposition", inject_competing_tuple)
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "stale decision must not win",
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": shown_hash,
        },
    )
    stored = load_effective_task_result(tmp_path, "child1")
    assert "competing disposition before prepare" in out
    assert stored["child_result_disposition"] == "irrelevant"
    assert stored["child_result_disposition_beacon_state"] == "pending"
    assert join._current_child_result_disposition(stored) == ""
    assert ledger.tree_ledger_rows("parent1") == []


def test_competing_pending_tuple_blocks_stale_confirm(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    import ouroboros.tools.join_ledger as join
    from ouroboros.task_results import write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    shown_hash = join._child_result_sha256(child)
    real_append = join._append_child_disposition_beacon

    def append_then_replace(ctx, **kwargs):
        appended = real_append(ctx, **kwargs)
        rationale = "replacement landed before stale confirmation"
        receipt = join._child_disposition_beacon_binding_sha256(
            root_task_id="parent1",
            parent_task_id="parent1",
            child_task_id="child1",
            disposition="irrelevant",
            child_result_sha256=shown_hash,
            rationale=rationale,
        )
        write_task_result(
            tmp_path,
            "child1",
            "completed",
            child_result_disposition="irrelevant",
            child_result_disposition_sha256=shown_hash,
            child_result_disposition_reason=rationale,
            child_result_disposition_source="concurrent_parent_decision",
            child_result_disposition_beacon_state="pending",
            child_result_disposition_beacon_sha256=receipt,
        )
        return appended

    monkeypatch.setattr(join, "_append_child_disposition_beacon", append_then_replace)
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "old caller must not confirm",
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": shown_hash,
        },
    )
    stored = load_effective_task_result(tmp_path, "child1")
    assert "pending tuple" in out and "replaced" in out
    assert stored["child_result_disposition"] == "irrelevant"
    assert stored["child_result_disposition_beacon_state"] == "pending"
    assert join._current_child_result_disposition(stored) == ""


def test_child_mutation_after_beacon_blocks_confirmation(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    import ouroboros.tools.join_ledger as join
    from ouroboros.task_results import write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    child = load_effective_task_result(tmp_path, "child1")
    shown_hash = join._child_result_sha256(child)
    real_append = join._append_child_disposition_beacon

    def append_then_mutate(ctx, **kwargs):
        appended = real_append(ctx, **kwargs)
        write_task_result(tmp_path, "child1", "completed", result="changed after beacon")
        return appended

    monkeypatch.setattr(join, "_append_child_disposition_beacon", append_then_mutate)
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "old child snapshot",
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": shown_hash,
        },
    )
    current = load_effective_task_result(tmp_path, "child1")
    assert "CHILD_RESULT_STALE" in out
    assert current["child_result_disposition_beacon_state"] == "pending"
    assert join._current_child_result_disposition(current) == ""


def test_beacon_dedupe_requires_same_parent_and_rationale(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    shown_hash = _child_result_sha256(load_effective_task_result(tmp_path, "child1"))
    payload = {
        "type": "child_result_disposition",
        "child_task_id": "child1",
        "disposition": "integrated",
        "child_result_sha256": shown_hash,
    }
    for task_id, rationale in (
        ("other-parent", "exact rationale"),
        ("parent1", "different rationale"),
    ):
        assert ledger.tree_ledger_append(
            "parent1",
            "decision",
            rationale,
            task_id=task_id,
            payload=payload,
            allow_child_result_disposition=True,
        ).startswith("OK:")

    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "exact rationale",
        payload=payload,
    )
    rows = ledger.tree_ledger_rows("parent1")
    assert out.startswith("OK:")
    assert len(rows) == 3
    assert rows[-1]["task_id"] == "parent1"
    assert rows[-1]["text"] == "exact rationale"


def test_malformed_tagged_disposition_never_becomes_plain_note(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import load_task_result
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    ctx = _parent_ctx(tmp_path)
    out = _tree_note(
        ctx,
        "decision",
        "should not land",
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
        },
    )
    assert "CHILD_RESULT_DISPOSITION_INVALID" in out
    assert ledger.tree_ledger_rows("parent1") == []
    assert "child_result_disposition" not in (load_task_result(tmp_path, "child1") or {})


def test_disposition_accepts_and_preserves_exact_500_character_rationale(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import load_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    rationale = "r" * 500
    current = load_effective_task_result(tmp_path, "child1")
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        rationale,
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": _child_result_sha256(current),
        },
    )

    assert out.startswith("OK:")
    assert (load_task_result(tmp_path, "child1") or {})[
        "child_result_disposition_reason"
    ] == rationale
    assert ledger.tree_ledger_rows("parent1")[-1]["text"] == rationale


def test_disposition_rejects_501_character_rationale_without_mutation(
    tmp_path,
    monkeypatch,
):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import load_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    _write_child(tmp_path)
    before_result = load_task_result(tmp_path, "child1")
    before_rows = ledger.tree_ledger_rows("parent1")
    current = load_effective_task_result(tmp_path, "child1")
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "r" * 501,
        payload={
            "type": "child_result_disposition",
            "child_task_id": "child1",
            "disposition": "integrated",
            "child_result_sha256": _child_result_sha256(current),
        },
    )

    assert "CHILD_RESULT_DISPOSITION_INVALID" in out
    assert "at most 500 characters" in out
    assert load_task_result(tmp_path, "child1") == before_result
    assert ledger.tree_ledger_rows("parent1") == before_rows


def test_tagged_disposition_rejects_non_direct_child_without_mutation(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import load_task_result, write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    write_task_result(
        tmp_path,
        "foreign-child",
        "completed",
        parent_task_id="different-parent",
        root_task_id="different-parent",
        delegation_role="subagent",
        result="foreign result",
    )
    current = load_effective_task_result(tmp_path, "foreign-child")
    before = load_task_result(tmp_path, "foreign-child")
    out = _tree_note(
        _parent_ctx(tmp_path),
        "decision",
        "must not cross lineage",
        payload={
            "type": "child_result_disposition",
            "child_task_id": "foreign-child",
            "disposition": "integrated",
            "child_result_sha256": _child_result_sha256(current),
        },
    )

    assert "CHILD_RESULT_LINEAGE_FORBIDDEN" in out
    assert load_task_result(tmp_path, "foreign-child") == before
    assert ledger.tree_ledger_rows("parent1") == []


def test_old_visible_hash_is_stale_after_result_status_or_artifact_change(tmp_path, monkeypatch):
    import ouroboros.task_tree_ledger as ledger
    from ouroboros.task_results import write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256
    from ouroboros.tools.task_tree import _tree_note

    monkeypatch.setattr(ledger, "DATA_DIR", tmp_path)
    ctx = _parent_ctx(tmp_path)
    mutations = {
        "child_result": {"status": "running", "result": "changed"},
        "child_status": {"status": "completed"},
        "child_artifact": {
            "status": "running",
            "artifacts": [{"kind": "report", "name": "report.md", "sha256": "b" * 64}],
        },
    }
    for child_id, mutation in mutations.items():
        _write_child(tmp_path, child_id=child_id, status="running")
        old_hash = _child_result_sha256(load_effective_task_result(tmp_path, child_id))
        payload = {
            "type": "child_result_disposition",
            "child_task_id": child_id,
            "disposition": "integrated",
            "child_result_sha256": old_hash,
        }
        assert _tree_note(ctx, "decision", f"consumed {child_id}", payload=payload).startswith("OK:")
        new_status = mutation.pop("status")
        write_task_result(tmp_path, child_id, new_status, **mutation)
        assert "CHILD_RESULT_STALE" in _tree_note(
            ctx, "decision", f"stale {child_id}", payload=payload,
        )


def _prepare_cancel_race(tmp_path, child_id: str, terminal_status: str = "completed"):
    from ouroboros.task_results import STATUS_CANCEL_REQUESTED, write_task_result

    child_drive = tmp_path / "state" / "headless_tasks" / child_id / "data"
    child_drive.mkdir(parents=True)
    task = {
        "id": child_id,
        "chat_id": 0,
        "delegation_role": "subagent",
        "role": "reviewer",
        "parent_task_id": "parent1",
        "root_task_id": "parent1",
        "drive_root": str(child_drive),
        "child_drive_root": str(child_drive),
    }
    write_task_result(
        tmp_path,
        child_id,
        STATUS_CANCEL_REQUESTED,
        **{key: value for key, value in task.items() if key != "id"},
        parent_decision="cancelled",
        parent_decision_reason="superseded by the parent",
        result="Cancellation requested by agent; awaiting supervisor teardown.",
    )
    terminal = write_task_result(
        child_drive,
        child_id,
        terminal_status,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        result="late full child result\nTAIL_MARKER",
        trace_summary="complete child trace",
        artifact_status="ready",
        artifacts=[{
            "kind": "report",
            "name": "report.md",
            "sha256": "c" * 64,
            "status": "ready",
            "path": "/volatile/child/report.md",
        }],
        artifact_bundle={
            "status": "ready",
            "artifacts": [],
        },
        cost_usd=17.0,
        queue_reconciliation_warning="volatile",
        parent_decision="cancelled",
    )
    return task, child_drive, terminal


def _patch_cancel_runtime(tmp_path, monkeypatch, task, worker=None):
    from supervisor import queue as queue_module
    from supervisor import workers

    monkeypatch.setattr(queue_module, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_module, "PENDING", [])
    monkeypatch.setattr(
        queue_module,
        "RUNNING",
        ({task["id"]: {"task": task, "worker_id": worker.wid}} if worker else {}),
    )
    monkeypatch.setattr(workers, "WORKERS", ({worker.wid: worker} if worker else {}))
    monkeypatch.setattr(queue_module, "reconstruct_task_cost", lambda *_a, **_k: {
        "cost_usd": 19.0,
        "cost_final": True,
    })
    monkeypatch.setattr(queue_module, "_emit_cancel_task_done", lambda *_a, **_k: None)
    monkeypatch.setattr(queue_module, "_kept_service_pids", lambda: set())
    monkeypatch.setattr(queue_module, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(workers, "respawn_worker", lambda _wid: None)
    monkeypatch.setattr(
        "ouroboros.tools.services.archive_task_service_logs",
        lambda *_a, **_k: None,
    )
    return queue_module


def test_running_cancel_race_preserves_terminal_snapshot_before_drive_removal(
    tmp_path,
    monkeypatch,
):
    from ouroboros.loop import _child_disposition_state, _compute_subagent_handoff
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256

    class DeadProc:
        pid = None

        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def join(timeout=None):
            del timeout

        @staticmethod
        def terminate():
            raise AssertionError("already dead")

    task, child_drive, child_terminal = _prepare_cancel_race(tmp_path, "childrace")
    expected_hash = _child_result_sha256(child_terminal)
    worker = SimpleNamespace(wid=7, busy_task_id=task["id"], proc=DeadProc())
    queue_module = _patch_cancel_runtime(tmp_path, monkeypatch, task, worker)
    from supervisor import workers
    import ouroboros.headless as headless

    class LiveReplacementProc:
        @staticmethod
        def is_alive():
            return True

    def mutate_handle_during_respawn(_wid):
        worker.proc = LiveReplacementProc()

    monkeypatch.setattr(workers, "respawn_worker", mutate_handle_during_respawn)

    original_rmtree = headless.shutil.rmtree
    removal_snapshots = []

    def assert_snapshot_before_removal(path, *args, **kwargs):
        current = load_task_result(tmp_path, task["id"]) or {}
        removal_snapshots.append(current.get("terminal_child_result_snapshot"))
        assert current["status"] == STATUS_CANCELLED
        assert current["terminal_child_result_snapshot"]["result"].endswith("TAIL_MARKER")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(headless.shutil, "rmtree", assert_snapshot_before_removal)

    assert queue_module._cancel_task_by_id_single(task["id"]) is True

    stored = load_task_result(tmp_path, task["id"]) or {}
    assert stored["status"] == STATUS_CANCELLED
    assert stored["result"] == "Running task cancelled and worker terminated."
    snapshot = stored["terminal_child_result_snapshot"]
    assert set(snapshot) == {
        "status", "result", "trace_summary", "artifact_status", "artifacts",
    }
    assert snapshot["status"] == "completed"
    assert snapshot["result"].endswith("TAIL_MARKER")
    assert snapshot["trace_summary"] == "complete child trace"
    assert snapshot["artifact_status"] == "ready"
    assert snapshot["artifacts"] == [{
        "kind": "report",
        "name": "report.md",
        "sha256": "c" * 64,
        "status": "ready",
    }]
    assert not {
        "cost_usd",
        "updated_at",
        "queue_reconciliation_warning",
        "parent_decision",
        "artifact_bundle",
    }.intersection(snapshot)
    assert not child_drive.parent.exists()
    assert worker.proc.is_alive(), "the test must exercise an in-place respawn mutation"
    assert len(removal_snapshots) == 1

    effective = load_effective_task_result(tmp_path, task["id"])
    assert effective["status"] == STATUS_CANCELLED
    assert effective["child_status"] == "completed"
    assert effective["result"].endswith("TAIL_MARKER")
    assert _child_result_sha256(effective) == expected_hash
    assert _child_disposition_state(effective) == ""

    tools = SimpleNamespace(_ctx=SimpleNamespace(
        task_metadata={"budget_drive_root": str(tmp_path), "root_task_id": "parent1"},
        budget_drive_root=str(tmp_path),
        _subagent_handoff_signature="",
    ))
    handoff = _compute_subagent_handoff(tools, tmp_path, "parent1", "done")
    assert "TAIL_MARKER" in handoff
    assert "terminal_result_status=completed" in handoff
    assert f"child_result_sha256={expected_hash}" in handoff


def test_cancel_on_miss_preserves_terminal_snapshot_and_reopens_disposition(
    tmp_path,
    monkeypatch,
):
    from ouroboros.loop import _child_disposition_state
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _child_result_sha256

    task, child_drive, child_terminal = _prepare_cancel_race(
        tmp_path,
        "childmiss",
        terminal_status="failed",
    )
    expected_hash = _child_result_sha256(child_terminal)
    queue_module = _patch_cancel_runtime(tmp_path, monkeypatch, task)

    assert queue_module._cancel_task_by_id_single(task["id"]) is True

    stored = load_task_result(tmp_path, task["id"]) or {}
    assert stored["status"] == STATUS_CANCELLED
    assert stored["terminal_child_result_snapshot"]["status"] == "failed"
    assert child_drive.is_dir()
    effective = load_effective_task_result(tmp_path, task["id"])
    assert effective["status"] == STATUS_CANCELLED
    assert effective["child_status"] == "failed"
    assert effective["result"].endswith("TAIL_MARKER")
    assert _child_result_sha256(effective) == expected_hash
    assert _child_disposition_state(effective) == ""


def test_running_cancel_retains_child_drive_when_snapshot_persist_fails(
    tmp_path,
    monkeypatch,
):
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result

    class DeadProc:
        pid = None

        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def join(timeout=None):
            del timeout

    task, child_drive, _terminal = _prepare_cancel_race(tmp_path, "childpersistfail")
    worker = SimpleNamespace(wid=8, busy_task_id=task["id"], proc=DeadProc())
    queue_module = _patch_cancel_runtime(tmp_path, monkeypatch, task, worker)
    import ouroboros.tools.join_ledger as join_ledger
    monkeypatch.setattr(
        join_ledger,
        "_preserve_cancelled_child_terminal_snapshot",
        lambda *_a, **_k: "failed",
    )

    assert queue_module._cancel_task_by_id_single(task["id"]) is True

    stored = load_task_result(tmp_path, task["id"]) or {}
    assert stored["status"] == STATUS_CANCELLED
    assert "terminal_child_result_snapshot" not in stored
    assert child_drive.is_dir(), "the only complete late result must survive a failed persist"
