from __future__ import annotations

class Bridge:
    def __init__(self, messages):
        self._messages = list(messages)
    def get_updates(self, offset=0, timeout=1):
        return [{"update_id": offset + idx, "message": msg} for idx, msg in enumerate(self._messages)]
    def broadcast(self, _payload):
        pass

class Ctx:
    def __init__(self, state):
        self.state = dict(state)
        self.sent = []
        self.consciousness = None
        self.kill_workers = None
    def load_state(self):
        return dict(self.state)
    def save_state(self, state):
        self.state = dict(state)
    def update_state(self, mutator):
        st = dict(self.state)
        mutator(st)
        self.state = st
        return st
    def send_with_budget(self, chat_id, text, **_kwargs):
        self.sent.append((chat_id, text))

def test_external_first_slash_binds_external_owner_without_executing(monkeypatch):
    import server
    import supervisor.message_bus as message_bus
    called = []
    bridge = Bridge([{"chat": {"id": 42}, "from": {"id": 7}, "text": "/panic", "source": "skill:telegram-bridge"}])
    ctx = Ctx({})
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_execute_panic_stop", lambda *args, **kwargs: called.append(True))
    server._process_bridge_updates(bridge, 0, ctx)
    # Global owner binds for outbound routing, external owner binds for slash auth.
    assert ctx.state["owner_id"] == 7
    assert ctx.state["owner_chat_id"] == 42
    assert ctx.state["owner_external_id"] == 7
    assert ctx.state["owner_external_chat_id"] == 42
    assert ctx.state["owner_external_bound_at"]
    assert called == []
    assert ctx.sent == [(42, "✅ Owner chat registered. Send the command again to execute it.")]

def test_external_non_owner_slash_is_ignored(monkeypatch):
    import server
    import supervisor.message_bus as message_bus
    called = []
    bridge = Bridge([{"chat": {"id": 99}, "from": {"id": 8}, "text": "/panic", "source": "skill:telegram-bridge"}])
    # An external owner is already bound to a different chat.
    ctx = Ctx({"owner_id": 7, "owner_chat_id": 42, "owner_external_id": 7, "owner_external_chat_id": 42})
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_execute_panic_stop", lambda *args, **kwargs: called.append(True))
    server._process_bridge_updates(bridge, 0, ctx)
    assert called == []
    assert ctx.sent == [(99, "⚠️ Command ignored: this transport is not the bound owner chat.")]

def test_desktop_web_owner_does_not_lock_out_telegram(monkeypatch):
    # Regression: on desktop the web UI binds owner=1/1 first; a real Telegram
    # owner must still be able to register (TOFU) and then execute slash commands.
    import server
    import supervisor.message_bus as message_bus
    called = []
    ctx = Ctx({"owner_id": 1, "owner_chat_id": 1})
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_execute_panic_stop", lambda *args, **kwargs: called.append(True))
    # First Telegram slash binds the external owner and asks for a resend.
    server._process_bridge_updates(
        Bridge([{"chat": {"id": 42}, "from": {"id": 7}, "text": "/panic", "source": "skill:telegram-bridge"}]),
        0, ctx,
    )
    assert called == []
    assert ctx.state["owner_id"] == 1 and ctx.state["owner_chat_id"] == 1
    assert ctx.state["owner_external_id"] == 7 and ctx.state["owner_external_chat_id"] == 42
    assert ctx.sent[-1] == (42, "✅ Owner chat registered. Send the command again to execute it.")
    # Resend from the bound external owner now executes.
    server._process_bridge_updates(
        Bridge([{"chat": {"id": 42}, "from": {"id": 7}, "text": "/panic", "source": "skill:telegram-bridge"}]),
        0, ctx,
    )
    assert called == [True]

def test_external_negative_id_cannot_bind_or_execute(monkeypatch):
    # Negative (A2A/synthetic) ids fail the chat_id>0 and user_id>0 gate, so they
    # can neither bind the external owner nor execute a slash command.
    import server
    import supervisor.message_bus as message_bus
    called = []
    bridge = Bridge([{"chat": {"id": -1001}, "from": {"id": -1001}, "text": "/panic", "source": "skill:a2a"}])
    ctx = Ctx({})
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_execute_panic_stop", lambda *args, **kwargs: called.append(True))
    server._process_bridge_updates(bridge, 0, ctx)
    assert called == []
    assert "owner_external_id" not in ctx.state
    assert ctx.sent == [(-1001, "⚠️ Command ignored: this transport did not provide owner identity.")]

def test_external_zero_identity_cannot_bind_owner_or_execute_on_retry(monkeypatch):
    import server
    import supervisor.message_bus as message_bus
    from supervisor.message_bus import LocalChatBridge
    called = []
    bridge = LocalChatBridge()
    bridge.enqueue_local_message("/panic", chat_id=0, user_id=0, source="skill:bridge")
    bridge.enqueue_local_message("/panic", chat_id=0, user_id=0, source="skill:bridge")
    ctx = Ctx({})
    monkeypatch.setattr(message_bus, "log_chat", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_execute_panic_stop", lambda *args, **kwargs: called.append(True))
    server._process_bridge_updates(bridge, 0, ctx)
    server._process_bridge_updates(bridge, 1, ctx)
    assert called == []
    assert "owner_id" not in ctx.state
    assert "owner_external_id" not in ctx.state
    assert ctx.sent == [(0, "⚠️ Command ignored: this transport did not provide owner identity."), (0, "⚠️ Command ignored: this transport did not provide owner identity.")]
