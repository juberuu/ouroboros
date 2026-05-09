from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_prompts_do_not_infer_current_human_from_authors():
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    consciousness = (REPO_ROOT / "prompts" / "CONSCIOUSNESS.md").read_text(encoding="utf-8")
    memory = (REPO_ROOT / "ouroboros" / "memory.py").read_text(encoding="utf-8")

    assert "my human" in system
    assert "I do not know their name" in system
    assert "README, BIBLE, git history, or author" in system
    assert "Messages From My Human" in consciousness
    assert "I do not yet know my human's name or profile" in memory
    assert "Anton" not in system
    assert "Razzhigaev" not in system


def test_live_task_message_marker_uses_my_human_wording():
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    loop = (REPO_ROOT / "ouroboros" / "loop.py").read_text(encoding="utf-8")
    tools = (REPO_ROOT / "ouroboros" / "tools" / "core.py").read_text(encoding="utf-8")

    assert "[Message from my human]" in system
    assert "[Message from my human]: {dmsg}" in loop
    assert "[Message from my human]" in tools
    assert "[Owner message during task]" not in system
    assert "[Owner message during task]" not in loop


def test_public_publishing_still_requires_creator_permission():
    system = (REPO_ROOT / "prompts" / "SYSTEM.md").read_text(encoding="utf-8")
    assert "Do not publish or make content publicly accessible" in system
    assert "permission from the creator" in system
    assert "My human may grant that permission only if they" in system
