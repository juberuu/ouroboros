"""Adversarial state-machine coverage for `/evolve stop` and abnormal-termination
cost reconstruction.

Covers:
  - reconstruct_task_cost summing the durable llm_usage SSOT by task_id;
  - cancel_running_evolution_tasks cancelling only evolution workers;
  - the hard-timeout retry gate: a killed evolution task is NOT re-enqueued when
    the campaign is stopped, IS re-enqueued when still enabled, and either way
    records reconstructed cost/rounds (never zeros).
"""

import json
from types import SimpleNamespace



def _write_events(tmp_path, rows):
    logs = tmp_path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / "events.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_reconstruct_task_cost_sums_llm_usage(tmp_path, monkeypatch):
    import supervisor.state as state

    monkeypatch.setattr(state, "DRIVE_ROOT", tmp_path)
    _write_events(tmp_path, [
        {"type": "llm_usage", "task_id": "t1", "cost": 0.5, "prompt_tokens": 100, "completion_tokens": 20},
        {"type": "llm_usage", "task_id": "t1", "cost": 0.25, "prompt_tokens": 50, "completion_tokens": 10},
        {"type": "llm_usage", "task_id": "other", "cost": 9.0, "prompt_tokens": 1, "completion_tokens": 1},
        {"type": "llm_round", "task_id": "t1", "cost_usd": 99},  # not llm_usage -> ignored
    ])

    cost, rounds, prompt, completion = state.reconstruct_task_cost("t1")
    assert round(cost, 6) == 0.75
    assert rounds == 2
    assert prompt == 150
    assert completion == 30
    # Unknown / empty task ids reconstruct to zeros, never raise.
    assert state.reconstruct_task_cost("missing") == (0.0, 0, 0, 0)
    assert state.reconstruct_task_cost("") == (0.0, 0, 0, 0)


def test_cancel_running_evolution_tasks_cancels_only_evolution(monkeypatch):
    import supervisor.queue as q

    monkeypatch.setattr(q, "RUNNING", {
        "evo1": {"task": {"type": "evolution"}},
        "task2": {"task": {"type": "task"}},
        "evo3": {"task": {"type": "evolution"}},
    })
    cancelled = []
    monkeypatch.setattr(q, "cancel_task_by_id", lambda tid: cancelled.append(tid) or True)

    out = q.cancel_running_evolution_tasks("test stop")

    assert sorted(out) == ["evo1", "evo3"]
    assert sorted(cancelled) == ["evo1", "evo3"]


