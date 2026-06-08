"""Regression tests for chat-history separate quotas (PR-D2, issue #8).

A progress/telemetry burst must never evict the user's real conversation. Subagent
lineage is kept on top of the progress quota so a flood can't drop a RECENT child's
lifecycle events — but only WITHIN the recent telemetry window, so a long-finished
swarm does not re-materialise as a stuck "Working" parent card on reload.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from ouroboros.gateway.history import make_chat_history_endpoint


def _run(tmp_path, params):
    endpoint = make_chat_history_endpoint(tmp_path)
    resp = asyncio.run(endpoint(SimpleNamespace(query_params=params)))
    return json.loads(resp.body.decode("utf-8"))["messages"]


def _lineage_row(ts, child, ev):
    return json.dumps({
        "ts": ts, "content": f"child {ev}", "task_id": child,
        "delegation_role": "subagent", "parent_task_id": "root", "subagent_event": ev,
    })


def test_progress_flood_does_not_evict_human_messages(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    chat_lines = [
        json.dumps({"ts": f"2026-06-05T00:00:0{i}Z",
                    "direction": "in" if i % 2 == 0 else "out", "text": f"human-{i}"})
        for i in range(5)
    ]
    (logs / "chat.jsonl").write_text("\n".join(chat_lines) + "\n", encoding="utf-8")
    prog_lines = [
        json.dumps({"ts": f"2026-06-05T01:00:{i:02d}Z", "content": f"telemetry-{i}", "task_id": "t1"})
        for i in range(50)
    ]
    (logs / "progress.jsonl").write_text("\n".join(prog_lines) + "\n", encoding="utf-8")

    msgs = _run(tmp_path, {"n_human": "3", "n_progress": "2"})
    human = [m["text"] for m in msgs if not m.get("is_progress")]
    progress = [m for m in msgs if m.get("is_progress")]
    assert human == ["human-2", "human-3", "human-4"]   # not evicted by the flood
    assert len(progress) == 2                            # telemetry bounded by n_progress


def test_recent_lineage_survives_progress_flood(tmp_path):
    """A RECENT child's lifecycle events survive even a tiny progress quota."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = [
        json.dumps({"ts": f"2026-06-05T00:00:{i:02d}Z", "content": f"noise-{i}", "task_id": "root"})
        for i in range(50)
    ]
    # 3 subagent lifecycle events INSIDE the recent window (near the latest noise)
    for i, ev in zip((47, 48, 49), ("scheduled", "update", "completed")):
        lines.append(_lineage_row(f"2026-06-05T00:00:{i:02d}Z", "child1", ev))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    msgs = _run(tmp_path, {"n_progress": "5"})
    lineage = [m for m in msgs if m.get("delegation_role") == "subagent"]
    assert len(lineage) == 3
    assert {m.get("subagent_event") for m in lineage} == {"scheduled", "update", "completed"}


def test_old_lineage_does_not_resurrect_finished_swarm(tmp_path):
    """Lineage OLDER than the retained telemetry window is dropped, so a
    long-finished swarm does not re-materialise as a stuck parent card."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = []
    # OLD swarm lineage (4 days earlier)
    for i, ev in zip((1, 2, 3), ("scheduled", "running", "completed")):
        lines.append(_lineage_row(f"2026-06-01T00:00:0{i}Z", "oldchild", ev))
    # RECENT telemetry flood
    for i in range(50):
        lines.append(json.dumps({"ts": f"2026-06-05T02:00:{i:02d}Z", "content": f"noise-{i}", "task_id": "root"}))
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    msgs = _run(tmp_path, {"n_progress": "5"})
    lineage = [m for m in msgs if m.get("delegation_role") == "subagent"]
    assert lineage == []  # old swarm does NOT resurface


def test_delegation_role_root_respects_progress_quota(tmp_path):
    """delegation_role='root' is NOT subagent lineage and must obey the quota."""
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    lines = [
        json.dumps({"ts": f"2026-06-05T00:00:{i:02d}Z", "content": f"root-{i}",
                    "task_id": f"root-{i}", "delegation_role": "root"})
        for i in range(50)
    ]
    (logs / "progress.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    progress = [m for m in _run(tmp_path, {"n_progress": "5"}) if m.get("is_progress")]
    assert len(progress) == 5  # 'root' did not bypass the quota


def test_default_quotas_keep_recent_history(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T00:00:00Z", "direction": "in", "text": "hello"}) + "\n",
        encoding="utf-8",
    )
    (logs / "progress.jsonl").write_text("", encoding="utf-8")
    msgs = _run(tmp_path, {})
    assert any(m.get("text") == "hello" and not m.get("is_progress") for m in msgs)


def test_history_annotates_terminal_from_effective_status(tmp_path, monkeypatch):
    """A SIGKILLed/panic'd task whose raw result is stuck "running" but whose
    EFFECTIVE status is failed (stale-orphan guard) must get task_terminal_status,
    so its card finalizes instead of replaying "Working" forever."""
    import ouroboros.task_status as ts_mod
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "chat.jsonl").write_text("", encoding="utf-8")
    (logs / "progress.jsonl").write_text(
        json.dumps({"ts": "2026-06-05T00:00:00Z", "content": "step", "task_id": "stuck1"}) + "\n",
        encoding="utf-8",
    )
    # Simulate the orphan guard: effective status resolves to "failed" even though
    # the raw on-disk file is still "running".
    monkeypatch.setattr(
        ts_mod, "load_effective_task_result",
        lambda dr, tid: {"status": "failed"} if tid == "stuck1" else {},
    )
    msgs = _run(tmp_path, {})
    row = next(m for m in msgs if m.get("task_id") == "stuck1")
    assert row.get("task_terminal_status") == "failed"
