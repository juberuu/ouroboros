"""Tests for evolution/consciousness status snapshots."""

import json
from unittest.mock import MagicMock, patch

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient


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


def test_evolution_data_strips_legacy_checkpoint_result_status(tmp_path, monkeypatch):
    from ouroboros.evolution_checkpoints import CHECKPOINTS_REL
    from ouroboros.gateway import control

    repo = tmp_path / "repo"
    repo.mkdir()
    checkpoint_path = tmp_path / CHECKPOINTS_REL
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(
        json.dumps({
            "task_id": "evo-legacy",
            "status": "completed",
            "result_status": "failed",
            "reason_code": "legacy_error",
            "loop_outcome": {
                "result_status": "failed",
                "compat_result_status": "failed",
            },
        })
        + "\n",
        encoding="utf-8",
    )

    async def fake_collect_metrics(*_args, **_kwargs):
        return []

    monkeypatch.setattr("ouroboros.utils.collect_evolution_metrics", fake_collect_metrics)
    control._evo_cache.clear()
    control._evo_task = None
    app = Starlette(routes=[Route("/api/evolution-data", endpoint=control.api_evolution_data)])
    app.state.drive_root = tmp_path
    app.state.repo_dir = repo

    payload = TestClient(app).get("/api/evolution-data?force=1").json()

    checkpoint = payload["checkpoints"][0]
    assert "result_status" not in checkpoint
    assert "result_status" not in checkpoint["loop_outcome"]
    assert "compat_result_status" not in checkpoint["loop_outcome"]
    assert checkpoint["outcome_axes"]["execution"]["status"] == "failed"
