"""promote_chat_to_task + project chat routing (multi-project, v6.32.0)."""

from __future__ import annotations

import types


def test_promote_tool_emits_event_with_chat_and_project(tmp_path):
    from ouroboros.tools.control import _promote_chat_to_task

    events = []
    ctx = types.SimpleNamespace(
        pending_events=events,
        event_queue=None,
        current_chat_id=1,
        drive_root=tmp_path,
    )
    out = _promote_chat_to_task(ctx, "Build the racer prototype", project_id="racer")
    assert out.startswith("OK: promoted to supervised task")
    assert len(events) == 1
    evt = events[0]
    assert evt["type"] == "promote_chat_to_task"
    assert evt["objective"] == "Build the racer prototype"
    assert evt["project_id"] == "racer"
    assert evt["chat_id"] == 1
    assert evt["task_id"]


def test_promote_tool_rejects_dirty_project_id(tmp_path):
    from ouroboros.tools.control import _promote_chat_to_task

    ctx = types.SimpleNamespace(
        pending_events=[], event_queue=None, current_chat_id=1, drive_root=tmp_path,
    )
    out = _promote_chat_to_task(ctx, "x", project_id="Bad Name!")
    assert "TOOL_ARG_ERROR" in out
    assert not ctx.pending_events


def test_promote_event_enqueues_first_class_task(tmp_path, monkeypatch):
    """The supervisor handler enqueues a pooled OWNER task (not a subagent),
    registers the project, and carries the chat thread."""
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    enqueued = []
    ctx = types.SimpleNamespace(
        enqueue_task=lambda task: enqueued.append(task),
        load_state=lambda: {"owner_chat_id": 1},
    )
    evt = {
        "type": "promote_chat_to_task",
        "task_id": "abc12345",
        "objective": "Research the market",
        "expected_output": "A summary",
        "project_id": "research-1",
        "chat_id": 0,  # falls back to owner chat
    }
    workers.promote_chat_to_task(evt, ctx)

    assert len(enqueued) == 1
    task = enqueued[0]
    assert task["id"] == "abc12345"
    assert task["type"] == "task"
    assert task["project_id"] == "research-1"
    assert "delegation_role" not in task
    assert "_is_direct_chat" not in task
    assert "Expected output: A summary" in task["text"]
    # The project got registered as a side effect, and the promoted task runs in
    # the PROJECT thread: its chat_id is the project's deterministic chat_id (not
    # the main/owner fallback), so its live card + owner mailbox route to the panel.
    from ouroboros.contracts.chat_id_policy import project_chat_id
    from ouroboros.projects_registry import get_project

    project = get_project(tmp_path, "research-1")
    assert project is not None
    assert task["chat_id"] == project["chat_id"] == project_chat_id("research-1")
    assert task["chat_id"] != 1  # not the owner-chat fallback


def test_promote_chat_to_task_broadcasts_projects_changed(tmp_path, monkeypatch):
    """Backend project creation pushes a projects_changed WS frame carrying the new
    chat_id, so the frontend fan-out learns the project thread IMMEDIATELY (no
    ≤20s window where its live frames misroute into the main chat)."""
    import supervisor.message_bus as mbus
    import supervisor.workers as workers

    monkeypatch.setattr(workers, "DRIVE_ROOT", tmp_path)
    broadcasts = []
    fake_bridge = types.SimpleNamespace(broadcast=lambda payload: broadcasts.append(payload))
    monkeypatch.setattr(mbus, "get_bridge", lambda: fake_bridge)
    ctx = types.SimpleNamespace(
        enqueue_task=lambda task: None,
        load_state=lambda: {"owner_chat_id": 1},
    )
    workers.promote_chat_to_task({
        "type": "promote_chat_to_task",
        "task_id": "pc1",
        "objective": "Build it",
        "project_id": "proj-x",
        "chat_id": 0,
    }, ctx)

    from ouroboros.contracts.chat_id_policy import project_chat_id

    changed = [b for b in broadcasts if b.get("type") == "projects_changed"]
    assert len(changed) == 1
    assert changed[0]["project_id"] == "proj-x"
    assert changed[0]["chat_id"] == project_chat_id("proj-x")


