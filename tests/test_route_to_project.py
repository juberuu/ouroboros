"""route_to_project + list_projects (v6.33.0 WS10 LLM-first routing)."""

from __future__ import annotations

import types
import json

from ouroboros.projects_registry import create_project
from ouroboros.tools.control import _list_projects, _route_to_project, get_tools


def _ctx(tmp_path, events=None, *, task_metadata=None):
    return types.SimpleNamespace(
        pending_events=events if events is not None else [],
        event_queue=None,
        current_chat_id=1,
        drive_root=tmp_path,
        task_metadata=task_metadata or {},
    )


def test_route_to_existing_project_emits_event_and_receipt(tmp_path):
    create_project(tmp_path, "racer", name="Racer")
    chat_path = tmp_path / "logs" / "chat.jsonl"
    chat_path.parent.mkdir(parents=True)
    chat_path.write_text(json.dumps({
        "direction": "in",
        "chat_id": 1,
        "client_message_id": "owner-route-1",
        "ts": "2026-07-14T12:00:00Z",
        "text": "continue the engine tuning",
    }) + "\n", encoding="utf-8")
    events = []
    ctx = _ctx(tmp_path, events, task_metadata={"client_message_id": "owner-route-1"})
    out = _route_to_project(ctx, "racer", "continue the engine tuning", reason="follow-up")
    assert out.startswith("✉️ Routed to project 'Racer' (racer)")
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "promote_chat_to_task"
    assert evt["project_id"] == "racer"
    assert evt["routed_from_main"] is True
    assert "continue the engine tuning" in evt["objective"]
    assert "routing reason: follow-up" in evt["objective"]
    assert evt["chat_id"] == 1
    assert evt["task_id"]
    assert evt["source_ref"]["client_message_id"] == "owner-route-1"
    assert evt["source_ref"]["chat_id"] == 1
    assert ctx._typed_routing_action_emitted == "route_to_project"


def test_route_to_missing_project_emits_typed_manual_target(tmp_path):
    events = []
    metadata = {
        "client_message_id": "owner-1",
        "routing_contract": {"manual_options": [{"task_id": "task-1", "title": "Fix it"}]},
    }
    ctx = _ctx(tmp_path, events, task_metadata=metadata)
    out = _route_to_project(ctx, "ghost", "do the thing")
    assert "NEEDS_MANUAL_TARGET" in out
    assert events == [{
        "type": "routing_manual_target",
        "chat_id": 1,
        "client_message_id": "owner-1",
        "requested_target": "ghost",
        "reason": "target_not_found",
        "options": [{"task_id": "task-1", "title": "Fix it"}],
        "ts": events[0]["ts"],
    }]
    assert ctx._typed_routing_action_emitted == "routing_manual_target"


def test_route_rejects_dirty_project_id(tmp_path):
    events = []
    out = _route_to_project(_ctx(tmp_path, events), "Bad Name!", "msg")
    assert "NEEDS_MANUAL_TARGET" in out
    assert events[0]["reason"] == "invalid_project_id"


def test_route_empty_target_is_the_typed_abstention_path(tmp_path):
    events = []
    metadata = {
        "client_message_id": "owner-2",
        "routing_contract": {
            "manual_options": [{"action": "new_task_in_project", "label": "New task in Project"}],
        },
    }
    out = _route_to_project(_ctx(tmp_path, events, task_metadata=metadata), "", "ambiguous follow-up")
    assert "NEEDS_MANUAL_TARGET" in out
    assert events[0]["reason"] == "target_unspecified"
    assert events[0]["options"][0]["label"] == "New task in Project"


def test_route_requires_message(tmp_path):
    create_project(tmp_path, "racer", name="Racer")
    events = []
    ctx = _ctx(tmp_path, events)
    out = _route_to_project(ctx, "racer", "   ")
    assert "TOOL_ARG_ERROR" in out
    assert events == []
    assert not hasattr(ctx, "_typed_routing_action_emitted")


def test_list_projects_lists_created_projects(tmp_path):
    create_project(tmp_path, "racer", name="Racer")
    create_project(tmp_path, "site", name="Marketing Site")
    out = _list_projects(_ctx(tmp_path))
    assert "racer" in out and "Racer" in out
    assert "site" in out and "Marketing Site" in out


def test_list_projects_empty(tmp_path):
    out = _list_projects(_ctx(tmp_path))
    assert "No projects yet" in out


def test_route_tool_uncertainty_contract_requires_manual_target():
    tool = next(entry for entry in get_tools() if entry.name == "route_to_project")
    description = tool.schema["description"]
    assert "needs_manual_target" in description
    assert "New task in Project" in description
    assert "answer inline and offer" not in description
