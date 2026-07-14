import supervisor.message_bus as message_bus
import ouroboros.event_bus as event_bus


def _make_bridge(monkeypatch, settings=None):
    return message_bus.LocalChatBridge(settings or {})


def test_configure_from_settings_without_legacy_field(monkeypatch):
    """configure_from_settings remains a no-op compatibility path."""
    bridge = _make_bridge(monkeypatch)
    bridge.configure_from_settings({
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "999",
    })
    assert bridge.get_updates(offset=0, timeout=0) == []


def test_ui_send_enqueues_structured_message_and_broadcasts(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    broadcasts = []
    bridge._broadcast_fn = broadcasts.append

    bridge.ui_send("hello", sender_session_id="sess-1", client_message_id="c-1")
    updates = bridge.get_updates(offset=0, timeout=1)

    assert broadcasts[0]["role"] == "user"
    assert broadcasts[0]["sender_session_id"] == "sess-1"
    assert broadcasts[0]["client_message_id"] == "c-1"
    assert updates[0]["message"]["text"] == "hello"
    assert updates[0]["message"]["source"] == "web"
    assert updates[0]["message"]["sender_session_id"] == "sess-1"
    assert updates[0]["message"]["client_message_id"] == "c-1"


def test_ui_send_preserves_suppress_chat_log_flag(monkeypatch):
    bridge = _make_bridge(monkeypatch)

    bridge.ui_send("FULL_PROMPT", broadcast=False, suppress_chat_log=True)
    updates = bridge.get_updates(offset=0, timeout=1)

    assert updates[0]["message"]["text"] == "FULL_PROMPT"
    assert updates[0]["message"]["suppress_chat_log"] is True


def test_send_photo_publishes_transport_event_with_payload(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_photo(123, b"img", caption="caption", mime="image/png")

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_PHOTO
    assert payload["image_base64"]
    assert payload["caption"] == "caption"
    assert payload["mime"] == "image/png"


def test_send_video_publishes_transport_event_with_payload(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_video(123, b"vid", caption="trailer", mime="video/mp4")

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_VIDEO
    assert payload["video_base64"]
    assert payload["caption"] == "trailer"
    assert payload["mime"] == "video/mp4"


def test_send_document_publishes_transport_event_with_payload(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    events = []
    monkeypatch.setattr(event_bus, "publish_event", lambda topic, data: events.append((topic, data)))
    monkeypatch.setattr(message_bus, "publish_event", lambda topic, data: events.append((topic, data)))

    ok, _ = bridge.send_document(
        123, b"filebytes", filename="report.csv", caption="q3", mime="text/csv",
        download_url="/api/files/download?path=Desktop/report.csv",
    )

    assert ok is True
    topic, payload = events[-1]
    assert topic == event_bus.CHAT_DOCUMENT
    assert payload["file_base64"]
    assert payload["filename"] == "report.csv"
    assert payload["caption"] == "q3"
    assert payload["mime"] == "text/csv"
    assert payload["download_url"] == "/api/files/download?path=Desktop/report.csv"


def test_send_document_persists_compact_chat_row(monkeypatch, tmp_path):
    """A delivered document persists a base64-free chat.jsonl row so it can be
    rebuilt on reload (the durable download_url carries the bytes)."""
    import json

    bridge = _make_bridge(monkeypatch)
    bridge._broadcast_fn = lambda *_a, **_k: None
    monkeypatch.setattr(event_bus, "publish_event", lambda *_a, **_k: None)
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_k: None)
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"session_id": "s", "owner_id": 7})

    ok, _ = bridge.send_document(
        123, b"filebytes", filename="report.csv", caption="q3", mime="text/csv",
        download_url="/api/files/download?path=Desktop/report.csv", task_id="t-1",
    )
    assert ok is True

    rows = [json.loads(line) for line in (tmp_path / "logs" / "chat.jsonl").read_text().splitlines() if line.strip()]
    doc_rows = [r for r in rows if r.get("type") == "document"]
    assert len(doc_rows) == 1
    row = doc_rows[0]
    assert row["direction"] == "out"
    assert row["chat_id"] == 123
    assert row["filename"] == "report.csv"
    assert row["mime"] == "text/csv"
    assert row["download_url"] == "/api/files/download?path=Desktop/report.csv"
    assert row["task_id"] == "t-1"
    assert row["text"] == "q3"
    assert row["caption"] == "q3"  # explicit caption survives reload
    assert "file_base64" not in row  # no base64 bloat in chat.jsonl


def test_send_photo_and_video_persist_compact_rows_before_unread_revision(monkeypatch, tmp_path):
    """Durable Project unread never advances for media absent from history."""
    import json

    bridge = _make_bridge(monkeypatch)
    bridge._broadcast_fn = lambda *_a, **_k: None
    monkeypatch.setattr(event_bus, "publish_event", lambda *_a, **_k: None)
    monkeypatch.setattr(message_bus, "publish_event", lambda *_a, **_k: None)
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "load_state", lambda: {"session_id": "s", "owner_id": 7})
    revisions = []
    monkeypatch.setattr(message_bus, "_advance_project_visible_revision", revisions.append)

    bridge.send_photo(123, b"image", caption="shot", mime="image/png")
    bridge.send_video(123, b"video", caption="clip", mime="video/mp4")

    rows = [
        json.loads(line)
        for line in (tmp_path / "logs" / "chat.jsonl").read_text().splitlines()
        if line.strip()
    ]
    assert [(row["type"], row["text"], row["mime"]) for row in rows] == [
        ("photo", "shot", "image/png"),
        ("video", "clip", "video/mp4"),
    ]
    assert all("image_base64" not in row and "video_base64" not in row for row in rows)
    assert revisions == [123, 123]


