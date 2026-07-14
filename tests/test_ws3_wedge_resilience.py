"""WS3 — chat-lane wedge resilience (v6.34.0).

A dedicated watchdog thread (outside the supervisor loop) surfaces TWO silent-wedge
classes as observable owner alerts instead of silent hours: a supervisor loop stall
(new-message intake starvation) and a heartbeat-silent in-process direct-chat turn.
New-message intake is reordered EARLY in the loop so a blocking step can't starve it.
The watchdog cannot kill a hung thread or free the chat-agent lock (a wedged turn
holds it for its whole duration; out-of-process kill was deferred per owner), so it
detects + reports + recommends /restart rather than force-recovering in-process; WS10
ephemeral decision turns keep the chat responsive meanwhile.
"""

from __future__ import annotations

import threading
import time


def test_supervisor_loop_stalled_detection():
    import server

    now = 1000.0
    assert server._supervisor_loop_stalled(now - 100, now, 90) is True   # past deadline
    assert server._supervisor_loop_stalled(now - 30, now, 90) is False   # healthy tick
    assert server._supervisor_loop_stalled(now - 100, now, 0) is False   # 0 = disabled


def test_supervisor_liveness_deadline_getter(monkeypatch):
    from ouroboros.config import (
        SUPERVISOR_LIVENESS_DEADLINE_DEFAULT_SEC,
        get_supervisor_liveness_deadline_sec,
    )

    monkeypatch.delenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", raising=False)
    assert get_supervisor_liveness_deadline_sec() == SUPERVISOR_LIVENESS_DEADLINE_DEFAULT_SEC
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "30")
    assert get_supervisor_liveness_deadline_sec() == 30
    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "0")
    assert get_supervisor_liveness_deadline_sec() == 0  # disabled


def test_watchdog_noop_when_disabled(monkeypatch):
    import server

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "0")
    before = threading.active_count()
    server._start_supervisor_liveness_watchdog([time.time()])
    assert threading.active_count() == before  # no watchdog thread spawned


def test_watchdog_alerts_owner_once_on_stall(monkeypatch):
    import server

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "1")
    alerts = []

    class _Bridge:
        def send_message(self, chat_id, text, *a, **k):
            alerts.append((chat_id, text, k))
            return (True, "")

    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: _Bridge())
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 5})
    monkeypatch.setattr("supervisor.state.append_jsonl", lambda *a, **k: None)
    stop = threading.Event()  # local per-test token; do NOT touch the global restart flag
    try:
        server._start_supervisor_liveness_watchdog([time.time() - 100], stop)  # already stale
        end = time.time() + 6
        while not alerts and time.time() < end:
            time.sleep(0.1)
    finally:
        stop.set()  # stop THIS watchdog thread (no cross-test leakage)
    assert len(alerts) == 1
    assert alerts[0][0] == 5 and "stalled" in alerts[0][1]
    assert alerts[0][2]["is_progress"] is True
    assert alerts[0][2]["progress_meta"]["task_incident"] == "supervisor_loop_stall"
    assert alerts[0][2]["progress_meta"]["toast_once"].startswith("supervisor-loop-stall:")


def test_chat_turn_wedged_detection():
    import server

    now = 1000.0
    assert server._chat_turn_wedged(True, now - 100, now, 90) is True    # busy + silent past deadline
    assert server._chat_turn_wedged(True, now - 30, now, 90) is False    # busy + recent heartbeat
    assert server._chat_turn_wedged(False, now - 100, now, 90) is False  # not busy
    assert server._chat_turn_wedged(True, None, now, 90) is False        # liveness loop not started yet
    assert server._chat_turn_wedged(True, now - 100, now, 0) is False    # 0 = disabled


def test_chat_turn_liveness_reads_agent_without_taking_the_lock(monkeypatch):
    import types

    import supervisor.workers as w

    monkeypatch.setattr(w, "_chat_agent", None)
    assert w.chat_turn_liveness() == (False, None, None)

    monkeypatch.setattr(w, "_chat_agent", types.SimpleNamespace(
        _busy=True, _current_task_id="t1", _last_activity_ts=1234.0))
    # Hold _chat_agent_lock to prove the liveness read never blocks on it (a wedged
    # turn holds the lock for its whole duration — the watchdog must not deadlock).
    assert w._chat_agent_lock.acquire(blocking=False)
    try:
        assert w.chat_turn_liveness() == (True, "t1", 1234.0)
    finally:
        w._chat_agent_lock.release()


def test_watchdog_alerts_on_chat_turn_wedge(monkeypatch):
    import types

    import server
    import supervisor.workers as w

    monkeypatch.setenv("OUROBOROS_SUPERVISOR_LIVENESS_DEADLINE_SEC", "1")
    alerts = []

    class _Bridge:
        def send_message(self, chat_id, text, *a, **k):
            alerts.append((chat_id, text, k))
            return (True, "")

    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: _Bridge())
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"owner_chat_id": 7})
    monkeypatch.setattr("supervisor.state.append_jsonl", lambda *a, **k: None)
    monkeypatch.setattr(w, "_chat_agent", types.SimpleNamespace(
        _busy=True, _current_task_id="wedged1", _last_activity_ts=time.time() - 100))
    stop = threading.Event()  # local per-test token
    try:
        server._start_supervisor_liveness_watchdog([time.time()], stop)
        end = time.time() + 6
        while not any("wedged" in a[1] for a in alerts) and time.time() < end:
            time.sleep(0.1)
    finally:
        stop.set()
    assert any("wedged" in a[1] for a in alerts)  # the chat-turn wedge was surfaced
    assert any(a[0] == 7 for a in alerts)
    wedge = next(a for a in alerts if "wedged" in a[1])
    assert wedge[2]["is_progress"] is True
    assert wedge[2]["task_id"] == "wedged1"
    assert wedge[2]["progress_meta"] == {
        "task_incident": "chat_turn_wedge",
        "toast_once": "wedged1:chat_turn_wedge",
    }
