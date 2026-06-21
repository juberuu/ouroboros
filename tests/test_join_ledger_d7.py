"""D#7 soft-join: explicit child-decision tools (discard_child_result, peek_task).

These are the structured (P5, not parsed-from-prose) signals that let a parent finalize
without orphaning a child: discard stamps a durable parent_decision the pre-finalization
reminder honors; peek lets the parent inspect a child WITHOUT absorbing it. A parent
decision may only touch the caller's OWN children (lineage-gated, fail-closed).
"""

from __future__ import annotations

from types import SimpleNamespace


def _ctx(tmp_path, task_id="parent1"):
    return SimpleNamespace(
        drive_root=str(tmp_path),
        budget_drive_root=str(tmp_path),
        task_metadata={},
        task_id=task_id,
        role="orchestrator",
    )


def _write_child(tmp_path, child_id, parent_id, **fields):
    from ouroboros.task_results import STATUS_RUNNING, write_task_result

    # Real subagent children carry delegation_role="subagent"; find_child_tasks (and thus
    # the lineage gate) only matches those.
    write_task_result(
        tmp_path, child_id, STATUS_RUNNING,
        parent_task_id=parent_id, root_task_id=parent_id, delegation_role="subagent", **fields,
    )


def test_discard_own_child_stamps_parent_decision(tmp_path):
    from ouroboros.task_results import load_task_result
    from ouroboros.tools.join_ledger import _discard_child_result

    _write_child(tmp_path, "child1", "parent1", result="partial work")
    out = _discard_child_result(_ctx(tmp_path), "child1", "superseded by a different approach")
    assert "Discarded" in out
    res = load_task_result(tmp_path, "child1") or {}
    assert res.get("parent_decision") == "discarded"
    assert "superseded" in str(res.get("parent_decision_reason") or "")


def test_discard_requires_reason(tmp_path):
    from ouroboros.tools.join_ledger import _discard_child_result

    _write_child(tmp_path, "child1", "parent1", result="x")
    out = _discard_child_result(_ctx(tmp_path), "child1", "   ")
    assert "reason is required" in out


def test_discard_refuses_non_child(tmp_path):
    """Lineage safety: discarding a task that is NOT this task's child is refused, so a
    parent cannot corrupt an unrelated parent's join ledger."""
    from ouroboros.task_results import load_task_result
    from ouroboros.tools.join_ledger import _discard_child_result

    # child of a DIFFERENT parent
    _write_child(tmp_path, "stranger", "other_parent", result="not yours")
    out = _discard_child_result(_ctx(tmp_path), "stranger", "trying to discard")
    assert "not a child of this task" in out
    res = load_task_result(tmp_path, "stranger") or {}
    assert res.get("parent_decision") != "discarded"  # untouched


def test_discard_unknown_child_is_safe(tmp_path):
    from ouroboros.tools.join_ledger import _discard_child_result

    out = _discard_child_result(_ctx(tmp_path), "nope", "n/a")
    # unknown id is not a child either -> refused (never raises)
    assert "not a child of this task" in out or "unknown" in out.lower()


def test_peek_task_reads_without_absorbing(tmp_path):
    from ouroboros.tools.join_ledger import _peek_task

    _write_child(tmp_path, "child1", "parent1", result="intermediate finding X", cost_usd=0.05)
    out = _peek_task(_ctx(tmp_path), "child1", view="summary")
    assert "child1" in out
    assert "NOT absorbed" in out  # peek must not look like an absorbed result
    assert "intermediate finding X" in out  # result tail is visible
