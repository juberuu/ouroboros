"""Tests for context and memory overhaul behavior."""

import inspect
import json
import os
import sys
from unittest.mock import MagicMock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def test_progress_limit_50_in_context():
    from ouroboros.context import build_recent_sections
    source = inspect.getsource(build_recent_sections)
    assert "limit=50" in source


def test_recent_chat_limit_1000_in_context():
    from ouroboros.context import build_recent_sections
    source = inspect.getsource(build_recent_sections)
    assert 'read_jsonl_tail("chat.jsonl", 1000)' in source


def test_recent_sections_filter_process_logs_by_task_id(tmp_path):
    from ouroboros.context import build_recent_sections
    from ouroboros.memory import Memory

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "chat.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "progress.jsonl").write_text(
        "\n".join([
            json.dumps({"ts": "2026-03-19T16:00:00Z", "task_id": "task-a", "text": "progress-a"}),
            json.dumps({"ts": "2026-03-19T16:01:00Z", "task_id": "task-b", "text": "progress-b"}),
        ]) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "tools.jsonl").write_text(
        "\n".join([
            json.dumps({"tool": "repo_read", "task_id": "task-a", "args": {"path": "A.md"}, "result_preview": "ok"}),
            json.dumps({"tool": "repo_read", "task_id": "task-b", "args": {"path": "B.md"}, "result_preview": "ok"}),
        ]) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "events.jsonl").write_text(
        "\n".join([
            json.dumps({"type": "task_done", "task_id": "task-a"}),
            json.dumps({"type": "tool_error", "task_id": "task-b", "error": "boom"}),
        ]) + "\n",
        encoding="utf-8",
    )
    (logs_dir / "supervisor.jsonl").write_text("", encoding="utf-8")
    (logs_dir / "task_reflections.jsonl").write_text("", encoding="utf-8")

    sections = build_recent_sections(Memory(drive_root=tmp_path), env=None, task_id="task-a")
    combined = "\n\n".join(sections)

    assert "progress-a" in combined
    assert "progress-b" not in combined
    assert "path=A.md" in combined
    assert "path=B.md" not in combined
    assert "task_done: 1" in combined
    assert "tool_error: 1" not in combined


def test_should_consolidate_chat_blocks_alias(tmp_path):
    from ouroboros.consolidator import should_consolidate_chat_blocks, BLOCK_SIZE
    chat_path = tmp_path / 'chat.jsonl'
    meta_path = tmp_path / 'dialogue_meta.json'
    entries = [json.dumps({"ts": f"2026-03-09T10:{i % 60:02d}:00Z", "direction": "in", "text": "msg"}) for i in range(BLOCK_SIZE + 5)]
    chat_path.write_text("\n".join(entries) + "\n", encoding='utf-8')
    assert should_consolidate_chat_blocks(meta_path, chat_path) is True


def test_consolidate_chat_alias_creates_block(tmp_path):
    from ouroboros.consolidator import consolidate_chat_blocks, _load_meta, _load_blocks, BLOCK_SIZE
    chat_path = tmp_path / 'chat.jsonl'
    blocks_path = tmp_path / 'dialogue_blocks.json'
    meta_path = tmp_path / 'dialogue_meta.json'
    entries = [json.dumps({"ts": f"2026-03-09T10:{i % 60:02d}:00Z", "direction": "in", "text": f"msg {i}"}) for i in range(BLOCK_SIZE + 5)]
    chat_path.write_text("\n".join(entries) + "\n", encoding='utf-8')
    mock_llm = MagicMock()
    mock_llm.chat.return_value = ({"content": "### Block: test\n\nSummary."}, {"prompt_tokens": 100, "completion_tokens": 50, "cost": 0.001})
    usage = consolidate_chat_blocks(chat_path, blocks_path, meta_path, mock_llm)
    assert usage is not None
    meta = _load_meta(meta_path)
    assert meta["last_consolidated_offset"] == BLOCK_SIZE
    blocks = _load_blocks(blocks_path)
    assert len(blocks) == 1


