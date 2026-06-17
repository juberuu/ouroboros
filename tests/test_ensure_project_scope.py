"""v6.37.0 guard (C4.1): the in-task ensure_project_scope affordance — create/attach
a named Ouroboros project and scope the CURRENT running task into it, instead of the
cyber-racing fallback (bare `mkdir ~/Desktop`). Idempotent for the same project,
refuses to re-scope to a different one, rejects subagents."""

from types import SimpleNamespace


def _ctx(**kw):
    base = dict(project_id="", task_metadata={}, task_contract={}, task_id="t1", event_queue=None, pending_events=[])
    base.update(kw)
    return SimpleNamespace(**base)


def test_creates_named_project_and_scopes_current_task():
    from ouroboros.tools.control import _ensure_project_scope
    from ouroboros.project_facts import project_id_from_display_name

    ctx = _ctx()
    out = _ensure_project_scope(ctx, project_name="Cyber Racing")
    assert out.startswith("OK")
    expected_pid = project_id_from_display_name("Cyber Racing")
    # the rest of THIS task is scoped immediately (journal/knowledge work now)
    assert ctx.project_id == expected_pid
    # a durable ensure_project_scope event is emitted for the supervisor
    evs = [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"]
    assert len(evs) == 1
    assert evs[0]["task_id"] == "t1"
    assert evs[0]["project_id"] == expected_pid
    assert evs[0]["project_name"] == "Cyber Racing"


def test_idempotent_same_project_and_refuses_different():
    from ouroboros.tools.control import _ensure_project_scope
    from ouroboros.project_facts import project_id_from_display_name

    pid = project_id_from_display_name("Cyber Racing")
    ctx = _ctx(project_id=pid)
    out = _ensure_project_scope(ctx, project_name="Cyber Racing")
    assert "already scoped" in out
    assert not [e for e in ctx.pending_events if e.get("type") == "ensure_project_scope"]

    ctx2 = _ctx(project_id="other-project")
    out2 = _ensure_project_scope(ctx2, project_name="Cyber Racing")
    assert "cannot be re-scoped" in out2
    assert ctx2.project_id == "other-project"  # scope NOT changed


def test_rejects_subagent_and_requires_an_arg():
    from ouroboros.tools.control import _ensure_project_scope

    # delegation_role lives on task_metadata / contract lineage, not a ctx attr
    out = _ensure_project_scope(_ctx(task_metadata={"delegation_role": "subagent"}), project_name="X")
    assert "subagents" in out.lower()
    out_lineage = _ensure_project_scope(
        _ctx(task_contract={"lineage": {"delegation_role": "subagent"}}), project_name="X"
    )
    assert "subagents" in out_lineage.lower()

    out2 = _ensure_project_scope(_ctx())
    assert "TOOL_ARG_ERROR" in out2


def test_supervisor_handler_creates_binds_updates_running_and_broadcasts(monkeypatch):
    """C4.1 supervisor side (review F1/F2): the handler must create_project,
    bind the task, UPDATE the RUNNING map's task project_id (so the project lease
    counts it as a lane occupant), and broadcast projects_changed."""
    import ouroboros.projects_registry as reg
    import supervisor.message_bus as mb
    from supervisor import workers

    calls = {"create": None, "bind": None, "touch": None, "broadcast": None}
    monkeypatch.setattr(reg, "create_project", lambda dr, pid, **kw: calls.__setitem__("create", (pid, kw)) or {"id": pid, "chat_id": 7})
    monkeypatch.setattr(reg, "bind_task_to_project", lambda dr, tid, pid, chat=None: calls.__setitem__("bind", (tid, pid, chat)))
    monkeypatch.setattr(reg, "touch_project", lambda dr, pid: calls.__setitem__("touch", pid))

    class _Bridge:
        def broadcast(self, payload):
            calls["broadcast"] = payload

    monkeypatch.setattr(mb, "get_bridge", lambda: _Bridge())

    running = {"t1": {"task": {"id": "t1"}}}
    ctx = SimpleNamespace(RUNNING=running)
    workers.ensure_project_scope({"task_id": "t1", "project_id": "cyber-racing", "project_name": "Cyber Racing"}, ctx)

    assert calls["create"][0] == "cyber-racing"
    assert calls["bind"] == ("t1", "cyber-racing", 7)
    assert running["t1"]["task"]["project_id"] == "cyber-racing"  # F1: lease lane occupancy
    assert calls["broadcast"] == {"type": "projects_changed", "project_id": "cyber-racing", "chat_id": 7}
