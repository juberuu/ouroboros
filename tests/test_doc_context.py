"""Tests for the low/max reference-doc layout in the always-on agent context.

Doc matrix (matrix B):
  max: self-body tasks get ARCHITECTURE full + DEVELOPMENT full; external
       headless/workspace tasks get navigation/on-demand unless explicitly
       requesting self-body docs; README/CHECKLISTS on-demand pointer.
  low: ARCHITECTURE nav-map; DEVELOPMENT full on runnable task contexts;
       pointer only when a structured caller declares no development context is needed;
       README/CHECKLISTS on-demand pointer.
SYSTEM + BIBLE are tier-0 and always full.
"""

import os
import pathlib
import re
import tempfile

# Unique sentinel placed inside the ARCHITECTURE body so we can prove the full
# body is inlined (max) vs replaced by a structure-only nav map (low).
_ARCH_BODY_SENTINEL = "ARCH_BODY_SENTINEL_XYZ"


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
    (repo_dir / "docs" / "ARCHITECTURE.md").write_text(
        "# Ouroboros v5.5.0 — Architecture\n\n## Section A\n\n"
        + _ARCH_BODY_SENTINEL
        + " alpha\n\n## Section B\n\nbeta\n",
        encoding="utf-8",
    )
    (repo_dir / "docs" / "DEVELOPMENT.md").write_text("# DEVELOPMENT.md — Dev Guide", encoding="utf-8")
    (repo_dir / "README.md").write_text('[![Version 5.5.0](https://img.shields.io/badge/version-5.5.0-green.svg)](VERSION)', encoding="utf-8")
    (repo_dir / "docs" / "CHECKLISTS.md").write_text("## Repo Commit Checklist\n| # | item |", encoding="utf-8")
    (drive_root / "state" / "state.json").write_text('{"spent_usd": 0}', encoding="utf-8")
    (drive_root / "memory" / "scratchpad.md").write_text("test scratchpad", encoding="utf-8")
    (drive_root / "memory" / "identity.md").write_text("I am Ouroboros.", encoding="utf-8")
    env = Env(repo_dir=repo_dir, drive_root=drive_root)
    memory = Memory(drive_root=drive_root, repo_dir=repo_dir)
    return env, memory


def _build_system_text(task_overrides=None, *, context_mode="max"):
    from ouroboros.context import build_llm_messages
    tmpdir = pathlib.Path(tempfile.mkdtemp())
    env, memory = _make_env_and_memory(tmpdir)
    task = {"id": "test-1", "type": "task", "text": "hello"}
    if task_overrides:
        task.update(task_overrides)
    prev = os.environ.get("OUROBOROS_CONTEXT_MODE")
    os.environ["OUROBOROS_CONTEXT_MODE"] = context_mode
    try:
        messages, _ = build_llm_messages(env=env, memory=memory, task=task)
    finally:
        if prev is None:
            os.environ.pop("OUROBOROS_CONTEXT_MODE", None)
        else:
            os.environ["OUROBOROS_CONTEXT_MODE"] = prev
    content = messages[0]["content"]
    return " ".join(block.get("text", "") for block in content if isinstance(block, dict))


def test_max_mode_inlines_architecture_and_development_in_full():
    text = _build_system_text(context_mode="max")
    assert "## ARCHITECTURE.md" in text
    assert _ARCH_BODY_SENTINEL in text  # full body inlined
    assert "navigation map" not in text
    assert "## DEVELOPMENT.md" in text


def test_max_mode_external_workspace_uses_navigation_docs_unless_self_body_requested():
    from ouroboros.contracts.task_contract import build_task_contract

    external = _build_system_text(
        {
            "workspace_root": "/tmp/example-workspace",
            "workspace_mode": "external",
            "actor_id": "cli",
            "metadata": {"source": "cli"},
        },
        context_mode="max",
    )
    assert "navigation map" in external
    assert _ARCH_BODY_SENTINEL not in external
    assert "## DEVELOPMENT.md" not in external
    assert "DEVELOPMENT.md" in external

    external_false = _build_system_text(
        {
            "workspace_root": "/tmp/example-workspace",
            "workspace_mode": "external",
            "actor_id": "cli",
            "metadata": {"source": "cli"},
            "context_requires_self_body_docs": "false",
        },
        context_mode="max",
    )
    assert "navigation map" in external_false
    assert _ARCH_BODY_SENTINEL not in external_false

    self_body = _build_system_text(
        {
            "workspace_root": "/tmp/example-workspace",
            "workspace_mode": "external",
            "actor_id": "cli",
            "metadata": {"source": "cli"},
            "context_requires_self_body_docs": True,
        },
        context_mode="max",
    )
    assert "## ARCHITECTURE.md" in self_body
    assert _ARCH_BODY_SENTINEL in self_body
    assert "## DEVELOPMENT.md" in self_body

    contract = build_task_contract({
        "id": "task-docs",
        "context_requires_self_body_docs": "true",
        "metadata": {"source": "api_task"},
    })

    assert contract["context_requires_self_body_docs"] is True

    contract_false = build_task_contract({
        "id": "task-docs-false",
        "context_requires_self_body_docs": "false",
        "metadata": {"source": "api_task"},
    })
    assert contract_false["context_requires_self_body_docs"] is False


def test_readme_and_checklists_are_on_demand_pointer_in_both_modes():
    for mode in ("max", "low"):
        text = _build_system_text(context_mode=mode)
        assert "Reference docs available on demand" in text
        # Named in the pointer (visible, never silently dropped) but not inlined.
        assert "README.md" in text
        assert "CHECKLISTS.md" in text


def test_low_mode_architecture_is_navigation_map_not_full_body():
    text = _build_system_text(context_mode="low")
    assert "navigation map" in text
    assert "Section A" in text and "Section B" in text  # headings present
    assert _ARCH_BODY_SENTINEL not in text  # full body NOT inlined in low


def test_low_mode_development_full_for_direct_chat_tasks_unless_explicitly_disabled():
    code_text = _build_system_text({"type": "task"}, context_mode="low")
    assert "## DEVELOPMENT.md" in code_text  # code / self-mod task → full

    chat_text = _build_system_text({"_is_direct_chat": True}, context_mode="low")
    assert "## DEVELOPMENT.md" in chat_text  # chat can still be code / self-mod work

    pure_chat_text = _build_system_text(
        {"_is_direct_chat": True, "context_requires_development": False},
        context_mode="low",
    )
    assert "## DEVELOPMENT.md" not in pure_chat_text
    assert "DEVELOPMENT.md" in pure_chat_text  # but named in the on-demand pointer


def test_version_regexes_match_runtime_formats():
    badge = '[![Version 5.5.0](https://img.shields.io/badge/version-5.5.0-green.svg)](VERSION)'
    assert re.search(r'version[- ](\d+\.\d+\.\d+)', badge, re.IGNORECASE)
    header = '# Ouroboros v5.5.0 — Architecture & Reference'
    assert re.search(r'# Ouroboros v(\d+\.\d+\.\d+)', header)
