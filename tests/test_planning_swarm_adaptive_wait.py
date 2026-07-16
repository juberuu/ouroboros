"""Planning scouts share one terminal-or-cutoff wait boundary."""
from __future__ import annotations

import json
import time
import types
from datetime import datetime, timedelta, timezone

import ouroboros.tools.plan_review as pr


def _ctx(tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(
        budget_drive_root=str(tmp_path), drive_root=tmp_path, task_id="parent"
    )


def _write_snapshot(tmp_path, running_rows):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "queue_snapshot.json").write_text(
        json.dumps({"running": running_rows}), encoding="utf-8")


def test_collect_does_not_drop_scout_on_stale_heartbeat(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 999.0}])
    calls = {"n": 0}

    def fake_wait(root, ids, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"tasks": {"s1": {"status": "running"}}, "all_terminal": False}
        return {"tasks": {"s1": {"status": "completed", "result": "late but valid"}}, "all_terminal": True}

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=1.0)
    assert out["wait_stop_reason"] == ""
    assert out["included_task_ids"] == ["s1"]
    assert calls["n"] == 2


def test_collect_returns_on_completed(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(pr, "wait_for_effective_tasks", lambda r, i, **k: {
        "tasks": {"s1": {"status": "completed", "result": "ok"}}, "all_terminal": True})
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=1.0)
    assert out["wait_stop_reason"] == ""


def test_collect_waits_for_every_healthy_scout_not_first_result(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s2", "heartbeat_lag_sec": 1.0}])
    waits = [
        {
            "tasks": {
                "s1": {"status": "completed", "result": "first"},
                "s2": {"status": "running", "result": ""},
            },
            "all_terminal": False,
        },
        {
            "tasks": {
                "s1": {"status": "completed", "result": "first"},
                "s2": {"status": "completed", "result": "second"},
            },
            "all_terminal": True,
        },
    ]
    calls = {"count": 0}

    def fake_wait(*_args, **_kwargs):
        calls["count"] += 1
        return waits.pop(0)

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx,
        task_ids=["s1", "s2"],
        schedule_outputs=[],
        fingerprint="fp",
        wait_timeout=0.25,
        max_wait=1.0,
    )
    assert calls["count"] == 2
    assert out["included_task_ids"] == ["s1", "s2"]
    assert out["omissions"] == []


def test_collect_records_explicit_omission_at_shared_cutoff(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s2", "heartbeat_lag_sec": 999.0}])
    def fake_wait(*_a, **kwargs):
        time.sleep(float(kwargs.get("timeout_sec") or 0))
        return {
            "tasks": {
                "s1": {"status": "completed", "result": "usable"},
                "s2": {"status": "running", "result": ""},
            },
            "all_terminal": False,
        }

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx,
        task_ids=["s1", "s2"],
        schedule_outputs=[],
        fingerprint="fp",
        wait_timeout=0.25,
        max_wait=0.3,
    )
    assert out["included_task_ids"] == ["s1"]
    assert out["omissions"] == [{
        "task_id": "s2",
        "role": "",
        "status": "running",
        "reason": "not_terminal_at_review_cutoff:ceiling",
    }]


def test_terminal_omission_has_bounded_redacted_detail():
    leaked = "abcdefghijklmnop-secret-value"
    included, omissions = pr._planning_handoff_selection(
        [
            {
                "role": "planning-scout-1",
                "schedule_status": "started",
                "task_ids": ["s1"],
                "schedule_reason": "scheduled",
            },
            {
                "role": "planning-scout-2",
                "schedule_status": "started",
                "task_ids": ["s2"],
                "schedule_reason": "scheduled",
            },
        ],
        {
            "s1": {"status": "completed", "result": "usable"},
            "s2": {
                "status": "failed",
                "error": f"api_key={leaked} " + ("diagnostic " * 100),
            },
        },
        "",
    )
    assert included == ["s1"]
    assert len(omissions) == 1
    assert omissions[0]["role"] == "planning-scout-2"
    assert omissions[0]["reason"] == "terminal_without_usable_handoff:failed"
    assert len(omissions[0]["detail"]) <= 600
    assert "⚠️ OMISSION NOTE:" in omissions[0]["detail"]
    assert "original length" in omissions[0]["detail"]
    assert "***REDACTED***" in omissions[0]["detail"]
    assert leaked not in omissions[0]["detail"]