def _drive_hard_timeout(tmp_path, monkeypatch, *, evolution_enabled):
    """Drive enforce_task_timeouts for a single overdue evolution task and return
    (enqueued, emitted_events, written_result)."""
    import time

    import supervisor.queue as q
    import supervisor.state as state
    from supervisor import workers as workers_mod
    import ouroboros.tools.services as services_mod

    monkeypatch.setattr(q, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(state, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(q, "FINALIZATION_GRACE_SEC", 0)
    # Activity model: drive an IDLE kill (no progress for the idle window), not a flat
    # wall-clock/ceiling kill — small idle/ceiling getters + a recent started_at keep
    # terminal_reason == "idle_timeout" so the evolution retry path is exercised.
    monkeypatch.setattr(q, "get_task_idle_timeout_sec", lambda: 1)
    monkeypatch.setattr(q, "get_per_call_timeout_ceiling_sec", lambda: 1)
    monkeypatch.setattr(q, "_ensure_reaper_started", lambda: None)
    monkeypatch.setattr(q, "_reap_queue", q._stdqueue.Queue())
    # 3 llm_usage rounds totalling $1.50 are already durable before the kill.
    _write_events(tmp_path, [
        {"type": "llm_usage", "task_id": "evo1", "cost": 0.5, "prompt_tokens": 10, "completion_tokens": 2}
        for _ in range(3)
    ])

    task = {"id": "evo1", "type": "evolution", "chat_id": 7, "_attempt": 1, "metadata": {}}
    monkeypatch.setattr(q, "RUNNING", {
        "evo1": {"task": task, "started_at": time.time() - 1000, "worker_id": 0, "attempt": 1},
    })

    class _FakeProc:
        pid = 0
        def is_alive(self):
            return False
        def join(self, timeout=None):
            return None

    fake_worker = SimpleNamespace(busy_task_id="evo1", proc=_FakeProc(), wid=0)
    monkeypatch.setattr(workers_mod, "WORKERS", {0: fake_worker})
    monkeypatch.setattr(workers_mod, "respawn_worker", lambda wid: None)

    emitted = []
    monkeypatch.setattr(workers_mod, "get_event_q", lambda: SimpleNamespace(put=lambda evt: emitted.append(evt)))
    monkeypatch.setattr(services_mod, "archive_task_service_logs", lambda *a, **k: None)

    enqueued = []
    monkeypatch.setattr(q, "enqueue_task", lambda t, front=False: enqueued.append(t))
    monkeypatch.setattr(q, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(q, "send_with_budget", lambda *a, **k: None)
    monkeypatch.setattr(q, "load_state", lambda: {"evolution_mode_enabled": evolution_enabled, "owner_chat_id": 0})

    q.enforce_task_timeouts()
    # Variant A: terminal write + retry happen in the off-loop reaper; drain it here.
    while not q._reap_queue.empty():
        q._reap_timed_out_task(q._reap_queue.get_nowait())

    from ouroboros.task_results import load_task_result
    written = load_task_result(tmp_path, "evo1") or {}
    return enqueued, emitted, written


def test_hard_timeout_cleans_owner_mailbox(tmp_path, monkeypatch):
    """Block 5 (v6.29.0 scope advisory): a hard-killed worker never reaches the
    loop's mailbox cleanup, so the kill path must remove the task mailbox —
    otherwise finalize_now files accumulate and a stale control would instantly
    force-finalize a subagent retry reusing the same task id."""
    from ouroboros.owner_mailbox import KIND_FINALIZE_NOW, _mailbox_path, write_owner_message

    write_owner_message(tmp_path, "hard_timeout", "evo1", kind=KIND_FINALIZE_NOW)
    assert _mailbox_path(tmp_path, "evo1").exists()

    _drive_hard_timeout(tmp_path, monkeypatch, evolution_enabled=False)

    assert not _mailbox_path(tmp_path, "evo1").exists()


def test_hard_timeout_evolution_stopped_no_requeue_records_cost(tmp_path, monkeypatch):
    enqueued, emitted, written = _drive_hard_timeout(tmp_path, monkeypatch, evolution_enabled=False)

    # Campaign stopped: the killed evolution task must NOT be re-enqueued.
    assert enqueued == []
    # Terminal status records reconstructed cost/rounds, not zeros.
    assert written.get("status") == "failed"
    assert round(float(written.get("cost_usd") or 0), 6) == 1.5
    assert int(written.get("total_rounds") or 0) == 3
    # The terminal task_done carries the reconstructed cost for the campaign tally.
    done = [e for e in emitted if e.get("type") == "task_done"]
    assert done and round(float(done[0].get("cost_usd") or 0), 6) == 1.5
    assert int(done[0].get("total_rounds") or 0) == 3


def test_handle_task_done_falls_back_to_persisted_cost_on_zeroed_event(tmp_path):
    """A terminal task_done that omits cost (e.g. the _emit_task_done_terminal
    replay from kill_workers / already-done) must not zero the campaign tally:
    _handle_task_done falls back to the reconstructed cost already persisted in
    the task result."""
    from supervisor import queue
    from supervisor import state as supervisor_state
    from supervisor.events import _handle_task_done
    from ouroboros.task_results import STATUS_CANCELLED, write_task_result

    supervisor_state.init(tmp_path)
    queue.init(tmp_path, 600, 1800)
    queue.start_evolution_campaign("Improve", source="test")

    # The abnormal-termination path already reconstructed and persisted the cost.
    write_task_result(
        tmp_path, "evo-zero", STATUS_CANCELLED,
        cost_usd=2.5, total_rounds=4, prompt_tokens=100, completion_tokens=50,
    )

    broadcast = []
    ctx = SimpleNamespace(
        RUNNING={}, WORKERS={}, DRIVE_ROOT=tmp_path, REPO_DIR=tmp_path,
        load_state=supervisor_state.load_state, save_state=supervisor_state.save_state,
        append_jsonl=supervisor_state.append_jsonl,
        persist_queue_snapshot=lambda reason="": None,
        bridge=SimpleNamespace(push_log=lambda event: broadcast.append(event)),
    )

    _handle_task_done(
        {
            "type": "task_done", "task_id": "evo-zero", "task_type": "evolution",
            "result_status": "cancelled", "cost_usd": 0, "total_rounds": 0,
        },
        ctx,
    )

    # The broadcast task_done carries the reconstructed cost, not the zeroed event value.
    done = [e for e in broadcast if e.get("type") == "task_done"]
    assert done and float(done[0].get("cost_usd") or 0) == 2.5
    assert int(done[0].get("total_rounds") or 0) == 4
    # Reconstructed cost/rounds are preserved, but a cancelled task is still an
    # axis-level failure. Cost no longer proves evolution success.
    assert int(supervisor_state.load_state().get("evolution_consecutive_failures") or 0) == 1


def test_hard_timeout_evolution_enabled_requeues(tmp_path, monkeypatch):
    enqueued, emitted, written = _drive_hard_timeout(tmp_path, monkeypatch, evolution_enabled=True)

    # Campaign still enabled: the timed-out task is re-enqueued (one retry).
    assert len(enqueued) == 1
    assert enqueued[0].get("type") == "evolution"
    assert enqueued[0].get("_attempt") == 2
    # A retry keeps the live card active, so no terminal task_done is emitted.
    assert [e for e in emitted if e.get("type") == "task_done"] == []
    # The interrupted rollup still records reconstructed cost (not zeros).
    assert round(float(written.get("cost_usd") or 0), 6) == 1.5
