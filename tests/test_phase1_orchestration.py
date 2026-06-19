"""Phase 1 golden coverage: honest runtime digest, activity-based timeout model,
no-blind-retry of orchestrators, and the task-tree coordination ledger."""

import time
from types import SimpleNamespace


def _patch_queue(queue_module, workers_module, monkeypatch, tmp_path, workers):
    monkeypatch.setattr(queue_module, "DRIVE_ROOT", tmp_path)
    monkeypatch.setattr(queue_module, "PENDING", [])
    monkeypatch.setattr(queue_module, "RUNNING", {})
    monkeypatch.setattr(queue_module, "FINALIZATION_GRACE_SEC", 0)
    monkeypatch.setattr(queue_module, "QUEUE_MAX_RETRIES", 1)
    monkeypatch.setattr(queue_module, "load_state", lambda: {})
    monkeypatch.setattr(queue_module, "append_jsonl", lambda *a, **k: None)
    monkeypatch.setattr(queue_module, "persist_queue_snapshot", lambda reason="": None)
    monkeypatch.setattr(queue_module, "_ensure_reaper_started", lambda: None)
    monkeypatch.setattr(queue_module, "_reap_queue", queue_module._stdqueue.Queue())
    monkeypatch.setattr(workers_module, "WORKERS", workers)
    monkeypatch.setattr(workers_module, "respawn_worker", lambda worker_id: None)


class _FakeProc:
    pid = 0

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def test_progressing_subtree_keeps_idle_parent_alive(tmp_path, monkeypatch):
    """The core fix: a parent that is idle ITSELF but whose subtree is progressing must
    NOT be killed (no flat wall-clock guillotine on a productively-waiting orchestrator)."""
    from supervisor import queue as q
    from supervisor import workers as w

    workers = {1: SimpleNamespace(busy_task_id="root1", proc=_FakeProc(), reaping=False),
               2: SimpleNamespace(busy_task_id="child1", proc=_FakeProc(), reaping=False)}
    _patch_queue(q, w, monkeypatch, tmp_path, workers)
    now = time.time()
    q.RUNNING["root1"] = {
        "task": {"id": "root1", "type": "task"},
        "started_at": now - 5000, "last_heartbeat_at": now - 5000,  # idle itself
        "worker_id": 1, "attempt": 1,
    }
    q.RUNNING["child1"] = {
        "task": {"id": "child1", "type": "task", "parent_task_id": "root1", "delegation_role": "subagent"},
        "started_at": now - 60, "last_progress_at": now - 2,  # fresh real progress
        "last_heartbeat_at": now - 2, "worker_id": 2, "attempt": 1,
    }

    q.enforce_task_timeouts()

    assert "root1" in q.RUNNING, "idle parent with a progressing subtree must survive"
    assert "child1" in q.RUNNING
    assert q.PENDING == []


def test_idle_parent_with_pending_descendant_survives(tmp_path, monkeypatch):
    """A parent that is idle ITSELF but has a QUEUED (PENDING, worker-saturated) descendant
    must NOT be killed — killing it would orphan the queued subtree before it ever ran."""
    from supervisor import queue as q
    from supervisor import workers as w

    workers = {1: SimpleNamespace(busy_task_id="root1", proc=_FakeProc(), reaping=False)}
    _patch_queue(q, w, monkeypatch, tmp_path, workers)
    monkeypatch.setattr(q, "get_task_idle_timeout_sec", lambda: 1)
    monkeypatch.setattr(q, "get_per_call_timeout_ceiling_sec", lambda: 1)
    monkeypatch.setattr(q, "PENDING", [{"id": "child1", "parent_task_id": "root1", "root_task_id": "root1"}])
    now = time.time()
    q.RUNNING["root1"] = {
        "task": {"id": "root1", "type": "task"},
        "started_at": now - 5000, "last_heartbeat_at": now - 5000,  # idle itself
        "worker_id": 1, "attempt": 1,
    }

    q.enforce_task_timeouts()

    assert "root1" in q.RUNNING, "idle parent with a queued descendant must survive"
    assert q.PENDING and q.PENDING[0]["id"] == "child1"


def test_explicit_deadline_is_hard_even_while_progressing(tmp_path, monkeypatch):
    """Owner decision (Option A): an explicit deadline_at is HARD — honored promptly even
    while the task is actively progressing. Only the removed BLANKET wall-clock was
    activity-gated; a deliberate/caller deadline is not."""
    from supervisor import queue as q
    from supervisor import workers as w
    from ouroboros.task_results import STATUS_FAILED, load_task_result

    workers = {3: SimpleNamespace(busy_task_id="dl1", proc=_FakeProc(), reaping=False)}
    _patch_queue(q, w, monkeypatch, tmp_path, workers)
    now = time.time()
    q.RUNNING["dl1"] = {
        "task": {"id": "dl1", "type": "task", "deadline_at": "2000-01-01T00:00:00Z"},
        "started_at": now - 30, "last_progress_at": now - 1,  # actively progressing
        "worker_id": 3, "attempt": 1,
    }

    q.enforce_task_timeouts()
    while not q._reap_queue.empty():
        q._reap_timed_out_task(q._reap_queue.get_nowait())

    assert "dl1" not in q.RUNNING, "a past-deadline task must be stopped even while progressing"
    assert q.PENDING == []  # deadline => no retry
    res = load_task_result(tmp_path, "dl1")
    assert res["status"] == STATUS_FAILED
    assert res["reason_code"] == "deadline"