def test_bounded_planning_reason_keeps_disclosure_across_reprojection():
    original = "diagnostic " * 200
    bounded = pr._bounded_planning_reason(original, limit=600)

    assert len(bounded) <= 600
    assert "⚠️ OMISSION NOTE:" in bounded
    assert f"original length {len(original.strip())}" in bounded
    assert pr._bounded_planning_reason(bounded, limit=600) == bounded


def test_consumed_marker_contains_only_included_handoffs(monkeypatch, tmp_path):
    from ouroboros.task_results import (
        record_plan_review_collection,
        record_plan_review_scout,
        reserve_plan_review_wave,
        write_task_result,
    )

    ctx = _ctx(tmp_path)
    reserve_plan_review_wave(
        tmp_path,
        "parent",
        fingerprint="f" * 64,
        plan_text_hash="a" * 64,
        scout_roles=["planning-scout-1", "planning-scout-2"],
        cutoff_at="2099-01-01T00:00:00+00:00",
    )
    for role, task_id in (("planning-scout-1", "s1"), ("planning-scout-2", "s2")):
        record_plan_review_scout(
            tmp_path,
            "parent",
            fingerprint="f" * 64,
            role=role,
            schedule_status="started",
            task_ids=[task_id],
            reason=f"scheduled {task_id}",
        )
    record_plan_review_collection(
        tmp_path,
        "parent",
        fingerprint="f" * 64,
        included_task_ids=["s1"],
        omissions=[{
            "task_id": "s2", "role": "planning-scout-2", "status": "running"
        }],
        stop_reason="ceiling",
    )
    reviewed_snapshot = {
        "status": "completed",
        "role": "planning-scout-1",
        "result": "summary: reviewed",
    }
    write_task_result(
        tmp_path,
        "s1",
        "completed",
        parent_task_id="parent",
        root_task_id="parent",
        delegation_role="subagent",
        role="planning-scout-1",
        result=reviewed_snapshot["result"],
    )
    handoffs = {
        "request_fingerprint": "f" * 64,
        "included_task_ids": ["s1"],
        "wait": {"tasks": {"s1": reviewed_snapshot}},
        "omissions": [{
            "task_id": "s2", "role": "planning-scout-2", "status": "running"
        }],
    }
    snapshots = []
    monkeypatch.setattr(
        pr,
        "_persist_planning_handoffs",
        lambda _ctx, payload: snapshots.append(dict(payload)) or {"path": "x"},
    )
    pr._mark_planning_handoffs_consumed(ctx, handoffs)
    assert handoffs["consumed_task_ids"] == ["s1"]
    assert "s2" not in handoffs["consumed_task_ids"]
    assert snapshots[-1]["consumed_task_ids"] == ["s1"]
    from ouroboros.task_status import load_effective_task_result
    from ouroboros.tools.join_ledger import _current_child_result_disposition

    assert _current_child_result_disposition(
        load_effective_task_result(tmp_path, "s1")
    ) == "integrated"


