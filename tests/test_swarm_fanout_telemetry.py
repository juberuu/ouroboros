"""WS8: swarm_fanout telemetry shape + reject-meta marker."""
from __future__ import annotations

import json
import types

from ouroboros.tools.control import _emit_swarm_fanout
from supervisor.events import _subagent_rejection_meta, _subagent_scheduled_meta


def test_swarm_fanout_event_shape(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    ctx = types.SimpleNamespace(drive_logs=lambda: logs, _last_wave_ts=0.0)
    _emit_swarm_fanout(
        ctx,
        parent_task_id="p1",
        root_task_id="r1",
        depth=2,
        task_group_id="subagents-x",
        task_ids=["a", "b"],
        role="researcher",
        requested_model_lane="auto",
        effective_model_lanes=["light", "light"],
        objective="o" * 300,
        emitted_live=True,
    )
    lines = (logs / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    evt = json.loads(lines[0])
    assert evt["type"] == "swarm_fanout"
    # Must not be foldable into a grouped-task lane or rendered as a subagent card.
    assert not evt["type"].startswith(("task_", "llm_", "tool_"))
    assert "delegation_role" not in evt and "subagent_task_id" not in evt
    assert evt["requested_count"] == 2 and evt["task_ids"] == ["a", "b"]
    assert evt["slot_count"] == 2
    assert len(evt["objective_preview"]) == 200
    assert evt["inter_wave_latency_sec"] is None  # first wave (prev ts was 0)


def test_swarm_fanout_inter_wave_latency_on_second_wave(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    ctx = types.SimpleNamespace(drive_logs=lambda: logs, _last_wave_ts=0.0)
    for _ in range(2):
        _emit_swarm_fanout(
            ctx, parent_task_id="p", root_task_id="r", depth=1,
            task_group_id="", task_ids=["x"], role="r",
            requested_model_lane="auto", effective_model_lanes=["light"],
            objective="o", emitted_live=False,
        )
    evts = [json.loads(line) for line in (logs / "events.jsonl").read_text().splitlines()]
    assert len(evts) == 2
    assert evts[0]["inter_wave_latency_sec"] is None
    assert isinstance(evts[1]["inter_wave_latency_sec"], float)


def test_reject_meta_marks_not_accepted():
    meta = _subagent_rejection_meta(
        "t1", root_task_id="r1", parent_id="p1", role="x", status="failed", error="e",
    )
    assert meta.get("accepted") is False


def test_scheduled_meta_marks_accepted():
    meta = _subagent_scheduled_meta(
        tid="t1",
        role="researcher",
        task_constraint={"surface": "external_workspace"},
        task_group_id="g1",
        requested_model_lane="auto",
        effective_model_lane="light",
        active_subagent_count=2,
        max_active_subagents=6,
    )
    assert meta["accepted"] is True
    assert meta["active_subagent_count"] == 2
    assert meta["max_active_subagents"] == 6
    assert meta["subagent_event"] == "scheduled"
    assert meta["write_surface"] == "external_workspace"
