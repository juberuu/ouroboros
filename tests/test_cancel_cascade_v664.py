"""Regression coverage for snapshot-before-teardown subtree cancellation."""

from __future__ import annotations


def _isolate_queue(monkeypatch, tmp_path, tasks):
    from supervisor import queue
    from supervisor import workers

    pending = [dict(task) for task in tasks]
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", pending)
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {}, raising=False)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    return queue, pending


def test_root_cancel_snapshots_and_cancels_whole_live_subtree(tmp_path, monkeypatch):
    from ouroboros.task_results import STATUS_CANCELLED, load_task_result

    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root", "depth": 0},
            {
                "id": "child",
                "chat_id": 0,
                "root_task_id": "root",
                "parent_task_id": "root",
                "depth": 1,
            },
            {
                "id": "grandchild",
                "chat_id": 0,
                "root_task_id": "root",
                "parent_task_id": "child",
                "depth": 2,
            },
            {"id": "other", "chat_id": 0, "root_task_id": "other", "depth": 0},
        ],
    )
    order: list[str] = []
    monkeypatch.setattr(queue, "_emit_cancel_task_done", lambda _task, task_id, **_kw: order.append(task_id))

    assert queue.cancel_task_by_id("root", cascade=True) is True

    assert order == ["grandchild", "child", "root"]
    assert [task["id"] for task in pending] == ["other"]
    for task_id in ("root", "child", "grandchild"):
        assert load_task_result(tmp_path, task_id)["status"] == STATUS_CANCELLED


def test_midtree_cancel_uses_combined_pending_lineage(tmp_path, monkeypatch):
    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root"},
            {"id": "child", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root"},
            {
                "id": "grandchild",
                "chat_id": 0,
                "root_task_id": "root",
                "parent_task_id": "child",
            },
        ],
    )

    assert queue.cancel_task_by_id("child", cascade=True) is True
    assert [task["id"] for task in pending] == ["root"]


def test_default_cancel_preserves_live_children(tmp_path, monkeypatch):
    queue, pending = _isolate_queue(
        monkeypatch,
        tmp_path,
        [
            {"id": "root", "chat_id": 0, "root_task_id": "root"},
            {"id": "child", "chat_id": 0, "root_task_id": "root", "parent_task_id": "root"},
        ],
    )

    assert queue.cancel_task_by_id("root") is True
    assert [task["id"] for task in pending] == ["child"]