def test_stale_reviewed_scout_snapshot_is_consumed_with_audit_warning(monkeypatch, tmp_path):
    from ouroboros.task_results import (
        load_plan_review_state,
        plan_review_wave,
        record_plan_review_collection,
        record_plan_review_scout,
        reserve_plan_review_wave,
        write_task_result,
    )

    ctx = _ctx(tmp_path)
    fingerprint = "f" * 64
    reserve_plan_review_wave(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        plan_text_hash="a" * 64,
        scout_roles=["planning-scout-1"],
        cutoff_at="2099-01-01T00:00:00+00:00",
    )
    record_plan_review_scout(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        role="planning-scout-1",
        schedule_status="started",
        task_ids=["s1"],
        reason="scheduled s1",
    )
    record_plan_review_collection(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        included_task_ids=["s1"],
        omissions=[],
        stop_reason="",
    )
    reviewed_snapshot = {
        "status": "completed",
        "role": "planning-scout-1",
        "result": "version one",
    }
    write_task_result(
        tmp_path,
        "s1",
        "completed",
        parent_task_id="parent",
        root_task_id="parent",
        delegation_role="subagent",
        role="planning-scout-1",
        result="version two",
    )
    handoffs = {
        "request_fingerprint": fingerprint,
        "included_task_ids": ["s1"],
        "omissions": [],
        "wait": {"tasks": {"s1": reviewed_snapshot}},
    }
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda *_a, **_k: {"path": "x"})

    pr._mark_planning_handoffs_consumed(ctx, handoffs)

    wave = plan_review_wave(load_plan_review_state(tmp_path, "parent"), fingerprint)
    assert wave["consumed_task_ids"] == ["s1"]
    assert wave["disposition_warnings"][0]["code"] == "CHILD_RESULT_STALE"
    assert handoffs["disposition_warnings"] == wave["disposition_warnings"]
    current = __import__(
        "ouroboros.task_status", fromlist=["load_effective_task_result"]
    ).load_effective_task_result(tmp_path, "s1")
    assert "child_result_disposition" not in current


def test_late_omitted_handoff_is_audit_only(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    handoffs = {
        "included_task_ids": ["s1"],
        "consumed_task_ids": ["s1"],
        "omissions": [{"task_id": "s2", "status": "running"}],
        "review": {"aggregate_signal": "GREEN", "closed": True},
    }
    monkeypatch.setattr(pr, "wait_for_effective_tasks", lambda *_a, **_k: {
        "tasks": {"s2": {"status": "completed", "result": "late result"}},
        "all_terminal": True,
    })
    snapshots = []
    monkeypatch.setattr(
        pr,
        "_persist_planning_handoffs",
        lambda _ctx, payload: snapshots.append(dict(payload)) or {"path": "x"},
    )
    pr._capture_late_planning_audit(ctx, handoffs)
    assert handoffs["late_audit"]["affects_review"] is False
    assert handoffs["late_audit"]["tasks"]["s2"]["result"] == "late result"
    assert handoffs["consumed_task_ids"] == ["s1"]
    assert handoffs["review"]["aggregate_signal"] == "GREEN"


def test_reviewed_omitted_scout_does_not_reopen_generic_child_gate(tmp_path):
    from types import SimpleNamespace

    from ouroboros.loop import _compute_subagent_handoff, _direct_child_results
    from ouroboros.task_results import (
        record_plan_review_collection,
        record_plan_review_consumed,
        record_plan_review_result,
        record_plan_review_scout,
        reserve_plan_review_wave,
        write_task_result,
    )

    fingerprint = "f" * 64
    reserve_plan_review_wave(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        plan_text_hash="a" * 64,
        scout_roles=["planning-scout-1"],
        cutoff_at="2099-01-01T00:00:00+00:00",
    )
    record_plan_review_scout(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        role="planning-scout-1",
        schedule_status="started",
        task_ids=["s-late"],
        reason="scheduled",
    )
    record_plan_review_collection(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        included_task_ids=[],
        omissions=[{
            "task_id": "s-late",
            "role": "planning-scout-1",
            "status": "running",
            "reason": "not_terminal_at_review_cutoff:ceiling",
        }],
        stop_reason="ceiling",
    )
    record_plan_review_consumed(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        consumed_task_ids=[],
    )
    record_plan_review_result(
        tmp_path,
        "parent",
        fingerprint=fingerprint,
        review={
            "request_fingerprint": fingerprint,
            "plan_text_hash": "a" * 64,
            "aggregate_signal": "GREEN",
            "closed": True,
            "findings": [],
        },
    )
    write_task_result(
        tmp_path,
        "s-late",
        "completed",
        parent_task_id="parent",
        root_task_id="parent",
        delegation_role="subagent",
        role="planning-scout-1",
        result="late result",
    )
    ctx = SimpleNamespace(
        status_drive_root=tmp_path,
        drive_root=tmp_path,
        drive_logs=tmp_path / "logs",
        task_id="parent",
        root_task_id="parent",
    )
    assert _direct_child_results(ctx) == []
    tools = SimpleNamespace(_ctx=SimpleNamespace(
        task_metadata={"budget_drive_root": str(tmp_path), "root_task_id": "parent"},
        budget_drive_root=str(tmp_path),
        drive_root=str(tmp_path),
        task_id="parent",
        _subagent_handoff_signature="",
    ))
    assert _compute_subagent_handoff(tools, tmp_path, "parent", "done") == ""


def test_collect_wait_does_not_overshoot_ceiling(monkeypatch, tmp_path):
    """Sum of requested slice waits must not overshoot the ceiling by a full slice."""
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 1.0}])  # fresh -> progressing
    seen = []

    def fake_wait(root, ids, timeout_sec=0.0, **kw):
        seen.append(float(timeout_sec))
        time.sleep(float(timeout_sec))
        return {"tasks": {"s1": {"status": "running"}}, "all_terminal": False}

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    # slice_sec floors at 0.25; max_wait=0.6 forces multiple slices with a shrunk last one.
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=0.6)
    assert out["wait_stop_reason"] == "ceiling"
    # Requested waits sum to <= ceiling (old after-the-slice check would reach ~0.75).
    assert sum(seen) <= 0.6 + 1e-3


