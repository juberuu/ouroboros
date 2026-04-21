"""Tests for evolution/consciousness status snapshots."""

from unittest.mock import MagicMock, patch


def test_evolution_status_waits_for_owner_chat(monkeypatch):
    from supervisor import queue as queue_module

    monkeypatch.setattr(queue_module, "PENDING", [])
    monkeypatch.setattr(queue_module, "RUNNING", {})
    monkeypatch.setattr(
        queue_module,
        "load_state",
        lambda: {
            "evolution_mode_enabled": True,
            "owner_chat_id": None,
            "evolution_cycle": 3,
            "evolution_consecutive_failures": 0,
            "last_evolution_task_at": "",
        },
    )
    monkeypatch.setattr(queue_module, "budget_remaining", lambda st: 25.0)

    snapshot = queue_module.get_evolution_status_snapshot()

    assert snapshot["status"] == "waiting_for_owner_chat"
    assert snapshot["enabled"] is True
    assert snapshot["owner_chat_bound"] is False


def test_evolution_status_reports_waiting_for_idle(monkeypatch):
    from supervisor import queue as queue_module

    monkeypatch.setattr(queue_module, "PENDING", [{"id": "task-1", "type": "task"}])
    monkeypatch.setattr(queue_module, "RUNNING", {})
    monkeypatch.setattr(
        queue_module,
        "load_state",
        lambda: {
            "evolution_mode_enabled": True,
            "owner_chat_id": 7,
            "evolution_cycle": 4,
            "evolution_consecutive_failures": 0,
            "last_evolution_task_at": "",
        },
    )
    monkeypatch.setattr(queue_module, "budget_remaining", lambda st: 25.0)

    snapshot = queue_module.get_evolution_status_snapshot()

    assert snapshot["status"] == "waiting_for_idle"
    assert snapshot["pending_count"] == 1


def test_evolution_status_reports_budget_stop_when_disabled_after_run(monkeypatch):
    from supervisor import queue as queue_module

    monkeypatch.setattr(queue_module, "PENDING", [])
    monkeypatch.setattr(queue_module, "RUNNING", {})
    monkeypatch.setattr(
        queue_module,
        "load_state",
        lambda: {
            "evolution_mode_enabled": False,
            "owner_chat_id": 7,
            "evolution_cycle": 6,
            "evolution_consecutive_failures": 0,
            "last_evolution_task_at": "2026-03-31T10:00:00Z",
        },
    )
    monkeypatch.setattr(queue_module, "budget_remaining", lambda st: 1.25)

    snapshot = queue_module.get_evolution_status_snapshot()

    assert snapshot["status"] == "budget_stopped"
    assert snapshot["budget_remaining_usd"] == 1.25


def test_consciousness_status_snapshot_exposes_runtime_fields():
    from ouroboros.consciousness import BackgroundConsciousness

    with patch.object(BackgroundConsciousness, "_build_registry", return_value=MagicMock()):
        consciousness = BackgroundConsciousness(
            drive_root=MagicMock(),
            repo_dir=MagicMock(),
            event_queue=None,
            owner_chat_id_fn=lambda: 1,
        )

    consciousness.pause()
    consciousness._next_wakeup_sec = 180
    snapshot = consciousness.status_snapshot()

    assert snapshot["paused"] is True
    assert snapshot["next_wakeup_sec"] == 180
    assert snapshot["last_idle_reason"] == "paused_by_active_task"
