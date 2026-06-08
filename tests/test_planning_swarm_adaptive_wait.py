"""WS-T: progress-aware planning-swarm wait."""
from __future__ import annotations

import json
import time
import types

import ouroboros.tools.plan_review as pr


def _ctx(tmp_path):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return types.SimpleNamespace(budget_drive_root=str(tmp_path), drive_root=tmp_path)


def _write_snapshot(tmp_path, running_rows):
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "queue_snapshot.json").write_text(
        json.dumps({"running": running_rows}), encoding="utf-8")


def test_progress_classifier_states(tmp_path):
    tasks_nonterminal = {"s1": {"status": "running"}}
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 3.0}])
    assert pr._planning_swarm_progress(tmp_path, ["s1"], tasks_nonterminal) == "progressing"
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 999.0}])
    assert pr._planning_swarm_progress(tmp_path, ["s1"], tasks_nonterminal) == "stalled"
    _write_snapshot(tmp_path, [])  # not running anywhere
    assert pr._planning_swarm_progress(tmp_path, ["s1"], tasks_nonterminal) == "saturated"
    assert pr._planning_swarm_progress(tmp_path, ["s1"], {"s1": {"status": "completed"}}) == "progressing"


def test_progress_classifier_tolerates_malformed_heartbeat(tmp_path):
    """A malformed heartbeat_lag_sec in a corrupt snapshot must not raise; it is
    treated as not-fresh so the classifier degrades to a structured 'stalled'."""
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": "not-a-number"}])
    assert pr._planning_swarm_progress(tmp_path, ["s1"], {"s1": {"status": "running"}}) == "stalled"


def test_collect_extends_then_stalls(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 999.0}])  # stale -> stalled
    calls = {"n": 0}

    def fake_wait(root, ids, **kw):
        calls["n"] += 1
        return {"tasks": {"s1": {"status": "running"}}, "all_terminal": False}

    monkeypatch.setattr(pr, "wait_for_effective_tasks", fake_wait)
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=1.0)
    assert out["wait_stop_reason"] == "stalled"
    assert calls["n"] >= 1


def test_collect_returns_on_completed(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    monkeypatch.setattr(pr, "wait_for_effective_tasks", lambda r, i, **k: {
        "tasks": {"s1": {"status": "completed", "result": "ok"}}, "all_terminal": True})
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=1.0)
    assert out["wait_stop_reason"] == ""


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


def test_ceiling_honors_max_wait_below_slice(monkeypatch, tmp_path):
    """When max_wait is intentionally below the poll slice, the ceiling is max_wait
    (lower values apply as-is), so the first poll is capped to max_wait, not the slice."""
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [{"id": "s1", "heartbeat_lag_sec": 999.0}])  # stale -> stalled, 1 poll
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


def test_failed_closed_error_surfaces_stop_reason(monkeypatch, tmp_path):
    """The user-facing fail-closed error must include the precise wait_stop_reason."""
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
    monkeypatch.setattr(pr, "_load_resumable_planning_handoffs", lambda c, fp: None)
    monkeypatch.setattr(pr, "_collect_planning_handoffs", lambda *a, **k: {
        "wait": {"tasks": {"scout-1": {"status": "running"}}},
        "wait_stop_reason": "stalled",
        "wait_elapsed_sec": 12.3,
        "artifact": {"path": "x"},
    })
    out = pr._start_planning_swarm(
        ctx, plan="p", goal="g", files_to_touch=[],
        context_level="minimal", context_notes="")
    assert out["started"] is False
    assert "stalled" in out["error"]


def test_queue_snapshot_emits_heartbeat_lag_consumed_by_classifier(monkeypatch, tmp_path):
    """Cross-module contract: supervisor persist_queue_snapshot emits heartbeat_lag_sec
    on running rows, which _planning_swarm_progress consumes. Guards against a silent
    staleness break if the snapshot writer ever drops the field."""
    import time as _t
    import supervisor.queue as q

    snap_path = tmp_path / "state" / "queue_snapshot.json"
    snap_path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(q, "QUEUE_SNAPSHOT_PATH", snap_path, raising=False)
    monkeypatch.setattr(q, "PENDING", [], raising=False)
    monkeypatch.setattr(q, "RUNNING", {
        "s1": {"task": {"type": "task"}, "started_at": _t.time() - 5, "last_heartbeat_at": _t.time()},
    }, raising=False)

    q.persist_queue_snapshot(reason="contract-test")
    snap = json.loads(snap_path.read_text(encoding="utf-8"))
    assert snap["running"] and snap["running"][0]["id"] == "s1"
    assert "heartbeat_lag_sec" in snap["running"][0]  # contract the classifier depends on
    # End-to-end: the classifier reads the same snapshot and sees a fresh scout.
    assert pr._planning_swarm_progress(tmp_path, ["s1"], {"s1": {"status": "running"}}) == "progressing"


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


def test_collect_saturated_when_not_running(monkeypatch, tmp_path):
    ctx = _ctx(tmp_path)
    _write_snapshot(tmp_path, [])  # scout not in running -> saturated
    monkeypatch.setattr(pr, "wait_for_effective_tasks", lambda r, i, **k: {
        "tasks": {"s1": {"status": "running"}}, "all_terminal": False})
    monkeypatch.setattr(pr, "_persist_planning_handoffs", lambda c, h: {"path": ""})
    out = pr._collect_planning_handoffs(
        ctx, task_ids=["s1"], schedule_outputs=[],
        fingerprint="fp", wait_timeout=0.25, max_wait=1.0)
    assert out["wait_stop_reason"] == "saturated"