def test_has_live_descendant_detects_orchestrator(tmp_path, monkeypatch):
    from supervisor import queue as q
    monkeypatch.setattr(q, "RUNNING", {
        "root": {"task": {"id": "root"}},
        "c": {"task": {"id": "c", "parent_task_id": "root"}},
        "gc": {"task": {"id": "gc", "parent_task_id": "c"}},
        "other": {"task": {"id": "other"}},
    })
    monkeypatch.setattr(q, "PENDING", [])
    assert q._has_live_descendant("root") is True   # via c and gc
    assert q._has_live_descendant("c") is True       # via gc
    assert q._has_live_descendant("gc") is False     # leaf
    assert q._has_live_descendant("other") is False

    # A parent whose only child is still QUEUED (PENDING, not yet assigned) is still an
    # orchestrator and must not be blind-retried.
    monkeypatch.setattr(q, "RUNNING", {"p": {"task": {"id": "p"}}})
    monkeypatch.setattr(q, "PENDING", [{"id": "pc", "parent_task_id": "p", "root_task_id": "p"}])
    assert q._has_live_descendant("p") is True
    assert q._has_live_descendant("nope") is False


def test_descendant_detection_survives_missing_intermediate_parent(tmp_path, monkeypatch):
    """A grandchild whose intermediate parent already left RUNNING is still a descendant of
    the root (via root_task_id), so the root orchestrator is recognised (not blind-retried)
    and a progressing grandchild keeps it alive."""
    from supervisor import queue as q
    now = time.time()
    monkeypatch.setattr(q, "RUNNING", {
        "root": {"task": {"id": "root"}},
        # intermediate parent 'c' is GONE from RUNNING; grandchild remains (root_task_id=root)
        "gc": {"task": {"id": "gc", "parent_task_id": "c", "root_task_id": "root"},
               "last_progress_at": now - 1, "started_at": now - 30},
    })
    assert q._has_live_descendant("root") is True
    assert q._subtree_progressing("root", now, 100.0) is True
    # a stale grandchild does NOT keep the root alive
    q.RUNNING["gc"]["last_progress_at"] = now - 10_000
    assert q._subtree_progressing("root", now, 100.0) is False


def test_capability_and_queue_digest_in_runtime_context(tmp_path, monkeypatch):
    """The honesty SSOT: the live capability gate is surfaced into context each turn."""
    import ouroboros.context as ctx_mod

    monkeypatch.setattr(ctx_mod, "get_git_info", lambda *a, **k: ("ouroboros", "abc1234"), raising=False)
    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "true")
    env = SimpleNamespace(repo_dir=tmp_path, drive_root=tmp_path,
                          drive_path=lambda p: tmp_path / p)
    section = ctx_mod.build_runtime_section(env, {"id": "t1", "type": "task"})
    assert '"allow_mutative_subagents": true' in section
    assert "MASTER gate" in section  # the honest note teaching forward reasoning

    monkeypatch.setenv("OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS", "false")
    section_off = ctx_mod.build_runtime_section(env, {"id": "t1", "type": "task"})
    assert '"allow_mutative_subagents": false' in section_off


def test_tree_tools_in_core_and_initial_schemas():
    """tree_note/tree_read must be in the round-one envelope so a NORMAL parent can publish
    the shared frame before fan-out (no enable_tools detour)."""
    import pathlib
    import tempfile

    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    from ouroboros.tool_policy import initial_tool_schemas
    from ouroboros.tools.registry import ToolRegistry

    assert "tree_note" in CORE_TOOL_NAMES and "tree_read" in CORE_TOOL_NAMES
    with tempfile.TemporaryDirectory() as d:
        reg = ToolRegistry(repo_dir=pathlib.Path(d), drive_root=pathlib.Path(d))
        names = {s["function"]["name"] for s in initial_tool_schemas(reg)}
        assert "tree_note" in names and "tree_read" in names


def test_tree_tools_available_to_subagents_but_writes_stay_blocked():
    """Both-paths isolation contract: a subagent (read-only AND acting) CAN coordinate via
    the task-tree ledger (tree_note/tree_read), while the repo/data/runtime write-escalation
    surface stays blocked. The capability sets are the SSOT the registry filter reads."""
    from ouroboros.tool_capabilities import (
        ACTING_SUBAGENT_TOOL_NAMES,
        LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
    )

    for name in ("tree_note", "tree_read"):
        assert name in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
        assert name in ACTING_SUBAGENT_TOOL_NAMES
    # a read-only subagent that can tree_note must STILL NOT escalate to real writes
    for blocked in ("write_file", "edit_text", "run_command", "commit_reviewed", "knowledge_write"):
        assert blocked not in LOCAL_READONLY_SUBAGENT_TOOL_NAMES


