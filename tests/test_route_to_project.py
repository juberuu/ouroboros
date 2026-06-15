"""route_to_project + list_projects (v6.33.0 WS10 LLM-first routing)."""

from __future__ import annotations

import types

from ouroboros.projects_registry import create_project
from ouroboros.tools.control import _list_projects, _route_to_project


def _ctx(tmp_path, events=None):
    return types.SimpleNamespace(
        pending_events=events if events is not None else [],
        event_queue=None,
        current_chat_id=1,
        drive_root=tmp_path,
    )


def test_route_to_existing_project_emits_event_and_receipt(tmp_path):
    create_project(tmp_path, "racer", name="Racer")
    events = []
    out = _route_to_project(_ctx(tmp_path, events), "racer", "continue the engine tuning", reason="follow-up")
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


def test_route_to_missing_project_is_not_found_no_event(tmp_path):
    events = []
    out = _route_to_project(_ctx(tmp_path, events), "ghost", "do the thing")
    assert "ROUTE_TARGET_NOT_FOUND" in out
    assert events == []


def test_route_rejects_dirty_project_id(tmp_path):
    events = []
    out = _route_to_project(_ctx(tmp_path, events), "Bad Name!", "msg")
    assert "TOOL_ARG_ERROR" in out
    assert events == []


def test_route_requires_message(tmp_path):
    create_project(tmp_path, "racer", name="Racer")
    events = []
    out = _route_to_project(_ctx(tmp_path, events), "racer", "   ")
    assert "TOOL_ARG_ERROR" in out
    assert events == []


def test_list_projects_lists_created_projects(tmp_path):
    create_project(tmp_path, "racer", name="Racer")
    create_project(tmp_path, "site", name="Marketing Site")
    out = _list_projects(_ctx(tmp_path))
    assert "racer" in out and "Racer" in out
    assert "site" in out and "Marketing Site" in out


def test_list_projects_empty(tmp_path):
    out = _list_projects(_ctx(tmp_path))
    assert "No projects yet" in out