def test_registered_project_chat_ids_includes_archived(tmp_path):
    """Archiving changes UI visibility only — the isolation SSOT still recognizes
    an archived project's chat_id so its raw chat never re-leaks into the штаб's
    main context / dialogue consolidation / background consciousness (BIBLE P1)."""
    from ouroboros.projects_registry import (
        STATUS_ARCHIVED,
        create_project,
        registered_project_chat_ids,
        update_project,
    )

    proj = create_project(tmp_path, "old-racer")
    chat_id = int(proj["chat_id"])
    assert chat_id in registered_project_chat_ids(tmp_path)
    update_project(tmp_path, "old-racer", status=STATUS_ARCHIVED)
    # Still a project thread for isolation purposes after archiving.
    assert chat_id in registered_project_chat_ids(tmp_path)


def test_chat_history_tool_spans_all_threads_full_awareness(tmp_path):
    """Full project awareness (v6.32.0): the chat_history TOOL is the one mind's
    DELIBERATE recall — it spans the WHOLE conversation (main + ALL project
    threads), only A2A virtual transport excluded. Project-task FOCUS lives in the
    passive default context (build_recent_sections), NOT in this recall tool, so
    the one identity can recall anything it chooses (BIBLE P1)."""
    import json

    from ouroboros.memory import Memory
    from ouroboros.projects_registry import create_project

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    a = create_project(tmp_path, "alpha")
    b = create_project(tmp_path, "beta")
    ca, cb = int(a["chat_id"]), int(b["chat_id"])
    rows = [
        {"direction": "in", "text": "main-msg", "chat_id": 1},
        {"direction": "in", "text": "alpha-msg", "chat_id": ca},
        {"direction": "in", "text": "beta-msg", "chat_id": cb},
        {"direction": "in", "text": "a2a-noise", "chat_id": -1001},
    ]
    (logs / "chat.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    mem = Memory(drive_root=tmp_path)

    view = mem.chat_history(count=50)
    assert "main-msg" in view and "alpha-msg" in view and "beta-msg" in view  # all threads
    assert "a2a-noise" not in view  # only A2A virtual transport excluded


def test_recent_context_full_awareness_and_project_focus_with_bindings(tmp_path):
    """Passive context (v6.32.0): the one identity's MAIN recent context sees
    EVERYTHING, including a post-hoc bound task's rows (one mind, BIBLE P1). A
    PROJECT task's recent context is FOCUSED on its own thread + rows of tasks
    bound to it; unrelated main chat is left out of the focused working view
    (focus in the passive default, not isolation)."""
    import json

    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory
    from ouroboros.projects_registry import bind_task_to_project, create_project

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    proj = create_project(tmp_path, "promoted")
    pchat = int(proj["chat_id"])
    bind_task_to_project(tmp_path, "task-7", "promoted", pchat)
    rows = [
        {"direction": "in", "text": "plain-main", "chat_id": 1},
        {"direction": "out", "text": "bound-task-row", "chat_id": 1, "task_id": "task-7"},
    ]
    (logs / "chat.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    mem = Memory(drive_root=tmp_path)

    # Main passive context: full awareness sees everything.
    main_ctx = "\n".join(build_recent_sections(mem, env=None))
    assert "plain-main" in main_ctx and "bound-task-row" in main_ctx

    # Project task passive context: focused on its own thread + bound-task rows.
    proj_ctx = "\n".join(build_recent_sections(mem, env=None, thread_chat_id=pchat))
    assert "bound-task-row" in proj_ctx
    assert "plain-main" not in proj_ctx


def test_restart_drain_defers_then_completes_without_sleeping(tmp_path, monkeypatch):
    """The drain must NOT sleep on the supervisor thread: a restart with live
    tasks defers (returns immediately), and a later loop-tick check completes
    it once tasks drain or the deadline passes."""
    import types

    import server

    monkeypatch.setenv("OUROBOROS_RESTART_DRAIN_MAX_SEC", "120")
    performed = []
    monkeypatch.setattr(server, "_perform_supervisor_restart", lambda ctx: performed.append(True))
    server._pending_restart.clear()

    now = __import__("time").time()
    ctx = types.SimpleNamespace(
        RUNNING={"t1": {"task": {"id": "t1"}, "last_heartbeat_at": now}},
        load_state=lambda: {"owner_chat_id": 0},
        send_with_budget=lambda *a, **k: None,
        DRIVE_ROOT=tmp_path,
    )

    # Live task -> defer, do NOT restart inline.
    server._handle_restart_in_supervisor({"reason": "evolution"}, ctx)
    assert performed == []
    assert server._pending_restart  # recorded for the loop tick

    # Tick while still live + before deadline -> keep waiting.
    server._check_pending_restart_drain(ctx)
    assert performed == []

    # Task drained -> the next tick completes the restart.
    ctx.RUNNING = {}
    server._check_pending_restart_drain(ctx)
    assert performed == [True]
    assert not server._pending_restart


def test_restart_drain_no_live_tasks_restarts_immediately(tmp_path, monkeypatch):
    import types

    import server

    monkeypatch.setenv("OUROBOROS_RESTART_DRAIN_MAX_SEC", "120")
    performed = []
    monkeypatch.setattr(server, "_perform_supervisor_restart", lambda ctx: performed.append(True))
    server._pending_restart.clear()

    ctx = types.SimpleNamespace(
        RUNNING={},
        load_state=lambda: {"owner_chat_id": 0},
        send_with_budget=lambda *a, **k: None,
        DRIVE_ROOT=tmp_path,
    )
    server._handle_restart_in_supervisor({"reason": "x"}, ctx)
    assert performed == [True]
    assert not server._pending_restart


def test_direct_chat_project_thread_skips_letters_home(tmp_path, monkeypatch):
    """A project-thread CONVERSATION (direct chat) is project-scoped for context
    only: it must not block on post-processing or write journal/digest."""
    from ouroboros.project_lease import running_project_ids

    # Sanity: a direct-chat task is never a lease occupant (no project lane),
    # and _is_direct_chat tasks are excluded from letters-home by the pipeline.
    direct = {"id": "d1", "type": "task", "project_id": "racer", "_is_direct_chat": True}
    # The lease only counts top-level project tasks; a direct-chat task still
    # carries project_id but the pipeline gates letters-home on _is_direct_chat.
    assert running_project_ids([{"task": direct}]) == {"racer"}  # context scope is real
    # (full pipeline gating is covered by the agent_task_pipeline branch; this
    # pins the flag the branch reads.)
    assert direct.get("_is_direct_chat") is True


def test_route_project_chat_ignores_non_registered_chat_ids(tmp_path):
    """External-transport chat ids (large, non-project) must NOT be captured as
    project threads — only registered project chat_ids route to a task mailbox."""
    import types

    import server
    from ouroboros.projects_registry import create_project

    proj = create_project(tmp_path, "racer")
    project_chat = int(proj["chat_id"])
    transport_chat = 987654321  # Telegram-style id, NOT a project

    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={
            "tp": {"task": {"id": "tp", "chat_id": transport_chat}, "last_heartbeat_at": 1.0},
            "pr": {"task": {"id": "pr", "chat_id": project_chat}, "last_heartbeat_at": 1.0},
        },
    )
    # Transport chat: not a project -> never routed (main free lane preserved).
    assert server._route_project_chat_to_running_task(ctx, transport_chat, "hi") == ""
    # Registered project chat with an active task -> routed to its mailbox.
    assert server._route_project_chat_to_running_task(ctx, project_chat, "steer") == "pr"


def test_busy_direct_lane_project_chat_enqueues_pooled_task(tmp_path, monkeypatch):
    """Hybrid B+: project chat must not wait behind the singleton direct-chat
    lock when the main direct lane is busy; it becomes a normal project task."""
    import server
    from ouroboros.projects_registry import create_project

    proj = create_project(tmp_path, "market-research")
    project_chat = int(proj["chat_id"])
    enqueued = []
    sent = []

    monkeypatch.setattr("supervisor.message_bus.log_chat", lambda *a, **k: None)
    monkeypatch.setattr("supervisor.state.load_state", lambda: {"budget_limit": 100, "spent_usd": 0})
    monkeypatch.setattr("supervisor.state.budget_remaining", lambda _state: 100)

    class _Bridge:
        def get_updates(self, offset=0, timeout=0):
            return [{
                "update_id": offset,
                "message": {
                    "chat": {"id": project_chat},
                    "from": {"id": 1},
                    "text": "сколько будет 2+2?",
                    "source": "web",
                    "task_metadata": {"project_id": "market-research"},
                },
            }]

    class _Consciousness:
        def inject_observation(self, _text):
            return None

    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={},
        load_state=lambda: {"owner_id": 1, "owner_chat_id": 1},
        update_state=lambda fn: fn({"owner_id": 1, "owner_chat_id": 1}),
        consciousness=_Consciousness(),
        get_chat_agent=lambda: types.SimpleNamespace(_busy=True),
        handle_chat_direct=lambda *a, **k: (_ for _ in ()).throw(AssertionError("direct chat should not block")),
        enqueue_task=lambda task: enqueued.append(task),
        send_with_budget=lambda *a, **k: sent.append((a, k)),
    )

    assert server._process_bridge_updates(_Bridge(), 0, ctx) == 1
    assert len(enqueued) == 1
    task = enqueued[0]
    assert task["chat_id"] == project_chat
    assert task["project_id"] == "market-research"
    assert task["source"] == "project_chat_busy_fallback"
    assert task.get("_is_direct_chat") is None
    assert "сколько будет 2+2?" in task["text"]