def test_tree_ledger_scope_and_attention(monkeypatch, tmp_path):
    # Point the ledger at tmp_path WITHOUT a global config reload (which would leave
    # ouroboros.config.DATA_DIR stuck on this tmp_path and pollute later tests): the ledger
    # reads its module-level DATA_DIR, so monkeypatch THAT (auto-restored after the test).
    import ouroboros.task_tree_ledger as L
    monkeypatch.setattr(L, "DATA_DIR", str(tmp_path))

    assert L.tree_ledger_append("rootA", "contract", "API: f()->g", task_id="rootA", role="lead").startswith("OK")
    assert L.tree_ledger_append("rootA", "question", "lib X or Y?", task_id="c1", role="scout").startswith("OK")
    # blocker/question imply parent attention
    att = L.tree_ledger_attention_after("rootA", "")
    assert len(att) == 1 and att[0]["kind"] == "question"
    # bad kind rejected; empty text rejected
    assert "TOOL_ARG_ERROR" in L.tree_ledger_append("rootA", "bogus", "x")
    assert "TOOL_ARG_ERROR" in L.tree_ledger_append("rootA", "note", "")
    # scope isolation: a different tree has its own ledger
    assert L.tree_ledger_rows("rootB") == []
    digest = L.tree_ledger_tail_digest("rootA")
    assert "contract" in digest and "needs_parent_attention" in digest


def test_reaper_finalizes_stuck_artifact_on_self_finalized_result(tmp_path, monkeypatch):
    """Round-10 crit#2: a worker that self-finalized a workspace child but died before the
    parent ran artifact finalization leaves artifact_status stuck at 'finalizing'. The reaper
    terminalized the task (it is no longer in RUNNING), so the normal task_done finalize path
    finds nothing — the reaper must complete it. It must rescue ONLY a stuck non-terminal
    artifact state (re-finalizing a terminal result could regress it to FAILED) and skip
    readonly subagents (no durable artifacts), via the shared task_is_readonly_subagent gate."""
    from supervisor import queue as q
    from supervisor import workers as w
    from ouroboros import headless
    from ouroboros.task_results import write_task_result

    workers = {4: SimpleNamespace(busy_task_id=None, proc=_FakeProc(), reaping=True)}
    _patch_queue(q, w, monkeypatch, tmp_path, workers)
    monkeypatch.setattr(q, "_kept_service_pids", lambda: set(), raising=False)

    calls = []
    monkeypatch.setattr(headless, "finalize_task_artifacts",
                        lambda root, task: (calls.append(str(task.get("id"))), [])[1])

    def _run(task, artifact_status):
        # Pre-write the worker's own terminal result so the reaper's post-kill re-check honors
        # it (self_status set) instead of clobbering it — the branch crit#2 lives in.
        write_task_result(tmp_path, str(task["id"]), "completed", artifact_status=artifact_status)
        q._reap_timed_out_task({"worker_id": 4, "proc": None, "task_id": task["id"],
                                "task": task, "task_type": "task",
                                "terminal_reason": "idle_timeout", "attempt": 1})

    # 1. stuck 'finalizing' → reaper completes finalization
    _run({"id": "wt1", "type": "task"}, "finalizing")
    assert calls == ["wt1"], "reaper must finalize a self-finalized result stuck at 'finalizing'"

    # 2. already-terminal artifact_status → NOT re-finalized (no regression)
    calls.clear()
    _run({"id": "wt2", "type": "task"}, "ready")
    assert calls == [], "an already-terminal artifact result must not be re-finalized"

    # 3. readonly subagent → skipped even when stuck (no durable owner-facing artifacts)
    calls.clear()
    _run({"id": "wt3", "type": "task", "delegation_role": "subagent",
          "task_constraint": {"mode": "local_readonly_subagent"}}, "finalizing")
    assert calls == [], "a readonly subagent has no durable artifacts to finalize"


def test_task_is_readonly_subagent_gate():
    """The single SSOT gate the task_done path and the reaper both read (a re-derivation
    drift of this rule is what stranded the reaper's artifact finalization)."""
    from ouroboros.headless import task_is_readonly_subagent

    assert task_is_readonly_subagent(
        {"delegation_role": "subagent", "task_constraint": {"mode": "local_readonly_subagent"}}) is True
    # constraint nested under metadata is honored too
    assert task_is_readonly_subagent(
        {"delegation_role": "subagent", "metadata": {"task_constraint": {"mode": "local_readonly_subagent"}}}) is True
    # acting subagents and plain tasks are NOT readonly → they DO finalize artifacts
    assert task_is_readonly_subagent(
        {"delegation_role": "subagent", "task_constraint": {"mode": "acting_subagent"}}) is False
    assert task_is_readonly_subagent({"id": "root", "type": "task"}) is False
    assert task_is_readonly_subagent(None) is False