def test_resumed_collection_reuses_one_absolute_cutoff(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    now = [datetime(2030, 1, 1, tzinfo=timezone.utc)]
    cutoff = now[0] + timedelta(seconds=1)
    calls = [[], []]
    phase = {"index": 0}

    def fake_wait(_root, _ids, timeout_sec=0.0, **_kwargs):
        timeout = float(timeout_sec)
        calls[phase["index"]].append(timeout)
        now[0] += timedelta(seconds=timeout)
        return {"tasks": {"s1": {"status": "running"}}, "all_terminal": False}

    monkeypatch.setattr(pr, "_planning_now", lambda: now[0])
    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda _ctx, _payload: {"path": ""})
    first = pr._collect_planning_handoffs(
        ctx,
        task_ids=["s1"],
        schedule_outputs=[],
        fingerprint="fp",
        wait_timeout=0.25,
        max_wait=99.0,
        cutoff_at=cutoff.isoformat(),
    )
    phase["index"] = 1
    second = pr._collect_planning_handoffs(
        ctx,
        task_ids=["s1"],
        schedule_outputs=[],
        fingerprint="fp",
        wait_timeout=0.25,
        max_wait=99.0,
        cutoff_at=cutoff.isoformat(),
    )

    assert first["wait_stop_reason"] == "ceiling"
    assert second["wait_stop_reason"] == "ceiling"
    assert sum(timeout for group in calls for timeout in group if timeout > 0) <= 1.0
    assert calls[1] == [0.0]
    assert second["wait_remaining_at_start_sec"] == 0.0


def test_ceiling_honors_max_wait_below_slice(monkeypatch, tmp_path):
    """When max_wait is intentionally below the poll slice, the ceiling is max_wait
    (lower values apply as-is), so the first poll is capped to max_wait, not the slice."""
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 999.0}])  # diagnostic only
    seen = []

    def fake_wait(root, ids, timeout_sec=0.0, **kw):
        seen.append(float(timeout_sec))
        return {"tasks": {"s1": {"status": "running"}}, "all_terminal": False}

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=120.0, max_wait=10.0)
    assert seen and seen[0] <= 10.0 + 1e-9


