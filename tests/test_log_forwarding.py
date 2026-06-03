"""Coverage for the v6.12.0 dashboard log-delivery path: the worker-side log
sink (suppression contract), the main-side _handle_log_event consumer (broadcast
+ checkpoint-only persist), and the logs.js backfill/dedupe wiring."""

import inspect
import json
import pathlib
from types import SimpleNamespace

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_worker_log_sink_suppresses_live_sibling_types():
    from supervisor.workers import WORKER_LOG_SINK_SUPPRESSED_TYPES, worker_main

    # These five already arrive live via a dedicated EVENT_Q sibling/handler;
    # forwarding the worker's append_jsonl copy too would double-broadcast.
    assert WORKER_LOG_SINK_SUPPRESSED_TYPES == frozenset(
        {"tool_call", "llm_round", "task_checkpoint", "task_done", "llm_usage"}
    )
    src = inspect.getsource(worker_main)
    assert "set_log_sink" in src
    assert "emit_log_event" in src
    assert "WORKER_LOG_SINK_SUPPRESSED_TYPES" in src


def test_handle_log_event_broadcasts_all_but_persists_only_checkpoints(tmp_path):
    from supervisor import events as ev
    from supervisor import state as supervisor_state

    (tmp_path / "logs").mkdir()
    events_file = tmp_path / "logs" / "events.jsonl"
    broadcast = []
    ctx = SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        append_jsonl=supervisor_state.append_jsonl,
        bridge=SimpleNamespace(push_log=lambda e: broadcast.append(e)),
    )

    # A previously-missing worker log type is forwarded live, never re-persisted.
    ev._handle_log_event(
        {"type": "log_event", "data": {"type": "task_received", "task_id": "t1"}}, ctx
    )
    assert any(e.get("type") == "task_received" for e in broadcast)
    assert not events_file.exists()

    # task_checkpoint is broadcast AND persisted exactly once (the worker
    # suppresses its own copy, so this single main-side write is the only one).
    ev._handle_log_event(
        {"type": "log_event", "data": {"type": "task_checkpoint", "task_id": "t1", "round": 1}}, ctx
    )
    assert any(e.get("type") == "task_checkpoint" for e in broadcast)
    lines = [ln for ln in events_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["type"] == "task_checkpoint"


def test_logs_js_backfills_all_streams_and_dedupes_without_dropping_preconnect():
    src = (REPO / "web" / "modules" / "logs.js").read_text(encoding="utf-8")
    for stream in ("'events'", "'tools'", "'progress'", "'supervisor'"):
        assert stream in src, f"backfill must include the {stream} log stream"
    # Exact-duplicate guard collapses backfill/live overlap…
    assert "renderedLogKeys" in src
    # …and backfill reruns on reconnect…
    assert "ws.on('open'" in src
    # …without a load-time timestamp skip that could drop the pre-connect window.
    assert "loadStart" not in src
