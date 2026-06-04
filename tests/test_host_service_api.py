import pathlib

from starlette.testclient import TestClient

from ouroboros.gateway.host_service import AUTH_TOKEN_FILENAME, create_host_service_app
from ouroboros.event_bus import CHAT_OUTBOUND, publish_event
from ouroboros.skill_loader import compute_content_hash, save_enabled, save_review_state, save_skill_grants, SkillReviewState
from ouroboros.utils import atomic_write_json


class FakeBridge:
    def __init__(self) -> None:
        self.messages = []
        self._subs = {}

    def enqueue_local_message(self, text, **kwargs):
        self.messages.append({"text": text, **kwargs})
        for callback in list(self._subs.values()):
            callback("reply from host")

    def subscribe_response(self, chat_id, callback):
        self._subs["sub"] = callback
        return "sub"

    def unsubscribe_response(self, subscription_id):
        self._subs.pop(subscription_id, None)


def _seed_token(
    data_dir: pathlib.Path,
    skill: str = "test_skill",
    token: str = "token",
    permissions=None,
    review_status: str = "pass",
    subscribe_events=None,
    manifest_permissions=None,
) -> None:
    topics = list(subscribe_events or ["chat.outbound"])
    manifest_perms = list(manifest_permissions or ["inject_chat", "subscribe_event"])
    skill_dir = data_dir / "skills" / "external" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"""---
name: {skill}
description: test skill
version: 0.1
type: extension
entry: plugin.py
permissions: [{", ".join(manifest_perms)}]
subscribe_events: [{", ".join(topics)}]
---
# Test
""",
        encoding="utf-8",
    )
    (skill_dir / "plugin.py").write_text("def register(api): pass\n", encoding="utf-8")
    content_hash = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    save_review_state(
        data_dir,
        skill,
        SkillReviewState(status=review_status, content_hash=content_hash),
    )
    save_enabled(data_dir, skill, True)
    save_skill_grants(
        data_dir,
        skill,
        [],
        content_hash=content_hash,
        requested_keys=[],
        granted_permissions=list(permissions or []),
        requested_permissions=["inject_chat", *(f"subscribe_event:{topic}" for topic in topics if topic != "skill.lifecycle")],
    )
    atomic_write_json(
        data_dir / "state" / "skills" / skill / AUTH_TOKEN_FILENAME,
        {
            "token": token,
            "content_hash": content_hash,
            "issued_at": "now",
        },
    )


def test_ui_ws_message_relays_namespaced_event_from_token_identity(tmp_path: pathlib.Path) -> None:
    from ouroboros.extension_loader import extension_surface_name

    _seed_token(tmp_path, skill="wsskill", token="tok", manifest_permissions=["ws_handler"])
    sent: list[dict] = []
    app = create_host_service_app(tmp_path, ws_broadcaster_getter=lambda: sent.append)
    client = TestClient(app)

    # Spoofed body skill/type must be ignored — identity/namespace come from the token.
    resp = client.post(
        "/ui/ws-message",
        headers={"X-Skill-Token": "tok"},
        json={"message_type": "progress", "data": {"pct": 5}, "skill": "evil", "type": "evil"},
    )
    assert resp.status_code == 202
    assert len(sent) == 1
    assert sent[0]["skill"] == "wsskill"
    assert sent[0]["type"] == extension_surface_name("wsskill", "progress")
    assert sent[0]["data"] == {"pct": 5}


def test_ui_ws_message_requires_ws_handler_manifest_permission(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="nows", token="tok", manifest_permissions=["inject_chat"])
    sent: list[dict] = []
    app = create_host_service_app(tmp_path, ws_broadcaster_getter=lambda: sent.append)
    client = TestClient(app)

    resp = client.post("/ui/ws-message", headers={"X-Skill-Token": "tok"}, json={"message_type": "progress", "data": {}})
    assert resp.status_code == 403
    assert sent == []


def test_ui_ws_message_rejects_missing_or_bad_token(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="wsskill", token="tok", manifest_permissions=["ws_handler"])
    app = create_host_service_app(tmp_path, ws_broadcaster_getter=lambda: (lambda m: None))
    client = TestClient(app)

    assert client.post("/ui/ws-message", json={"message_type": "progress"}).status_code == 403
    assert client.post("/ui/ws-message", headers={"X-Skill-Token": "wrong"}, json={"message_type": "progress"}).status_code == 403


def test_identity_requires_skill_token(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, permissions=["inject_chat"])
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    assert client.get("/identity").status_code == 403
    assert client.get("/identity", headers={"X-Skill-Token": "token"}).status_code == 200


def test_identity_accepts_advisory_pass_review(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, permissions=["inject_chat"], review_status="advisory_pass")
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    assert client.get("/identity", headers={"X-Skill-Token": "token"}).status_code == 200


def test_chat_inject_allows_slash_commands_from_reviewed_skill(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": " /panic", "chat_id": 1},
    )

    assert response.status_code == 202
    assert bridge.messages[0]["text"] == " /panic"