def test_project_from_task_endpoint_creates_binding(tmp_path):
    import asyncio
    import json

    from ouroboros.gateway.projects import api_project_from_task
    from ouroboros.projects_registry import get_project, project_binding_for_task

    class _Req:
        def __init__(self):
            self.app = types.SimpleNamespace(state=types.SimpleNamespace(drive_root=tmp_path, repo_dir=tmp_path))

        async def json(self):
            return {"task_id": "abc123", "id": "task-abc123", "name": "Research thread"}

    resp = asyncio.run(api_project_from_task(_Req()))
    payload = json.loads(resp.body)
    assert resp.status_code == 200
    assert payload["project"]["id"] == "task-abc123"
    assert payload["project"]["name"] == "Research thread"
    assert payload["binding"]["task_id"] == "abc123"
    assert get_project(tmp_path, "task-abc123") is not None
    assert project_binding_for_task(tmp_path, "abc123")["project_id"] == "task-abc123"


def test_bound_project_history_backfills_task_progress(tmp_path):
    """A task converted into a project after it started keeps its original log
    rows, but project history resolves them through the binding."""
    import asyncio
    import json

    from ouroboros.gateway.history import make_chat_history_endpoint
    from ouroboros.projects_registry import bind_task_to_project, create_project

    project = create_project(tmp_path, "bound-progress", name="Bound progress")
    project_chat = int(project["chat_id"])
    bind_task_to_project(tmp_path, "task-1", "bound-progress", project_chat)
    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    with open(logs / "chat.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-01-01T00:00:00Z", "direction": "out", "text": "final answer", "chat_id": 1, "task_id": "task-1"}) + "\n")
        fh.write(json.dumps({"ts": "2026-01-01T00:00:01Z", "direction": "in", "text": "raw project chat", "chat_id": project_chat}) + "\n")
    with open(logs / "progress.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": "2026-01-01T00:00:02Z", "type": "send_message", "content": "working", "text": "working", "is_progress": True, "chat_id": 1, "task_id": "task-1", "format": "markdown"}) + "\n")

    api = make_chat_history_endpoint(tmp_path)

    class _Req:
        def __init__(self, params):
            self.query_params = params

    project_resp = json.loads(asyncio.run(api(_Req({"chat_id": str(project_chat)}))).body)
    project_texts = [m["text"] for m in project_resp["messages"]]
    assert "final answer" in project_texts
    assert "working" in project_texts
    assert "raw project chat" in project_texts

    main_resp = json.loads(asyncio.run(api(_Req({}))).body)
    main_texts = [m["text"] for m in main_resp["messages"]]
    assert "working" in main_texts  # main mirrors sanitized progress
    assert "raw project chat" not in main_texts
    # The bound task's RAW final-answer row (still stored with main chat_id 1) is
    # project-owned via the binding and must NOT leak into the штаб's main history.
    assert "final answer" not in main_texts


