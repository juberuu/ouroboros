"""Tests for documentation context loading invariants."""

import pathlib
import re
import tempfile


def _make_env_and_memory(tmpdir: pathlib.Path):
    from ouroboros.agent import Env
    from ouroboros.memory import Memory

    repo_dir = tmpdir / "repo"
    drive_root = tmpdir / "drive"
    repo_dir.mkdir(parents=True, exist_ok=True)
    drive_root.mkdir(parents=True, exist_ok=True)
    for subdir in ["state", "memory", "memory/knowledge", "logs"]:
        (drive_root / subdir).mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts").mkdir(parents=True, exist_ok=True)
    (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
    (repo_dir / "prompts" / "SYSTEM.md").write_text("You are Ouroboros.", encoding="utf-8")
    (repo_dir / "BIBLE.md").write_text("# Principle 0: Agency", encoding="utf-8")
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text("# Ouroboros v5.5.0 — Architecture", encoding="utf-8")
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text("# DEVELOPMENT.md — Dev Guide", encoding="utf-8")
    (repo_dir / "README.md").write_text('[![Version 5.5.0](https://img.shields.io/badge/version-5.5.0-green.svg)](VERSION)', encoding="utf-8")
    (repo_dir / "docs" / "CHECKLISTS.md").write_text("## Repo Commit Checklist\n| # | item |", encoding="utf-8")
    (drive_root / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (drive_root / "memory" / "scratchpad.md").write_text("test scratchpad", encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("I am Ouroboros.", encoding="utf-8")
    env = Env(repo_dir=repo_dir, drive_root=drive_root)
    memory = Memory(drive_root=drive_root, repo_dir=repo_dir)
    return env, memory


def _build_system_text(task_overrides=None):
    from ouroboros.context import build_llm_messages
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env, memory = _make_env_and_memory(tmpdir)
    task = {"id": "test-1", "type": "task", "text": "hello"}
    if task_overrides:
        task.update(task_overrides)
    messages, _ = build_llm_messages(env=env, memory=memory, task=task)
    content = messages[0]["content"]
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def test_direct_chat_includes_development_readme_and_checklists():
    text = _build_system_text({"_is_direct_chat": True})
    assert "DEVELOPMENT.md" in text
    assert "README.md" in text
    assert "CHECKLISTS.md" in text


def test_regular_and_evolution_tasks_include_all_docs():
    assert "DEVELOPMENT.md" in _build_system_text({"type": "task"})
    assert "DEVELOPMENT.md" in _build_system_text({"type": "evolution"})


def test_version_regexes_match_runtime_formats():
    badge = '[![Version 5.5.0](https://img.shields.io/badge/version-5.5.0-green.svg)](VERSION)'
    assert re.search(r'version[- ](\d+\.\d+\.\d+)', badge, re.IGNORECASE)
    header = '# Ouroboros v5.5.0 — Architecture & Reference'
    assert re.search(r'# Ouroboros v(\d+\.\d+\.\d+)', header)
