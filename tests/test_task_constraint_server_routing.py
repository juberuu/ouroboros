from types import SimpleNamespace

import server


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
        consciousness=SimpleNamespace(inject_observation=lambda *_: None, pause=lambda: None, resume=lambda: None),
        get_chat_agent=lambda: agent,
        handle_chat_direct=lambda cid, txt, img, task_constraint=None: calls["direct"].append(task_constraint),
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