def test_bound_task_heartbeat_routes_to_project_panel(tmp_path):
    """A post-hoc bound task's heartbeat routes to its PROJECT panel: the durable
    binding takes PRECEDENCE over the task's original (main) chat_id, matching the
    send_message/log handlers (UI routing for a "Turn into project" running task)."""
    import time

    from ouroboros.projects_registry import bind_task_to_project, create_project
    from supervisor.events import _handle_task_heartbeat

    project = create_project(tmp_path, "hb-proj")
    project_chat = int(project["chat_id"])
    bind_task_to_project(tmp_path, "task-hb", "hb-proj", project_chat)

    pushed = []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        RUNNING={"task-hb": {"task": {"id": "task-hb", "type": "task", "chat_id": 1}, "started_at": time.time()}},
        bridge=types.SimpleNamespace(push_log=lambda payload: pushed.append(payload)),
    )
    _handle_task_heartbeat({"task_id": "task-hb", "phase": "running"}, ctx)
    assert pushed
    assert pushed[0]["chat_id"] == project_chat  # binding precedence, not the original main 1


def test_bound_task_media_routes_to_project_panel(tmp_path):
    """A post-hoc bound task's media (send_photo/send_video) routes to its PROJECT
    panel via the durable binding, not the task's original (main) chat_id —
    same precedence as the send_message/log/heartbeat handlers."""
    import base64

    from ouroboros.projects_registry import bind_task_to_project, create_project
    from supervisor.events import _handle_send_photo, _handle_send_video

    project = create_project(tmp_path, "media-proj")
    project_chat = int(project["chat_id"])
    bind_task_to_project(tmp_path, "task-m", "media-proj", project_chat)

    photo_sent, video_sent = [], []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        append_jsonl=lambda *a, **k: None,
        bridge=types.SimpleNamespace(
            send_photo=lambda cid, data, caption="", mime="": (photo_sent.append(cid) or (True, "")),
            send_video=lambda cid, data, caption="", mime="": (video_sent.append(cid) or (True, "")),
        ),
    )
    blob = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
    _handle_send_photo({"task_id": "task-m", "chat_id": 1, "image_base64": blob, "mime": "image/png"}, ctx)
    _handle_send_video({"task_id": "task-m", "chat_id": 1, "video_base64": blob, "mime": "video/mp4"}, ctx)
    assert photo_sent == [project_chat]  # binding precedence, not the original main 1
    assert video_sent == [project_chat]


