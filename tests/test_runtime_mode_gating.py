"""Phase 6 regression tests for runtime_mode-aware hardcoded sandbox.

- light    : every repo-mutation tool returns LIGHT_MODE_BLOCKED.
- advanced : existing behaviour (blocked on safety-critical paths only).
- pro      : safety-critical writes allowed but annotated with a
             CORE_PATCH_NOTICE pending event.
"""
from __future__ import annotations

import pathlib
import pytest

from ouroboros.tools.registry import ToolRegistry


def _registry(tmp_path):
    return ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)


# ---------------------------------------------------------------------------
# Light mode blanket block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tool_name",
    [
        "repo_write",
        "repo_write_commit",
        "repo_commit",
        "str_replace_editor",
        "claude_code_edit",
        "revert_commit",
        "pull_from_remote",
        "restore_to_head",
        "rollback_to_target",
        "promote_to_stable",
    ],
)
def test_light_mode_blocks_repo_mutation_tools(tool_name, tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute(tool_name, {"path": "README.md"})
    assert "LIGHT_MODE_BLOCKED" in result, result[:200]


def test_light_mode_still_allows_read_only_tools(tmp_path, monkeypatch):
    """Read tools and non-repo-mutation tools are unaffected by light mode."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("repo_read", {"path": "README.md"})
    # repo_read may return a file-not-found error or similar, but
    # should NOT be the light-mode block sentinel.
    assert "LIGHT_MODE_BLOCKED" not in result


# ---------------------------------------------------------------------------
# Advanced mode: safety-critical still blocked
# ---------------------------------------------------------------------------


def test_advanced_mode_blocks_safety_critical_write(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write_commit",
        {"path": "ouroboros/safety.py", "content": "x"},
    )
    assert "SAFETY_VIOLATION" in result


def test_advanced_mode_allows_non_critical_write_calls_through(tmp_path, monkeypatch):
    """Non-critical paths fall through to the tool handler (which may then
    fail for other reasons like missing file). The sandbox specifically
    lets them through."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    # Build-up check: we don't want the safety sentinel.
    result = reg.execute(
        "repo_write_commit",
        {"path": "docs/README.md", "content": "x", "commit_message": "test"},
    )
    assert "SAFETY_VIOLATION" not in result
    assert "LIGHT_MODE_BLOCKED" not in result


# ---------------------------------------------------------------------------
# Pro mode: core edits allowed + annotated
# ---------------------------------------------------------------------------


def test_pro_mode_behaves_like_advanced_at_sandbox_gate(tmp_path, monkeypatch):
    """Phase 6 ships ``pro`` as a forward-compatible setting but does
    NOT yet relax the hardcoded safety-critical gate — the escape
    hatch requires plumbing runtime_mode through every enforcement
    layer (registry + git.py + claude_code_edit revert), which is
    deferred. Until that lands, ``pro`` behaves identically to
    ``advanced`` at the registry level."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write_commit",
        {"path": "ouroboros/safety.py", "content": "x"},
    )
    assert "SAFETY_VIOLATION" in result


def test_light_mode_blocks_runshell_mutation(tmp_path, monkeypatch):
    """Phase 6 regression: light mode pattern-matches repo-mutating
    shell commands. A ``git commit`` invocation under ``run_shell``
    in light mode must return LIGHT_MODE_BLOCKED."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": "git commit -m 'x'"})
    assert "LIGHT_MODE_BLOCKED" in result


def test_light_mode_allows_readonly_runshell(tmp_path, monkeypatch):
    """Read-only shell invocations (git status, pytest, ls) must
    still work in light mode — the filter only fires on mutation
    indicators."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    # The real handler may still fail for other reasons (no repo in
    # tmp_path), but the LIGHT_MODE_BLOCKED sentinel must not appear.
    result = reg.execute("run_shell", {"cmd": "git status"})
    assert "LIGHT_MODE_BLOCKED" not in result
