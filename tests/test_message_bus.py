import base64

import supervisor.message_bus as message_bus


def _make_bridge(monkeypatch, settings=None):
    monkeypatch.setattr(message_bus.LocalChatBridge, "_restart_telegram_polling", lambda self: None)
    return message_bus.LocalChatBridge(settings or {})


def test_parse_single_chat_id_valid(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    assert bridge._parse_single_chat_id("12345") == 12345


def test_parse_single_chat_id_empty(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    assert bridge._parse_single_chat_id("") == 0
    assert bridge._parse_single_chat_id("   ") == 0


def test_parse_single_chat_id_invalid(monkeypatch):
    bridge = _make_bridge(monkeypatch)
    assert bridge._parse_single_chat_id("not-a-number") == 0


def test_configure_from_settings_without_legacy_field(monkeypatch):
    """After removing TELEGRAM_ALLOWED_CHAT_IDS, configure_from_settings
    should work with only TELEGRAM_CHAT_ID."""
    bridge = _make_bridge(monkeypatch)
    bridge.configure_from_settings({
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "999",
    })
    assert bridge._telegram_chat_id == 999
    assert bridge._telegram_active_chat_id == 999


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


def test_telegram_poll_loop_enqueues_inbound_messages(monkeypatch):
    bridge = _make_bridge(monkeypatch, {"TELEGRAM_BOT_TOKEN": "token"})
    broadcasts = []
    bridge._broadcast_fn = broadcasts.append

    def fake_api(method, **kwargs):
        assert method == "getUpdates"
        bridge._telegram_stop.set()
        return {
            "ok": True,
            "result": [{
                "update_id": 10,
                "message": {
                    "text": "hi from telegram",
                    "chat": {"id": 777},
                    "from": {"id": 888, "username": "anton"},
                },
            }],
        }

    monkeypatch.setattr(bridge, "_telegram_api", fake_api)

    bridge._telegram_stop.clear()
    bridge._telegram_poll_loop()
    updates = bridge.get_updates(offset=0, timeout=1)

    assert updates[0]["message"]["chat"]["id"] == 777
    assert updates[0]["message"]["from"]["id"] == 888
    assert updates[0]["message"]["source"] == "telegram"
    assert updates[0]["message"]["sender_label"] == "Telegram (anton)"
    assert bridge._telegram_active_chat_id == 777
    assert broadcasts[0]["role"] == "user"
    assert broadcasts[0]["source"] == "telegram"


def test_telegram_poll_loop_enqueues_inbound_photo_messages(monkeypatch):
    bridge = _make_bridge(monkeypatch, {"TELEGRAM_BOT_TOKEN": "token"})
    broadcasts = []
    bridge._broadcast_fn = broadcasts.append

    def fake_api(method, **kwargs):
        assert method == "getUpdates"
        bridge._telegram_stop.set()
        return {
            "ok": True,
            "result": [{
                "update_id": 11,
                "message": {
                    "caption": "photo from telegram",
                    "chat": {"id": 777},
                    "from": {"id": 888, "username": "anton"},
                    "photo": [{"file_id": "small"}, {"file_id": "large"}],
                },
            }],
        }

    monkeypatch.setattr(bridge, "_telegram_api", fake_api)
    monkeypatch.setattr(bridge, "_telegram_download_file", lambda file_id, timeout=30: (b"img", "image/png"))

    bridge._telegram_stop.clear()
    bridge._telegram_poll_loop()
    updates = bridge.get_updates(offset=0, timeout=1)

    assert updates[0]["message"]["chat"]["id"] == 777
    assert updates[0]["message"]["text"] == "photo from telegram"
    assert updates[0]["message"]["image_base64"] == base64.b64encode(b"img").decode("ascii")
    assert updates[0]["message"]["image_mime"] == "image/png"
    assert updates[0]["message"]["image_caption"] == "photo from telegram"
    assert broadcasts[0]["type"] == "photo"
    assert broadcasts[0]["role"] == "user"
    assert broadcasts[0]["sender_label"] == "Telegram (anton)"


def test_telegram_bridge_routes_web_messages_replies_actions_and_photos(monkeypatch):
    bridge = _make_bridge(monkeypatch, {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_CHAT_ID": "555",
    })

    broadcasts = []
    bridge._broadcast_fn = broadcasts.append
    sent_text = []
    sent_actions = []
    sent_photos = []
    monkeypatch.setattr(
        bridge,
        "_send_telegram_text",
        lambda text, preferred_chat_id=0: sent_text.append((text, preferred_chat_id)),
    )
    monkeypatch.setattr(
        bridge,
        "_send_telegram_action",
        lambda action, preferred_chat_id=0: sent_actions.append((action, preferred_chat_id)),
    )
    monkeypatch.setattr(
        bridge,
        "_send_telegram_photo",
        lambda photo_bytes, caption="", mime="image/png", preferred_chat_id=0: sent_photos.append(
            (photo_bytes, caption, mime, preferred_chat_id)
        ),
    )

    bridge.ui_send("hello from web", sender_session_id="session-1", client_message_id="c-1")
    updates = bridge.get_updates(offset=0, timeout=1)
    assert updates[0]["message"]["chat"]["id"] == 555
    assert sent_text[0][1] == 555
    assert sent_text[0][0].startswith("WebUI (session-")

    bridge.send_message(555, "assistant reply", task_id="task-42")
    bridge.send_chat_action(555, "typing")
    bridge.send_photo(555, b"img", caption="caption")

    assert sent_text[1] == ("assistant reply", 555)
    assert broadcasts[1]["task_id"] == "task-42"
    assert sent_actions == [("typing", 555)]
    assert sent_photos == [(b"img", "caption", "image/png", 555)]
    photo_broadcast = next(item for item in broadcasts if item.get("type") == "photo")
    assert photo_broadcast["ts"].endswith("+00:00")