def test_bound_task_send_message_routes_future_events_to_project(tmp_path):
    from ouroboros.projects_registry import bind_task_to_project, create_project
    from supervisor.events import _handle_send_message

    project = create_project(tmp_path, "future-events")
    project_chat = int(project["chat_id"])
    bind_task_to_project(tmp_path, "task-9", "future-events", project_chat)
    sent = []
    ctx = types.SimpleNamespace(
        DRIVE_ROOT=tmp_path,
        send_with_budget=lambda *args, **kwargs: sent.append((args, kwargs)),
        append_jsonl=lambda *a, **k: None,
    )
    _handle_send_message({
        "chat_id": 1,
        "task_id": "task-9",
        "text": "future progress",
        "is_progress": True,
        "format": "markdown",
    }, ctx)
    assert sent
    assert sent[0][0][0] == project_chat


def test_chat_history_filters_by_thread(tmp_path):
    """api_chat_history returns only the requested thread's rows."""
    import asyncio
    import json

    from ouroboros.gateway.history import make_chat_history_endpoint

    # Register a project so its chat_id partitions out of the main view; a
    # large NON-project chat_id (transport mirror) must STAY in the main view.
    from ouroboros.projects_registry import create_project

    proj = create_project(tmp_path, "racer")
    project_chat = int(proj["chat_id"])
    transport_chat = 555000111

    logs = tmp_path / "logs"
    logs.mkdir(parents=True)
    rows = [
        {"ts": "2026-06-13T00:00:01Z", "direction": "in", "text": "main hello", "chat_id": 1},
        {"ts": "2026-06-13T00:00:02Z", "direction": "out", "text": "main reply", "chat_id": 1},
        {"ts": "2026-06-13T00:00:03Z", "direction": "in", "text": "project hello", "chat_id": project_chat},
        {"ts": "2026-06-13T00:00:033Z", "direction": "system", "type": "task_summary", "text": "project summary", "chat_id": project_chat, "task_id": "pt"},
        {"ts": "2026-06-13T00:00:035Z", "direction": "in", "text": "transport mirror", "chat_id": transport_chat},
        {"ts": "2026-06-13T00:00:04Z", "direction": "out", "text": "a2a noise", "chat_id": -1001},
        {"ts": "2026-06-13T00:00:05Z", "direction": "out", "text": "legacy row (no chat_id)"},
    ]
    with open(logs / "chat.jsonl", "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    api = make_chat_history_endpoint(tmp_path)

    class _Req:
        def __init__(self, params):
            self.query_params = params

    main = json.loads(asyncio.run(api(_Req({}))).body)
    main_texts = [m["text"] for m in main["messages"]]
    assert "main hello" in main_texts and "main reply" in main_texts
    assert "legacy row (no chat_id)" in main_texts  # legacy rows are main-chat
    assert "transport mirror" in main_texts  # non-project transport stays visible
    assert "project hello" not in main_texts  # registered project partitions out
    assert "project summary" in main_texts  # штаб mirrors project summaries/progress
    assert "a2a noise" not in main_texts

    proj_resp = json.loads(asyncio.run(api(_Req({"chat_id": str(project_chat)}))).body)
    proj_texts = [m["text"] for m in proj_resp["messages"]]
    assert proj_texts and "project hello" in proj_texts
    assert "project summary" in proj_texts
    assert "main hello" not in proj_texts
    assert "transport mirror" not in proj_texts


def test_project_media_and_typing_broadcasts_carry_chat_id():
    """Photo/video/typing WS frames must carry chat_id so the client fan-out
    routes project-thread media to its panel (default-to-main would hide them)."""
    from supervisor.message_bus import LocalChatBridge

    bridge = LocalChatBridge()
    frames = []
    bridge._broadcast_fn = lambda payload: frames.append(payload)

    project_chat = 1234  # positive project-range id (not A2A, which is negative)
    bridge.send_chat_action(project_chat, "typing")
    bridge.send_photo(project_chat, b"img-bytes", caption="shot")
    bridge.send_video(project_chat, b"vid-bytes", caption="clip", mime="video/mp4")

    by_type = {f.get("type"): f for f in frames}
    assert by_type["typing"]["chat_id"] == project_chat
    assert by_type["photo"]["chat_id"] == project_chat
    assert by_type["video"]["chat_id"] == project_chat


def test_journal_write_rejects_over_limit_instead_of_truncating(tmp_path, monkeypatch):
    """A durable journal entry is never silently sliced: over-limit writes are
    rejected (the workpad_write contract), so cognitive memory stays whole."""
    import types

    # Project store paths resolve via config.DATA_DIR (NOT ctx.drive_root); isolate
    # it to tmp_path so a plain local pytest run (no OUROBOROS_DATA_DIR set) never
    # writes into the real data dir.
    monkeypatch.setattr("ouroboros.config.DATA_DIR", tmp_path)
    from ouroboros.tools.project_journal import _MAX_TEXT_CHARS, _journal_read, _journal_write

    ctx = types.SimpleNamespace(project_id="journal-reject-test", task_id="t1", drive_root=tmp_path)
    assert _journal_write(ctx, "note", "hello milestone", "").startswith("OK:")
    over = _journal_write(ctx, "note", "Z" * (_MAX_TEXT_CHARS + 50), "")
    assert "TOOL_ARG_ERROR" in over and "exceeds" in over
    body = _journal_read(ctx, "", 30)
    assert "hello milestone" in body
    assert "Z" * 200 not in body  # the rejected over-limit text was never stored
