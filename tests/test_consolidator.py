"""Tests for ouroboros.consolidator (block-wise system)."""
import json
import pathlib
import pytest
from unittest.mock import MagicMock

from ouroboros.consolidator import (
    should_consolidate,
    consolidate,
    migrate_dialogue_summary_to_blocks,
    _load_meta,
    _save_meta,
    _count_lines,
    _read_chat_entries,
    _format_entries_for_block,
    _load_blocks,
    _save_blocks,
    BLOCK_SIZE,
)


@pytest.fixture
def tmp_paths(tmp_path):
    chat_path = tmp_path / "chat.jsonl"
    blocks_path = tmp_path / "dialogue_blocks.json"
    meta_path = tmp_path / "dialogue_meta.json"
    return chat_path, blocks_path, meta_path


def _write_chat_entries(path, count):
    """Write count fake chat entries."""
    with path.open("w") as f:
        for i in range(count):
            direction = "in" if i % 2 == 0 else "out"
            entry = {
                "ts": f"2026-02-25T{10 + i // 60:02d}:{i % 60:02d}:00Z",
                "direction": direction,
                "text": f"Message {i}",
            }
            f.write(json.dumps(entry) + "\n")


def test_should_consolidate_no_chat(tmp_paths):
    _, _, meta_path = tmp_paths
    chat_path = pathlib.Path("/nonexistent/chat.jsonl")
    assert should_consolidate(meta_path, chat_path) is False


def test_should_consolidate_not_enough_messages(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, 5)
    assert should_consolidate(meta_path, chat_path) is False


def test_should_consolidate_enough_messages(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, BLOCK_SIZE + 5)
    assert should_consolidate(meta_path, chat_path) is True


def test_should_consolidate_respects_offset(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, BLOCK_SIZE + 5)
    _save_meta(meta_path, {"last_consolidated_offset": BLOCK_SIZE + 2})
    assert should_consolidate(meta_path, chat_path) is False


def test_consolidate_creates_block(tmp_paths):
    chat_path, blocks_path, meta_path = tmp_paths
    _write_chat_entries(chat_path, BLOCK_SIZE + 5)

    mock_llm = MagicMock()
    mock_llm.chat.return_value = (
        {"content": "### Block: 2026-02-25 10:00 - 11:40\n\nSummary of events."},
        {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001},
    )

    usage = consolidate(chat_path, blocks_path, meta_path, mock_llm)

    assert usage is not None
    assert usage["cost"] == 0.001
    assert blocks_path.exists()
    blocks = json.loads(blocks_path.read_text())
    assert len(blocks) == 1
    assert blocks[0]["type"] == "summary"
    assert "Summary of events" in blocks[0]["content"]

    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == BLOCK_SIZE


def test_consolidate_not_enough_messages(tmp_paths):
    chat_path, blocks_path, meta_path = tmp_paths
    _write_chat_entries(chat_path, 5)

    mock_llm = MagicMock()
    result = consolidate(chat_path, blocks_path, meta_path, mock_llm)
    assert result is None
    mock_llm.chat.assert_not_called()


def test_load_save_meta(tmp_paths):
    _, _, meta_path = tmp_paths
    assert _load_meta(meta_path) == {}

    _save_meta(meta_path, {"last_consolidated_offset": 42})
    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == 42


def test_count_lines(tmp_paths):
    chat_path = tmp_paths[0]
    _write_chat_entries(chat_path, 15)
    assert _count_lines(chat_path) == 15


def test_should_consolidate_handles_log_rotation(tmp_paths):
    chat_path, _, meta_path = tmp_paths
    _write_chat_entries(chat_path, BLOCK_SIZE + 5)
    _save_meta(meta_path, {"last_consolidated_offset": 9999})
    assert should_consolidate(meta_path, chat_path) is True


def test_format_entries_for_block():
    entries = [
        {"ts": "2026-02-25T10:00:00Z", "direction": "in", "text": "Hello"},
        {"ts": "2026-02-25T10:01:00Z", "direction": "out", "text": "Hi there"},
    ]
    formatted = _format_entries_for_block(entries)
    assert "User: Hello" in formatted
    assert "Ouroboros: Hi there" in formatted


def test_load_save_blocks(tmp_path):
    blocks_path = tmp_path / "blocks.json"
    blocks = [{"ts": "2026-01-01", "type": "summary", "content": "test"}]
    _save_blocks(blocks_path, blocks)
    loaded = _load_blocks(blocks_path)
    assert len(loaded) == 1
    assert loaded[0]["content"] == "test"


def test_migrate_dialogue_summary(tmp_path):
    summary_path = tmp_path / "dialogue_summary.md"
    blocks_path = tmp_path / "dialogue_blocks.json"

    summary_path.write_text(
        "### Episode: 2026-02-20 10:00 – 11:00\n\nFirst episode.\n\n"
        "### Era: 2026-01-01 to 2026-01-31\n\nOld era summary.\n",
        encoding="utf-8",
    )

    migrate_dialogue_summary_to_blocks(summary_path, blocks_path)

    assert blocks_path.exists()
    blocks = json.loads(blocks_path.read_text())
    assert len(blocks) == 2
    assert blocks[0]["type"] == "summary"
    assert blocks[1]["type"] == "era"


def test_migrate_skips_if_blocks_exist(tmp_path):
    summary_path = tmp_path / "dialogue_summary.md"
    blocks_path = tmp_path / "dialogue_blocks.json"

    summary_path.write_text("### Episode: test\nContent.", encoding="utf-8")
    blocks_path.write_text("[]", encoding="utf-8")

    migrate_dialogue_summary_to_blocks(summary_path, blocks_path)
    assert json.loads(blocks_path.read_text()) == []
