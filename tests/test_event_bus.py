from ouroboros.event_bus import CHAT_OUTBOUND, EventBus
import asyncio


def test_event_bus_publishes_to_matching_topic() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", CHAT_OUTBOUND, received.append)
    bus.publish(CHAT_OUTBOUND, {"text": "hello"})

    assert received == [{"text": "hello", "topic": CHAT_OUTBOUND}]


def test_event_bus_unsubscribe_skill_removes_handlers() -> None:
    bus = EventBus()
    received = []

    bus.subscribe("skill", CHAT_OUTBOUND, received.append)
    bus.unsubscribe_skill("skill")
    bus.publish(CHAT_OUTBOUND, {"text": "hello"})

    assert received == []


def test_event_bus_rejects_unknown_topic() -> None:
    bus = EventBus()

    try:
        bus.subscribe("skill", "unknown.topic", lambda _payload: None)
    except ValueError as exc:
        assert "unsupported event topic" in str(exc)
    else:
        raise AssertionError("expected unknown topic to be rejected")


def test_event_bus_schedules_async_handler_from_sync_publish() -> None:
    async def main():
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())
        received = []

        async def handler(payload):
            received.append(payload["text"])

        bus.subscribe("skill", CHAT_OUTBOUND, handler)
        bus.publish(CHAT_OUTBOUND, {"text": "hello"})
        await asyncio.sleep(0.05)
        assert received == ["hello"]

    asyncio.run(main())
