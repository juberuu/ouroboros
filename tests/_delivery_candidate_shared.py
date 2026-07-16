from __future__ import annotations


def write_child(
    tmp_path,
    child_id: str = "child1",
    status: str = "completed",
    **fields,
):
    """Write the canonical child-result fixture shared by delivery tests."""
    from ouroboros.task_results import write_task_result

    return write_task_result(
        tmp_path,
        child_id,
        status,
        parent_task_id="parent1",
        root_task_id="parent1",
        delegation_role="subagent",
        result="full child result",
        trace_summary="trace",
        artifact_status="ready",
        artifacts=[{"kind": "report", "name": "report.md", "sha256": "a" * 64}],
        **fields,
    )


def write_confirmed_disposition_fixture(
    tmp_path,
    *,
    child_id: str = "child1",
    disposition: str,
    rationale: str,
):
    """Synthesize the host-confirmed projection for pure loop/outcome unit tests.

    The recorder/durable-beacon behavior itself is covered in
    ``test_child_result_disposition.py``; these tests intentionally exercise only
    consumers of an already-confirmed task-result record.
    """

    from ouroboros.task_results import write_task_result
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import (
        _child_disposition_beacon_binding_sha256,
        _child_result_sha256,
    )

    child = load_effective_task_result(tmp_path, child_id)
    result_sha256 = _child_result_sha256(child)
    root_task_id = str(child.get("root_task_id") or "parent1")
    parent_task_id = str(child.get("parent_task_id") or "parent1")
    receipt_sha256 = _child_disposition_beacon_binding_sha256(
        root_task_id=root_task_id,
        parent_task_id=parent_task_id,
        child_task_id=child_id,
        disposition=disposition,
        child_result_sha256=result_sha256,
        rationale=rationale,
    )
    return write_task_result(
        tmp_path,
        child_id,
        str(child.get("status") or "completed"),
        child_result_disposition=disposition,
        child_result_disposition_sha256=result_sha256,
        child_result_disposition_reason=rationale,
        child_result_disposition_beacon_state="confirmed",
        child_result_disposition_beacon_sha256=receipt_sha256,
    )