def test_chat_inject_tags_skill_source(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="telegram_bridge", token="token", permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "hello", "chat_id": 1234, "sender_label": "Telegram"},
    )

    assert response.status_code == 202
    assert bridge.messages[0]["source"] == "skill:telegram_bridge"
    assert bridge.messages[0]["chat_id"] == 1234


def test_chat_inject_preserves_transport_metadata(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="transport_bridge", token="token", permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={
            "text": "hello",
            "chat_id": 1234,
            "transport": {"kind": "messenger", "conversation_id": "abc", "sender_label": "Messenger"},
        },
    )

    assert response.status_code == 202
    assert bridge.messages[0]["transport"] == {"kind": "messenger", "conversation_id": "abc", "sender_label": "Messenger"}


def test_chat_inject_defaults_missing_ids_to_non_owner_sentinel(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="transport_bridge", token="token", permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "/panic"},
    )

    assert response.status_code == 202
    assert bridge.messages[0]["chat_id"] == 0
    assert bridge.messages[0]["user_id"] == 0


def test_chat_inject_wait_for_response_unsubscribes(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="waiter", token="token", permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "hello", "chat_id": 1234, "wait_for_response": True, "timeout_sec": 5},
    )

    assert response.status_code == 200
    assert response.json()["response"] == "reply from host"
    assert bridge._subs == {}


def test_allocate_internal_chat_ids_are_distinct(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="a2a", token="token", permissions=["inject_chat"])
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    first = client.post(
        "/chat/allocate-internal",
        headers={"X-Skill-Token": "token"},
        json={"range_name": "a2a"},
    ).json()["chat_id"]
    second = client.post(
        "/chat/allocate-internal",
        headers={"X-Skill-Token": "token"},
        json={"range_name": "a2a"},
    ).json()["chat_id"]

    assert first < 0
    assert second < 0
    assert second != first


def test_chat_inject_requires_permission_grant(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="unprivileged", token="token", permissions=[])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "hello", "chat_id": 1},
    )

    assert response.status_code == 403
    assert bridge.messages == []


def test_chat_inject_rejects_disabled_skill_token(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="disabled", token="token", permissions=["inject_chat"])
    save_enabled(tmp_path, "disabled", False)
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "hello", "chat_id": 1},
    )

    assert response.status_code == 403


def test_chat_inject_rejects_failed_review_token(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    _seed_token(tmp_path, skill="failed", token="token", permissions=["inject_chat"], review_status="blockers")
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "hello", "chat_id": 1},
    )

    assert response.status_code == 403


def test_identity_rejects_failed_review_token(tmp_path: pathlib.Path, monkeypatch) -> None:
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    _seed_token(tmp_path, skill="failed", token="token", permissions=["inject_chat"], review_status="blockers")
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    response = client.get("/identity", headers={"X-Skill-Token": "token"})

    assert response.status_code == 403


def test_events_websocket_receives_granted_topic(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="listener", token="token", permissions=["subscribe_event:chat.outbound"])
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    with client.websocket_connect("/events", headers={"X-Skill-Token": "token"}) as ws:
        ws.send_json({"type": "subscribe", "topic": CHAT_OUTBOUND})
        assert ws.receive_json()["type"] == "subscribed"
        publish_event(CHAT_OUTBOUND, {"text": "hello"})
        message = ws.receive_json()

    assert message["type"] == "event"
    assert message["topic"] == CHAT_OUTBOUND
    assert message["data"]["text"] == "hello"


def test_events_websocket_allows_manifest_declared_skill_lifecycle_without_grant(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="listener", token="token", permissions=[], subscribe_events=["skill.lifecycle"])
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    with client.websocket_connect("/events", headers={"X-Skill-Token": "token"}) as ws:
        ws.send_json({"type": "subscribe", "topic": "skill.lifecycle"})
        assert ws.receive_json()["type"] == "subscribed"


def test_events_websocket_rejects_ungranted_topic(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, skill="listener", token="token", permissions=[])
    app = create_host_service_app(tmp_path, bridge_getter=FakeBridge)
    client = TestClient(app)

    with client.websocket_connect("/events", headers={"X-Skill-Token": "token"}) as ws:
        ws.send_json({"type": "subscribe", "topic": CHAT_OUTBOUND})
        message = ws.receive_json()

    assert message["type"] == "error"
    assert "lacks grant" in message["error"]


def test_chat_inject_allows_slash_command_caption(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "", "image_caption": "/panic", "chat_id": 1},
    )

    assert response.status_code == 202
    assert bridge.messages[0]["image_caption"] == "/panic"


def test_chat_inject_allows_slash_command_caption_even_with_text(tmp_path: pathlib.Path) -> None:
    _seed_token(tmp_path, permissions=["inject_chat"])
    bridge = FakeBridge()
    app = create_host_service_app(tmp_path, bridge_getter=lambda: bridge)
    client = TestClient(app)

    response = client.post(
        "/chat/inject",
        headers={"X-Skill-Token": "token"},
        json={"text": "photo", "image_caption": "/panic", "chat_id": 1},
    )

    assert response.status_code == 202
    assert bridge.messages[0]["text"] == "photo"
    assert bridge.messages[0]["image_caption"] == "/panic"
