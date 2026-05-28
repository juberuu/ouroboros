from ouroboros.contracts.chat_id_policy import (
    A2A_CHAT_ID_MAX,
    A2A_CHAT_ID_MIN,
    WEB_UI_CHAT_ID,
    is_a2a_chat_id,
    is_internal_chat_id,
)


def test_a2a_chat_id_range_is_internal() -> None:
    assert is_a2a_chat_id(A2A_CHAT_ID_MIN)
    assert is_a2a_chat_id(A2A_CHAT_ID_MIN - 1)
    assert is_a2a_chat_id(-1001)
    assert is_a2a_chat_id(A2A_CHAT_ID_MAX)
    assert is_internal_chat_id(-1001)


def test_human_and_telegram_chat_ids_are_not_internal() -> None:
    assert not is_a2a_chat_id(WEB_UI_CHAT_ID)
    assert not is_internal_chat_id(WEB_UI_CHAT_ID)
    assert not is_a2a_chat_id(123456789)
    assert not is_internal_chat_id(123456789)


def test_invalid_chat_ids_are_not_internal() -> None:
    assert not is_a2a_chat_id(None)
    assert not is_a2a_chat_id("")
    assert not is_internal_chat_id("not-an-int")
