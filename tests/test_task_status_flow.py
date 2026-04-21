import json
from types import SimpleNamespace


def test_schedule_task_creates_requested_status_file(tmp_path):
    from ouroboros.tools.control import _schedule_task
    from ouroboros.task_results import STATUS_REQUESTED

    ctx = SimpleNamespace(
        task_depth=0,
        pending_events=[],
        drive_root=tmp_path,
        is_direct_chat=False,
    )

    result = _schedule_task(ctx, "Do the thing", context="Model focus A")

    assert "Task request queued" in result
    task_id = ctx.pending_events[0]["task_id"]
    path = tmp_path / "task_results" / f"{task_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REQUESTED
    assert data["description"] == "Do the thing"
    assert data["context"] == "Model focus A"


def test_get_task_result_returns_full_completed_output(tmp_path):
    from ouroboros.task_results import STATUS_COMPLETED, write_task_result
    from ouroboros.tools.control import _get_task_result

    full_text = ("hello\n" * 1200) + "TAIL_MARKER"
    write_task_result(
        tmp_path,
        "abc123",
        STATUS_COMPLETED,
        result=full_text,
        cost_usd=1.23,
        trace_summary="trace",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _get_task_result(ctx, "abc123")

    assert "TAIL_MARKER" in output
    assert full_text in output
    assert "[BEGIN_SUBTASK_OUTPUT]" in output


def test_wait_for_task_reports_rejected_duplicate(tmp_path):
    from ouroboros.task_results import STATUS_REJECTED_DUPLICATE, write_task_result
    from ouroboros.tools.control import _wait_for_task

    write_task_result(
        tmp_path,
        "dup123",
        STATUS_REJECTED_DUPLICATE,
        duplicate_of="orig999",
        result="Task was rejected as semantically similar to already active task orig999.",
    )

    ctx = SimpleNamespace(drive_root=tmp_path)
    output = _wait_for_task(ctx, "dup123")

    assert "rejected_duplicate" in output
    assert "duplicate_of=orig999" in output


def test_handle_schedule_task_duplicate_writes_rejected_status(tmp_path, monkeypatch):
    from supervisor import events as ev_module
    from ouroboros.task_results import STATUS_REJECTED_DUPLICATE

    monkeypatch.setattr(ev_module, "_find_duplicate_task", lambda *args, **kwargs: "orig111")

    sent = []

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        PENDING = []
        RUNNING = {}

        def load_state(self):
            return {"owner_chat_id": 1}

        def send_with_budget(self, chat_id, text, **kwargs):
            sent.append((chat_id, text))

    ev_module._handle_schedule_task(
        {
            "type": "schedule_task",
            "task_id": "dup222",
            "description": "Do the thing",
            "context": "Model focus B",
            "depth": 1,
        },
        FakeCtx(),
    )

    path = tmp_path / "task_results" / "dup222.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status"] == STATUS_REJECTED_DUPLICATE
    assert data["duplicate_of"] == "orig111"
    assert sent and "Task rejected" in sent[0][1]


def test_handle_text_response_keeps_full_reasoning_note():
    from ouroboros.loop import _handle_text_response

    content = "A" * 500
    llm_trace = {"reasoning_notes": [], "tool_calls": []}
    _, _, updated = _handle_text_response(content, llm_trace, {})

    assert updated["reasoning_notes"] == [content]


def test_request_restart_latches_reason_until_task_end(tmp_path, monkeypatch):
    from ouroboros.tools import control as control_module

    monkeypatch.setattr(control_module, "run_cmd", lambda *args, **kwargs: "value")
    written = {}
    monkeypatch.setattr(control_module, "write_text", lambda path, text: written.setdefault(str(path), text))

    class _Ctx:
        current_task_type = "task"
        last_push_succeeded = True
        pending_events = []
        pending_restart_reason = None
        repo_dir = tmp_path

        def drive_path(self, rel):
            return tmp_path / rel

    ctx = _Ctx()
    result = control_module._request_restart(ctx, "reload runtime")

    assert "Restart requested" in result
    assert ctx.pending_events == []
    assert ctx.pending_restart_reason == "reload runtime"
    assert written
