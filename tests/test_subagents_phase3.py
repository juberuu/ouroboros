from __future__ import annotations

import json
import queue


def test_subagent_lane_resolution_fans_out_and_depth_coerces_light(monkeypatch):
    import ouroboros.subagents as subagents
    from ouroboros.subagents import expand_subagent_lane_slots

    monkeypatch.setattr(subagents, "get_review_models", lambda: ["review-a", "review-b"])
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "light-model")

    review_slots = expand_subagent_lane_slots("review", depth=1)

    assert [slot.model for slot in review_slots] == ["review-a", "review-b"]
    assert {slot.effective_lane for slot in review_slots} == {"review"}
    assert [slot.slot_count for slot in review_slots] == [2, 2]

    nested_slots = expand_subagent_lane_slots("review", depth=2)

    assert len(nested_slots) == 1
    assert nested_slots[0].requested_lane == "review"
    assert nested_slots[0].effective_lane == "light"
    assert nested_slots[0].model == "light-model"


def test_schedule_subagent_review_lane_emits_task_group_metadata(monkeypatch, tmp_path):
    import ouroboros.subagents as subagents
    from ouroboros.task_results import STATUS_REQUESTED
    from ouroboros.tools.control import _schedule_task
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr(subagents, "get_review_models", lambda: ["review-a", "review-b"])
    event_queue: queue.Queue = queue.Queue()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = event_queue
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}

    result = _schedule_task(
        ctx,
        objective="Review the design",
        expected_output="Two independent findings lists",
        role="reviewer",
        model_lane="review",
    )

    assert result.startswith("Subagent group queued")
    events = [event_queue.get_nowait(), event_queue.get_nowait()]
    group_ids = {event["task_group_id"] for event in events}
    assert len(group_ids) == 1
    assert all(event["requested_model_lane"] == "review" for event in events)
    assert [event["model"] for event in events] == ["review-a", "review-b"]
    assert all(event["subagent_envelope"]["status"] == STATUS_REQUESTED for event in events)
    assert all(event["subagent_envelope"]["lineage"]["root_task_id"] == "root1" for event in events)
    assert all(event["task_group"]["size"] == 2 for event in events)

    for event in events:
        path = tmp_path / "task_results" / f"{event['task_id']}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["task_group_id"] == event["task_group_id"]
        assert data["model"] == event["model"]
        assert data["effective_model_lane"] == "review"


def test_schedule_subagent_group_drive_failure_is_fail_closed(monkeypatch, tmp_path):
    import ouroboros.subagents as subagents
    import ouroboros.tools.control as control
    from ouroboros.headless import HEADLESS_TASKS_DIR, task_state_dir
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setattr(subagents, "get_review_models", lambda: ["review-a", "review-b"])
    created: list[str] = []

    def fake_prepare(_root, tid, _mode):
        child = task_state_dir(tmp_path, tid) / "data"
        child.mkdir(parents=True)
        created.append(tid)
        if len(created) == 2:
            raise RuntimeError("boom")
        return child

    monkeypatch.setattr(control, "prepare_task_drive", fake_prepare)
    event_queue: queue.Queue = queue.Queue()
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = event_queue
    ctx.task_metadata = {"root_task_id": "root1", "session_id": "sess1"}

    result = control._schedule_task(
        ctx,
        objective="Review the design",
        expected_output="Two independent findings lists",
        role="reviewer",
        model_lane="review",
    )

    assert "SUBTASK_DRIVE_ERROR" in result
    assert event_queue.empty()
    assert not any((tmp_path / "task_results").glob("*.json"))
    assert not any((tmp_path / HEADLESS_TASKS_DIR).glob("*"))
