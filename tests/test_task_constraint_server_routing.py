from types import SimpleNamespace
import asyncio

import server
from ouroboros.gateway import control as gateway_control


class FakeBridge:
    def get_updates(self, offset, timeout=1):
        return [{
            "update_id": 1,
            "message": {
                "chat": {"id": 1},
                "from": {"id": 1},
                "text": "repair skill",
                "task_constraint": {"mode": "skill_repair", "skill_name": "alpha", "payload_root": "skills/external/alpha"},
                "suppress_chat_log": True,
            },
        }]

    def broadcast(self, payload):
        pass


def test_constrained_repair_is_not_injected_into_busy_agent(monkeypatch):
    calls = {"inject": 0, "direct": []}
    agent = SimpleNamespace(_busy=True, inject_message=lambda *a, **k: calls.__setitem__("inject", calls["inject"] + 1))
    ctx = SimpleNamespace(
        load_state=lambda: {"owner_id": 1},
        save_state=lambda st: None,
        update_state=lambda mutator: (lambda st: (mutator(st), st)[1])({"owner_id": 1}),
        consciousness=SimpleNamespace(inject_observation=lambda *_: None, pause=lambda: None, resume=lambda: None),
        get_chat_agent=lambda: agent,
        handle_chat_direct=lambda cid, txt, img, task_constraint=None, task_metadata=None: calls["direct"].append(task_constraint),
    )
    class ImmediateThread:
        def __init__(self, target, args=(), daemon=False):
            self.target = target
            self.args = args
        def start(self):
            self.target(*self.args)
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)

    server._process_bridge_updates(FakeBridge(), 0, ctx)

    assert calls["inject"] == 0
    assert calls["direct"] == [{"mode": "skill_repair", "skill_name": "alpha", "payload_root": "skills/external/alpha"}]


def test_visible_repair_command_is_deduped(monkeypatch):
    calls = []

    class Request:
        async def json(self):
            return {
                "cmd": "repair",
                "visible_text": "Repair task queued",
                "visible_task_id": "skill_repair_alpha",
                "task_constraint": {"mode": "skill_repair", "skill_name": "alpha", "payload_root": "skills/external/alpha"},
            }

    class Bridge:
        def ui_send(self, text, **kwargs):
            calls.append((text, kwargs))

    monkeypatch.setattr(gateway_control, "_RECENT_VISIBLE_COMMANDS", {})
    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: Bridge())
    monkeypatch.setattr("supervisor.message_bus.log_chat", lambda *a, **k: None)
    monkeypatch.setattr(gateway_control, "broadcast_ws_sync", lambda payload: None)

    first = asyncio.run(gateway_control.api_command(Request()))
    second = asyncio.run(gateway_control.api_command(Request()))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(calls) == 1


def test_failed_visible_repair_command_does_not_poison_dedupe(monkeypatch):
    calls = []
    bridges = []

    class Request:
        async def json(self):
            return {
                "cmd": "repair",
                "visible_text": "Repair task queued",
                "visible_task_id": "skill_repair_alpha",
                "task_constraint": {"mode": "skill_repair", "skill_name": "alpha", "payload_root": "skills/external/alpha"},
            }

    class FailingBridge:
        def ui_send(self, text, **kwargs):
            raise RuntimeError("bus down")

    class HealthyBridge:
        def ui_send(self, text, **kwargs):
            calls.append((text, kwargs))

    bridges.extend([FailingBridge(), HealthyBridge()])
    monkeypatch.setattr(gateway_control, "_RECENT_VISIBLE_COMMANDS", {})
    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: bridges.pop(0))
    monkeypatch.setattr("supervisor.message_bus.log_chat", lambda *a, **k: None)
    monkeypatch.setattr(gateway_control, "broadcast_ws_sync", lambda payload: None)

    first = asyncio.run(gateway_control.api_command(Request()))
    second = asyncio.run(gateway_control.api_command(Request()))

    assert first.status_code == 400
    assert second.status_code == 200
    assert len(calls) == 1


def test_visible_repair_command_can_retry_after_short_dedupe_window(monkeypatch):
    calls = []
    now = {"value": 100.0}

    class Request:
        async def json(self):
            return {
                "cmd": "repair",
                "visible_text": "Repair task queued",
                "visible_task_id": "skill_repair_alpha",
                "task_constraint": {"mode": "skill_repair", "skill_name": "alpha", "payload_root": "skills/external/alpha"},
            }

    class Bridge:
        def ui_send(self, text, **kwargs):
            calls.append((text, kwargs))

    monkeypatch.setattr(gateway_control, "_RECENT_VISIBLE_COMMANDS", {})
    monkeypatch.setattr(gateway_control.time, "monotonic", lambda: now["value"])
    monkeypatch.setattr("supervisor.message_bus.get_bridge", lambda: Bridge())
    monkeypatch.setattr("supervisor.message_bus.log_chat", lambda *a, **k: None)
    monkeypatch.setattr(gateway_control, "broadcast_ws_sync", lambda payload: None)

    first = asyncio.run(gateway_control.api_command(Request()))
    now["value"] += gateway_control._VISIBLE_COMMAND_DEDUPE_SEC + 0.1
    second = asyncio.run(gateway_control.api_command(Request()))

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(calls) == 2


def test_scoped_task_metadata_derives_project_from_chat_id():
    """chat_id is the SSOT for thread→project (full project awareness, v6.32.0):
    a registered project chat scopes task_metadata to its OWN project, OVERRIDING
    any client-supplied project_id; a non-project chat DROPS an untrusted client
    project_id; metadata is otherwise preserved (None stays None)."""
    # Registered project chat: override a mismatched client project_id.
    assert server._scoped_task_metadata("proj_a", {"project_id": "proj_b", "x": 1}) == {"project_id": "proj_a", "x": 1}
    # Registered project chat with no client value: set it.
    assert server._scoped_task_metadata("proj_a", None) == {"project_id": "proj_a"}
    # Non-project chat: drop an untrusted client project_id, keep the rest.
    assert server._scoped_task_metadata("", {"project_id": "proj_b", "x": 1}) == {"x": 1}
    # Non-project chat, nothing to scope: unchanged (None preserved).
    assert server._scoped_task_metadata("", None) is None