def test_push_log_broadcast_surfaces_chat_id(monkeypatch):
    """Live log frames surface the task's chat_id top-level so the browser's
    per-thread fan-out routes the live card to its project panel; events with
    no chat_id default to the main chat (0)."""
    bridge = _make_bridge(monkeypatch)
    frames = []
    bridge._broadcast_fn = frames.append

    bridge.push_log({"type": "tool_call", "task_id": "t1", "chat_id": 1234})
    bridge.push_log({"type": "tool_call", "task_id": "t2"})

    logs = [f for f in frames if f.get("type") == "log"]
    assert logs[0]["chat_id"] == 1234
    assert logs[0]["data"]["task_id"] == "t1"
    assert logs[1]["chat_id"] == 0


def test_budget_line_replays_unresolved_attempt_not_stale_state(monkeypatch, tmp_path):
    from ouroboros import usage_accounting as ua
    from supervisor import state as state_module

    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "state" / "state.json").write_text(
        '{"spent_usd":0,"spent_calls":0}\n', encoding="utf-8",
    )
    (tmp_path / "settings.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "logs" / "events.jsonl").write_text("", encoding="utf-8")
    ua.ensure_legacy_imported(tmp_path)
    reservation = ua.reserve_attempt(ua.AttemptRequest(
        model="openai/gpt-5.5",
        provider="openrouter",
        reservation_usd=1.0,
        drive_root=tmp_path,
        global_limit_usd=10.0,
    ))
    ua.mark_dispatched(reservation)
    ua.mark_unresolved(reservation, "timeout")

    stale = {
        "spent_usd": 0,
        "spent_calls": 0,
        "current_branch": "ouroboros",
        "current_sha": "abcdef123456",
    }

    def update_state(mutator):
        mutator(stale)
        return dict(stale)

    monkeypatch.setattr(state_module, "update_state", update_state)
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "TOTAL_BUDGET_LIMIT", 10.0)

    line = message_bus.budget_line(force=True)

    assert "$1.0000 / $10.00" in line
    assert "unresolved <=$1.0000" in line
    assert "ouroboros@abcdef12" in line
    assert "$0.0000 / $10.00" not in line


def test_budget_line_fails_loud_on_mid_ledger_corruption(monkeypatch, tmp_path):
    from ouroboros import usage_accounting as ua
    from supervisor import state as state_module

    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "state" / "state.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "logs" / "events.jsonl").write_text("", encoding="utf-8")
    ua.ensure_legacy_imported(tmp_path)
    reservation = ua.reserve_attempt(ua.AttemptRequest(
        model="openai/gpt-5.5", provider="openrouter", reservation_usd=1.0,
        drive_root=tmp_path, global_limit_usd=10.0,
    ))
    ua.mark_dispatched(reservation)
    ua.mark_unresolved(reservation, "timeout")
    ledger = tmp_path / ua.LEDGER_REL
    rows = ledger.read_text(encoding="utf-8").splitlines()
    ledger.write_text(rows[0] + "\nnot-json\n" + "\n".join(rows[1:]) + "\n", encoding="utf-8")

    stale = {"spent_usd": 0, "current_branch": "ouroboros", "current_sha": "abc"}
    monkeypatch.setattr(
        state_module, "update_state",
        lambda mutator: (mutator(stale), dict(stale))[1],
    )
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "TOTAL_BUDGET_LIMIT", 10.0)

    line = message_bus.budget_line(force=True)

    assert "Budget: unavailable (physical-attempt ledger error)" in line
    assert "$0.0000" not in line


def test_budget_line_marks_quarantined_tail_nonfinal(monkeypatch, tmp_path):
    from ouroboros import usage_accounting as ua
    from supervisor import state as state_module

    (tmp_path / "state").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "state" / "state.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "settings.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "logs" / "events.jsonl").write_text("", encoding="utf-8")
    ua.ensure_legacy_imported(tmp_path)
    reservation = ua.reserve_attempt(ua.AttemptRequest(
        model="openai/gpt-5.5", provider="openrouter", reservation_usd=0.1,
        drive_root=tmp_path, global_limit_usd=10.0,
    ))
    ua.release_attempt(reservation)
    with (tmp_path / ua.LEDGER_REL).open("ab") as handle:
        handle.write(b'{"seq":')

    stale = {"spent_usd": 0, "current_branch": "ouroboros", "current_sha": "abc"}
    monkeypatch.setattr(
        state_module, "update_state",
        lambda mutator: (mutator(stale), dict(stale))[1],
    )
    monkeypatch.setattr(message_bus, "DATA_DIR", tmp_path)
    monkeypatch.setattr(message_bus, "TOTAL_BUDGET_LIMIT", 10.0)

    line = message_bus.budget_line(force=True)

    assert "cost_final no" in line
    assert "ledger_integrity DEGRADED (quarantined ledger tail)" in line
