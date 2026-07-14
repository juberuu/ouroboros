"""Focused durability coverage for abnormal terminal and replay-safe paths."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _available_cost_fields(*, calls: int = 0, degraded: bool = False) -> dict:
    return {
        "cost_accounting_status": "available",
        "cost_usd": 0.0,
        "total_rounds": calls,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_final": True,
        "reserved_usd": 0.0,
        "unresolved_upper_bound_usd": 0.0,
        "unknown_unmetered": 0,
        "ledger_integrity_degraded": degraded,
    }


def test_headless_worker_crash_emits_task_done_without_main_chat_reroute(tmp_path, monkeypatch):
    from supervisor import queue, workers

    class DeadProc:
        pid = None
        exitcode = -11

        @staticmethod
        def is_alive():
            return False

        @staticmethod
        def join(timeout=None):
            del timeout

    task = {"id": "headless-crash", "type": "task", "chat_id": 0, "_attempt": 1}
    worker = SimpleNamespace(wid=0, busy_task_id=task["id"], proc=DeadProc(), reaping=False)
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "WORKERS", {0: worker})
    monkeypatch.setattr(workers, "RUNNING", {
        task["id"]: {
            "task": task,
            "started_at": 1.0,
            "last_heartbeat_at": 1.0,
            "attempt": 1,
        },
    })
    monkeypatch.setattr(workers, "QUEUE_MAX_RETRIES", 1)
    monkeypatch.setattr(workers, "_LAST_SPAWN_TIME", 0)
    monkeypatch.setattr(workers, "CRASH_TS", [])
    events = []
    monkeypatch.setattr(workers, "get_event_q", lambda: SimpleNamespace(put=events.append))
    monkeypatch.setattr(workers, "reconstruct_task_cost", lambda *_a, **_k: _available_cost_fields())
    monkeypatch.setattr(workers, "respawn_worker", lambda _wid: None)
    monkeypatch.setattr(workers, "send_with_budget", lambda *_a, **_k: None)
    monkeypatch.setattr(workers, "load_state", lambda: {})
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(queue, "enqueue_task", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.tools.services.archive_task_service_logs", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.task_results.load_task_result", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.task_results.write_task_result", lambda *_a, **_k: None)

    workers.ensure_workers_healthy()

    terminal = [event for event in events if event.get("type") == "task_done"]
    assert len(terminal) == 1
    assert terminal[0]["task_id"] == task["id"]
    assert terminal[0]["chat_id"] == 0


def test_headless_pending_cancel_still_emits_task_done(tmp_path, monkeypatch):
    from supervisor import queue, workers

    task = {"id": "headless-cancel", "type": "task", "chat_id": 0}
    events = []
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [task])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "get_event_q", lambda: SimpleNamespace(put=events.append))
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)

    assert queue.cancel_task_by_id(task["id"]) is True

    terminal = [event for event in events if event.get("type") == "task_done"]
    assert len(terminal) == 1
    assert terminal[0]["task_id"] == task["id"]
    assert terminal[0]["chat_id"] == 0
    assert terminal[0]["status"] == "cancelled"


def _patch_reaper(tmp_path, monkeypatch):
    from supervisor import queue, workers

    events = []
    enqueued = []
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(queue, "QUEUE_MAX_RETRIES", 1)
    monkeypatch.setattr(queue, "reconstruct_task_cost", lambda *_a, **_k: _available_cost_fields())
    monkeypatch.setattr(queue, "enqueue_task", lambda task, front=False: enqueued.append((dict(task), front)))
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(queue, "_kept_service_pids", lambda: set(), raising=False)
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "get_event_q", lambda: SimpleNamespace(put=events.append))
    monkeypatch.setattr("ouroboros.tools.services.archive_task_service_logs", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.headless.copy_child_task_result", lambda *_a, **_k: None)
    monkeypatch.setattr("ouroboros.observability.latest_llm_response_text", lambda *_a, **_k: "")
    monkeypatch.setattr("ouroboros.owner_mailbox.cleanup_task_mailbox", lambda *_a, **_k: None)
    return events, enqueued


def test_headless_reaper_still_emits_task_done(tmp_path, monkeypatch):
    from supervisor.task_reaper import reap_timed_out_task

    events, enqueued = _patch_reaper(tmp_path, monkeypatch)
    reap_timed_out_task({
        "worker_id": 4,
        "proc": None,
        "task_id": "headless-reaped",
        "task": {"id": "headless-reaped", "type": "task", "chat_id": 0},
        "task_type": "task",
        "terminal_reason": "idle_timeout",
        "attempt": 1,
        "owner_chat_id": 0,
        "will_retry": False,
    })

    terminal = [event for event in events if event.get("type") == "task_done"]
    assert enqueued == []
    assert len(terminal) == 1
    assert terminal[0]["task_id"] == "headless-reaped"
    assert terminal[0]["chat_id"] == 0


def test_reaper_admission_block_terminalizes_retry(tmp_path, monkeypatch):
    from ouroboros.task_results import STATUS_FAILED, load_task_result
    from supervisor import queue
    from supervisor.task_reaper import reap_timed_out_task

    events, _ = _patch_reaper(tmp_path, monkeypatch)
    monkeypatch.setattr(
        queue,
        "enqueue_task",
        lambda *_args, **_kwargs: {"_admission_blocked": "task_acceptance_fence"},
    )

    reap_timed_out_task({
        "worker_id": 4,
        "proc": None,
        "task_id": "fenced-retry",
        "task": {"id": "fenced-retry", "type": "task", "chat_id": 0},
        "task_type": "task",
        "terminal_reason": "idle_timeout",
        "attempt": 1,
        "owner_chat_id": 0,
        "will_retry": True,
    })

    result = load_task_result(tmp_path, "fenced-retry")
    assert result["status"] == STATUS_FAILED
    assert result["reason_code"] == "idle_timeout_retry_admission_blocked"
    terminal = [event for event in events if event.get("type") == "task_done"]
    assert terminal and terminal[-1]["status"] == "failed"


def test_assign_keeps_unsafe_pending_when_terminal_write_is_not_durable(tmp_path, monkeypatch):
    from supervisor import queue, state, workers

    task = {
        "id": "unsafe-write-failure",
        "type": "task",
        "chat_id": 0,
        "_attempt": 2,
        "original_task_id": "first-attempt",
    }
    events = []
    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(workers, "PENDING", [task])
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "load_state", lambda: {"owner_chat_id": 0})
    monkeypatch.setattr(workers, "reconstruct_task_cost", lambda *_a, **_k: _available_cost_fields(calls=1))
    monkeypatch.setattr(workers, "get_event_q", lambda: SimpleNamespace(put=events.append))
    monkeypatch.setattr(state, "budget_remaining", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(
        "ouroboros.task_results.write_task_result",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("durable write failed")),
    )

    workers.assign_tasks()

    assert [item["id"] for item in workers.PENDING] == [task["id"]]
    assert "_budget_pause" not in workers.PENDING[0]
    assert not any(event.get("type") == "task_done" for event in events)


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    (("quarantined_tail", "replay_unsafe"), ("midstream", "accounting_unavailable")),
)
def test_corrupt_or_integrity_degraded_ledger_never_permits_budget_resume(
    tmp_path, monkeypatch, corruption, expected_error,
):
    from ouroboros import usage_accounting as accounting
    from supervisor import queue, state, workers

    state.init(tmp_path, total_budget_limit=10.0)
    queue.init(tmp_path, 600, 1800)
    monkeypatch.setattr(queue, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue, "PENDING", [{
        "id": "replay-risk",
        "type": "task",
        "chat_id": 0,
        "_budget_pause": {
            "status": "paused_before_dispatch",
            "physical_calls": 0,
            "replay_safe": True,
            "auto_resume": False,
        },
    }])
    monkeypatch.setattr(queue, "RUNNING", {})
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(queue, "persist_queue_snapshot", lambda reason="": None)

    reservation = accounting.reserve_attempt(accounting.AttemptRequest(
        model="test/model",
        provider="test",
        drive_root=tmp_path,
        task_id="replay-risk",
        root_task_id="replay-risk",
        reservation_usd=0.01,
        global_limit_usd=10.0,
    ))
    accounting.release_attempt(reservation, "test_setup")
    ledger = tmp_path / accounting.LEDGER_REL
    if corruption == "quarantined_tail":
        with ledger.open("ab") as handle:
            handle.write(b'{"seq":')
    else:
        lines = ledger.read_text(encoding="utf-8").splitlines()
        ledger.write_text(lines[0] + "\nnot-json\n" + lines[1] + "\n", encoding="utf-8")

    result = queue.resume_budget_paused_task("replay-risk")

    assert result == {
        "ok": False,
        "error": expected_error,
        "action": "cancel_or_new_run",
    }
    assert "_budget_pause" in queue.PENDING[0]


def test_reaper_suppresses_retry_when_terminal_result_write_fails(tmp_path, monkeypatch):
    from supervisor.task_reaper import reap_timed_out_task

    events, enqueued = _patch_reaper(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "ouroboros.task_results.write_task_result",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("durable write failed")),
    )

    reap_timed_out_task({
        "worker_id": 7,
        "proc": None,
        "task_id": "retry-needs-terminal",
        "task": {"id": "retry-needs-terminal", "type": "task", "chat_id": 0},
        "task_type": "task",
        "terminal_reason": "idle_timeout",
        "attempt": 1,
        "owner_chat_id": 0,
        "will_retry": True,
        "retry_task_id": "retry-needs-terminal",
    })

    assert enqueued == []
    terminal = [event for event in events if event.get("type") == "task_done"]
    assert len(terminal) == 1
    assert terminal[0]["task_id"] == "retry-needs-terminal"
    assert terminal[0]["status"] == "failed"
