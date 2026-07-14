"""Owner-facing heartbeat presentation regressions for v6.64."""

from __future__ import annotations

import inspect
import json
import pathlib


def test_routine_heartbeat_is_not_rendered_as_chat_message() -> None:
    from supervisor import queue

    source = inspect.getsource(queue._enforce_task_timeouts_locked)
    assert "running for" not in source
    assert "heartbeat_lag=" not in source


def test_liveness_and_incident_controls_remain_active() -> None:
    from supervisor import queue

    source = inspect.getsource(queue._enforce_task_timeouts_locked)
    for invariant in (
        "last_heartbeat_at",
        "get_task_idle_timeout_sec",
        "get_task_abs_ceiling_sec",
        "deadline_reached",
        "finalization_requested_at",
        '"task_incident": terminal_reason',
        '"is_progress": True',
    ):
        assert invariant in source


def test_retired_timeout_defaults_are_quiet_but_custom_value_is_loud(tmp_path, monkeypatch) -> None:
    from supervisor import queue

    monkeypatch.setattr(queue, "_timeout_deprecation_emitted", False)
    queue.init(tmp_path, 600, 1800)
    events = tmp_path / "logs" / "events.jsonl"
    assert not events.exists()

    queue.init(tmp_path, 601, 1800)
    row = json.loads(events.read_text(encoding="utf-8"))
    assert row["type"] == "deprecated_settings_ignored"
    assert row["keys"] == ["OUROBOROS_SOFT_TIMEOUT_SEC"]


def test_owner_visible_incidents_use_canonical_message_seam() -> None:
    repo = pathlib.Path(__file__).resolve().parents[1]
    for relpath in ("server.py", "supervisor/workers.py"):
        source = (repo / relpath).read_text(encoding="utf-8")
        assert "get_bridge().send_message(" not in source
        assert "bridge.send_message(" not in source


def test_cancel_failure_is_progress_incident_not_chat_bubble() -> None:
    from supervisor.events import _handle_cancel_task

    sent = []

    class _Ctx:
        @staticmethod
        def load_state():
            return {"owner_chat_id": 9}

        @staticmethod
        def cancel_task_by_id(task_id):
            assert task_id == "cancel-me"
            return False

        @staticmethod
        def send_with_budget(*args, **kwargs):
            sent.append((args, kwargs))

    _handle_cancel_task({"task_id": "cancel-me"}, _Ctx())

    assert len(sent) == 1
    args, kwargs = sent[0]
    assert args == (9, "❌ cancel cancel-me (event)")
    assert kwargs == {
        "is_progress": True,
        "task_id": "cancel-me",
        "progress_meta": {
            "task_incident": "cancellation_fault",
            "toast_once": "cancel-me:cancellation_fault",
        },
    }
