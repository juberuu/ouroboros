import queue

from ouroboros.loop import _drain_incoming_messages


def test_drain_incoming_messages_preserves_image_payload():
    messages = []
    incoming_messages = queue.Queue()
    incoming_messages.put({
        "text": "photo from telegram",
        "image_base64": "aW1hZ2U=",
        "image_mime": "image/png",
        "image_caption": "photo from telegram",
    })

    _drain_incoming_messages(
        messages=messages,
        incoming_messages=incoming_messages,
        drive_root=None,
        task_id="",
        event_queue=None,
        _owner_msg_seen=set(),
    )

    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "photo from telegram"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == "data:image/png;base64,aW1hZ2U="