def test_no_identity_truncation_in_consolidator_prompts():
    from ouroboros.consolidator import _create_block_summary, consolidate_scratchpad_blocks
    assert 'identity_text[:' not in inspect.getsource(_create_block_summary)
    assert 'identity_text[:' not in inspect.getsource(consolidate_scratchpad_blocks)


def test_health_invariants_come_first_in_dynamic_context(tmp_path):
    from ouroboros.context import build_llm_messages
    from ouroboros.memory import Memory

    class FakeEnv:
        def drive_path(self, p):
            return tmp_path / p

        def repo_path(self, p):
            return tmp_path / "repo" / p

        @property
        def repo_dir(self):
            return tmp_path / "repo"

        @property
        def drive_root(self):
            return tmp_path

    (tmp_path / "repo" / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "repo" / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "memory").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    (tmp_path / "repo" / "prompts" / "SYSTEM.md").write_text("System prompt", encoding="utf-8")
    (tmp_path / "repo" / "BIBLE.md").write_text("Bible", encoding="utf-8")
    (tmp_path / "repo" / "README.md").write_text("README", encoding="utf-8")
    (tmp_path / "repo" / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v1.2.3", encoding="utf-8")
    (tmp_path / "repo" / "docs" / "DEVELOPMENT.md").write_text(
        "### File Size Budgets\n| Path | Budget chars |\n|------|--------------|\n| memory/identity.md | 1000 |\n",
        encoding="utf-8",
    )
    (tmp_path / "repo" / "docs" / "CHECKLISTS.md").write_text("Checklist", encoding="utf-8")
    (tmp_path / "repo" / "VERSION").write_text("1.2.3", encoding="utf-8")
    (tmp_path / "repo" / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
    (tmp_path / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
    (tmp_path / "memory" / "identity.md").write_text("x" * 950, encoding="utf-8")
    (tmp_path / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")

    messages, _cap_info = build_llm_messages(
        env=FakeEnv(),
        memory=Memory(drive_root=tmp_path),
        task={"id": "task-a", "type": "task", "text": "hello"},
    )

    dynamic_text = messages[0]["content"][2]["text"]
    assert dynamic_text.startswith("## Health Invariants")
    assert dynamic_text.index("## Health Invariants") < dynamic_text.index("## Drive state")


def test_health_invariants_come_first_in_background_consciousness_context(tmp_path):
    from ouroboros.consciousness import BackgroundConsciousness

    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "drive"
    (repo_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (drive_root / "memory" / "knowledge").mkdir(parents=True, exist_ok=True)
    (drive_root / "logs").mkdir(parents=True, exist_ok=True)
    (drive_root / "state").mkdir(parents=True, exist_ok=True)

    (repo_dir / "prompts" / "CONSCIOUSNESS.md").write_text("Consciousness prompt", encoding="utf-8")
    (repo_dir / "BIBLE.md").write_text("Bible", encoding="utf-8")
    (repo_dir / "VERSION").write_text("1.2.3", encoding="utf-8")
    (repo_dir / "pyproject.toml").write_text('version = "1.2.3"', encoding="utf-8")
    (repo_dir / "README.md").write_text("README", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v1.2.3", encoding="utf-8")
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text(
        "### File Size Budgets\n| Path | Budget chars |\n|------|--------------|\n| memory/identity.md | 1000 |\n",
        encoding="utf-8",
    )
    (drive_root / "state" / "state.json").write_text('{"spent_usd": 0, "budget_drift_alert": false}', encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("x" * 950, encoding="utf-8")
    (drive_root / "memory" / "scratchpad.md").write_text("scratchpad", encoding="utf-8")
    (drive_root / "logs" / "chat.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "progress.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "tools.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "events.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "supervisor.jsonl").write_text("", encoding="utf-8")
    (drive_root / "logs" / "task_reflections.jsonl").write_text("", encoding="utf-8")

    bg = BackgroundConsciousness(
        drive_root=drive_root,
        repo_dir=repo_dir,
        event_queue=None,
        owner_chat_id_fn=lambda: None,
    )

    text = bg._build_context()
    assert text.index("## Health Invariants") < text.index("## Drive state")
