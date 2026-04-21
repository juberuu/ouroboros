"""Tests for zombie task prevention logic.

Covers:
- _write_failure_result() writes correct JSON, guards for None/existing
- drain_all_pending() empties PENDING and returns tasks
- kill_workers() writes failure results for RUNNING + PENDING tasks
"""
import json
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# _write_failure_result
# ---------------------------------------------------------------------------

def test_write_failure_result_creates_correct_json(tmp_path):
    """_write_failure_result should create <task_id>.json with expected fields."""
    import supervisor.workers as workers

    orig = workers.DRIVE_ROOT
    workers.DRIVE_ROOT = tmp_path
    try:
        workers._write_failure_result("abc123")
    finally:
        workers.DRIVE_ROOT = orig

    result_file = tmp_path / "task_results" / "abc123.json"
    assert result_file.exists()

    data = json.loads(result_file.read_text(encoding="utf-8"))
    assert data["task_id"] == "abc123"
    assert data["status"] == "failed"
    assert isinstance(data["result"], str) and len(data["result"]) > 0
    assert data["cost_usd"] == 0
    assert data["total_rounds"] == 0
    assert "ts" in data


def test_write_failure_result_does_not_overwrite_existing(tmp_path):
    """_write_failure_result must NOT overwrite an existing result file."""
    import supervisor.workers as workers

    orig = workers.DRIVE_ROOT
    workers.DRIVE_ROOT = tmp_path
    try:
        results_dir = tmp_path / "task_results"
        results_dir.mkdir(parents=True, exist_ok=True)
        existing = {"task_id": "xyz789", "status": "completed", "result": "Success!"}
        (results_dir / "xyz789.json").write_text(
            json.dumps(existing, ensure_ascii=False), encoding="utf-8"
        )
        workers._write_failure_result("xyz789")
    finally:
        workers.DRIVE_ROOT = orig

    data = json.loads((results_dir / "xyz789.json").read_text(encoding="utf-8"))
    assert data["status"] == "completed", "Existing result was overwritten!"
    assert data["result"] == "Success!"


def test_write_failure_result_none_task_id(tmp_path):
    """_write_failure_result with None task_id should not crash or create files."""
    import supervisor.workers as workers

    orig = workers.DRIVE_ROOT
    workers.DRIVE_ROOT = tmp_path
    try:
        workers._write_failure_result(None)
        workers._write_failure_result("")
    finally:
        workers.DRIVE_ROOT = orig

    results_dir = tmp_path / "task_results"
    if results_dir.exists():
        assert list(results_dir.iterdir()) == [], "Files created for None/empty task_id"


# ---------------------------------------------------------------------------
# drain_all_pending
# ---------------------------------------------------------------------------

def test_drain_all_pending_returns_and_clears(tmp_path):
    """drain_all_pending should return all tasks and leave PENDING empty."""
    import supervisor.queue as queue

    orig_drive = queue.DRIVE_ROOT
    orig_pending = queue.PENDING
    queue.DRIVE_ROOT = tmp_path
    tasks = [{"id": "t1", "type": "task"}, {"id": "t2", "type": "evolution"}]
    queue.PENDING = list(tasks)
    try:
        with mock.patch.object(queue, "persist_queue_snapshot"):
            drained = queue.drain_all_pending()
    finally:
        queue.DRIVE_ROOT = orig_drive
        queue.PENDING = orig_pending

    assert drained == tasks
    # The local list that was assigned to queue.PENDING was cleared
    assert len(drained) == 2


# ---------------------------------------------------------------------------
# kill_workers — zombie prevention integration
# ---------------------------------------------------------------------------

def test_kill_workers_writes_failure_for_running_and_pending(tmp_path):
    """kill_workers should write failure results for both RUNNING and PENDING tasks."""
    import supervisor.workers as workers
    import supervisor.queue as queue

    # Save originals
    orig_drive = workers.DRIVE_ROOT
    orig_workers = dict(workers.WORKERS)
    orig_running = dict(workers.RUNNING)
    orig_pending = list(workers.PENDING)
    orig_q_drive = queue.DRIVE_ROOT
    orig_q_pending = queue.PENDING
    orig_q_running = queue.RUNNING

    workers.DRIVE_ROOT = tmp_path
    queue.DRIVE_ROOT = tmp_path
    workers.WORKERS.clear()
    workers.RUNNING.clear()
    workers.RUNNING["run1"] = {"task": {"id": "run1", "type": "task"}, "worker_id": 0}
    workers.RUNNING["run2"] = {"task": {"id": "run2", "type": "task"}, "worker_id": 1}

    pending_tasks = [{"id": "pend1", "type": "evolution"}, {"id": "pend2", "type": "task"}]
    workers.PENDING[:] = list(pending_tasks)
    queue.PENDING = workers.PENDING

    try:
        with mock.patch.object(queue, "persist_queue_snapshot"):
            workers.kill_workers()
    finally:
        workers.DRIVE_ROOT = orig_drive
        workers.WORKERS.clear()
        workers.WORKERS.update(orig_workers)
        workers.RUNNING.clear()
        workers.RUNNING.update(orig_running)
        workers.PENDING[:] = orig_pending
        queue.DRIVE_ROOT = orig_q_drive
        queue.PENDING = orig_q_pending
        queue.RUNNING = orig_q_running

    results_dir = tmp_path / "task_results"
    for tid in ("run1", "run2", "pend1", "pend2"):
        path = results_dir / f"{tid}.json"
        assert path.exists(), f"Missing failure result for {tid}"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["status"] == "failed"
        assert data["task_id"] == tid
