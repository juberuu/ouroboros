"""D#7 / P5: the pre-finalization subagent handoff reminder is suppressed ONLY by a
structured decision (parent_decision discarded/cancelled) or absorption — never by
parsing the final PROSE for status words (the removed _final_text_acknowledges_*
keyword gate). A nonterminal, undecided child surfaces the reminder regardless of what
the final text says.
"""

from __future__ import annotations

from types import SimpleNamespace


def _tools(tmp_path):
    ctx = SimpleNamespace(
        task_metadata={"budget_drive_root": str(tmp_path), "root_task_id": "root"},
        budget_drive_root=str(tmp_path),
        _subagent_handoff_signature="",
    )
    return SimpleNamespace(_ctx=ctx)


def _write_child(tmp_path, child_id, status="running", **fields):
    from ouroboros.task_results import write_task_result

    write_task_result(
        tmp_path, child_id, status,
        parent_task_id="root", root_task_id="root", delegation_role="subagent",
        result="partial", **fields,
    )


def test_prose_does_not_suppress_handoff(tmp_path):
    """Even when the final text 'acknowledges' the child in prose, an undecided
    nonterminal child still surfaces the handoff reminder (P5: no keyword gate)."""
    from ouroboros.loop import _compute_subagent_handoff

    _write_child(tmp_path, "childA", status="running")
    prose = "All set. I am leaving childA running / pending; not complete yet."
    out = _compute_subagent_handoff(_tools(tmp_path), tmp_path, "root", prose)
    assert out, "handoff reminder must fire despite prose acknowledgement"
    assert "childA" in out


def test_structured_discard_suppresses_handoff(tmp_path):
    """A child explicitly discarded (parent_decision) is excluded from the reminder."""
    from ouroboros.loop import _compute_subagent_handoff

    _write_child(tmp_path, "childA", status="running", parent_decision="discarded",
                 parent_decision_reason="not needed")
    out = _compute_subagent_handoff(_tools(tmp_path), tmp_path, "root", "done")
    assert out == "", f"discarded child must not surface a reminder, got: {out!r}"