def test_cutoff_omission_surfaces_precise_stop_reason(monkeypatch, tmp_path):
    import queue as _queue

    import ouroboros.tools.control as control
    from ouroboros.tools.registry import ToolContext

    monkeypatch.setenv("OUROBOROS_MAX_WORKERS", "3")
    monkeypatch.setenv("OUROBOROS_PLAN_TASK_SWARM_TIMEOUT_SEC", "0")
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    ctx.task_id = "parent1"
    ctx.task_depth = 0
    ctx.current_chat_id = 1
    ctx.event_queue = _queue.Queue()
    ctx.task_metadata = {"root_task_id": "parent1", "session_id": "s"}

    def fake_schedule(ctx_arg, **kwargs):
        records = list(getattr(ctx_arg, "_last_scheduled_subagents", []) or [])
        records.append({"task_ids": ["scout-1"]})
        ctx_arg._last_scheduled_subagents = records
        return "scheduled scout-1"

    monkeypatch.setattr(control, "_schedule_task", fake_schedule)
    monkeypatch.setattr(pr, "_collect_planning_handoffs", lambda *a, **k: {
        "wait": {"tasks": {"scout-1": {"status": "running"}}},
        "wait_stop_reason": "ceiling",
        "wait_elapsed_sec": 12.3,
        "included_task_ids": [],
        "omissions": [{
            "task_id": "scout-1",
            "role": "planning-scout-1",
            "status": "running",
            "reason": "not_terminal_at_review_cutoff:ceiling",
        }],
        "artifact": {"path": "x"},
    })
    out = pr._start_planning_swarm(
        ctx,
        pr._PlanReviewRequest(
            plan="p", goal="g", files_to_touch=[], context_level="minimal",
        ),
    )
    assert out["started"] is True
    assert out["degraded_evidence"] is True
    assert out["handoffs"]["omissions"][0]["reason"] == (
        "not_terminal_at_review_cutoff:ceiling"
    )


def test_plan_task_timeout_budget_invariant():
    """plan_task tool/wrapper budgets must honor the swarm max-wait ceiling and stay
    under the supervisor HARD task timeout (WS-T: a healthy long scout is not cut off
    before the adaptive ceiling)."""
    from ouroboros.config import SETTINGS_DEFAULTS, get_plan_task_swarm_max_wait_sec
    from supervisor.queue import HARD_TIMEOUT_SEC

    # The plan_review mirror of the default must match the config SSOT.
    assert pr._PLAN_SWARM_MAX_WAIT_DEFAULT_SEC == SETTINGS_DEFAULTS["OUROBOROS_PLAN_TASK_SWARM_MAX_WAIT_SEC"]
    max_wait = get_plan_task_swarm_max_wait_sec()
    # Wrapper covers swarm wait + a full reviewer slot (they run sequentially in one tool call).
    assert pr._PLAN_REVIEW_WRAPPER_TIMEOUT_SEC >= max_wait + pr._PLAN_REVIEW_SLOT_TIMEOUT_SEC
    # The tool future timeout must not fire before the asyncio wrapper.
    assert pr._PLAN_TASK_TOOL_TIMEOUT_SEC > pr._PLAN_REVIEW_WRAPPER_TIMEOUT_SEC
    # ...and the whole tool must finish before the supervisor hard-kills the task.
    assert pr._PLAN_TASK_TOOL_TIMEOUT_SEC < HARD_TIMEOUT_SEC


def test_effective_swarm_max_wait_clamps_to_supported(monkeypatch):
    """Raising the env above the budget-supported default is clamped (enforced),
    never silently exceeding the plan_task wrapper/tool budget. Lower values apply."""
    monkeypatch.setenv("OUROBOROS_PLAN_TASK_SWARM_MAX_WAIT_SEC", "5000")
    assert pr._effective_swarm_max_wait() == float(pr._PLAN_SWARM_MAX_WAIT_DEFAULT_SEC)
    monkeypatch.setenv("OUROBOROS_PLAN_TASK_SWARM_MAX_WAIT_SEC", "120")
    assert pr._effective_swarm_max_wait() == 120.0


def test_collect_not_running_still_gets_shared_cutoff(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [])

    def fake_wait(_root, _ids, **kwargs):
        time.sleep(float(kwargs.get("timeout_sec") or 0))
        return {"tasks": {"s1": {"status": "running"}}, "all_terminal": False}

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=0.3)
    assert out["wait_stop_reason"] == "ceiling"
