"""Regression tests for runtime_mode-aware repository protections.

- light    : every repo-mutation tool returns LIGHT_MODE_BLOCKED.
- advanced : evolutionary-layer self-modification is allowed, but protected
             core/contract/release paths are blocked.
- pro      : protected writes are allowed on disk with CORE_PATCH_NOTICE, and
             repo_commit must pass the extra core-patch review gate.
"""
from __future__ import annotations

import pathlib
import subprocess
from types import SimpleNamespace

import pytest

from ouroboros.runtime_mode_policy import ProtectedPath
from ouroboros.runtime_mode_policy import protected_path_category
from ouroboros.tools.registry import ToolRegistry
from ouroboros.tools.registry import ToolEntry


def _registry(tmp_path):
    return ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)


class _CommitCtx:
    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self.repo_dir = repo_dir
        self.drive_root = drive_root
        self.task_id = "runtime-mode-test"
        self._review_advisory = []
        self._last_triad_models = []
        self._last_scope_model = ""
        self._last_triad_raw_results = []
        self._last_scope_raw_result = {}
        self._review_degraded_reasons = []
        self._current_review_tool_name = "repo_commit"
        self._scope_review_history = {}
        self._review_history = []

    def emit_progress_fn(self, *_args, **_kwargs):
        return None

    def drive_logs(self):
        path = pathlib.Path(self.drive_root) / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path


def _git_repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    (repo / "BIBLE.md").write_text("constitution\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


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
# Advanced mode: protected core/contract/release surfaces are blocked
# ---------------------------------------------------------------------------


def test_advanced_mode_blocks_safety_critical_write(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write_commit",
        {"path": "ouroboros/safety.py", "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" in result


def test_advanced_mode_blocks_frozen_contract_write(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": "ouroboros/contracts/plugin_api.py", "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" in result


def test_advanced_mode_blocks_runtime_policy_guardrail_write(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": "ouroboros/runtime_mode_policy.py", "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" in result


def test_dot_github_workflow_is_release_invariant():
    assert protected_path_category(".github/workflows/ci.yml") == "release-invariant"
    assert protected_path_category("./.github/workflows/ci.yml") == "release-invariant"


def test_advanced_mode_blocks_release_invariant_write(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": ".github/workflows/ci.yml", "content": "name: nope\n"},
    )
    assert "CORE_PROTECTION_BLOCKED" in result


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
    assert "CORE_PROTECTION_BLOCKED" not in result
    assert "LIGHT_MODE_BLOCKED" not in result


# ---------------------------------------------------------------------------
# Pro mode: protected edits are allowed on disk + annotated
# ---------------------------------------------------------------------------


def test_pro_mode_allows_protected_write_with_core_patch_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    reg = _registry(tmp_path)
    result = reg.execute(
        "repo_write",
        {"path": "ouroboros/safety.py", "content": "x"},
    )
    assert "CORE_PROTECTION_BLOCKED" not in result
    assert "CORE_PATCH_NOTICE" in result


def test_pro_mode_claude_code_edit_emits_core_patch_notice(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    (repo / "ouroboros" / "contracts").mkdir(parents=True)
    (repo / "ouroboros" / "contracts" / "plugin_api.py").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "contracts"], cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path)

    def fake_edit(ctx, **_kwargs):
        (pathlib.Path(ctx.repo_dir) / "ouroboros" / "contracts" / "plugin_api.py").write_text(
            "new\n",
            encoding="utf-8",
        )
        return "edited"

    reg._entries["claude_code_edit"] = ToolEntry(
        name="claude_code_edit",
        schema={"name": "claude_code_edit"},
        handler=fake_edit,
    )

    result = reg.execute("claude_code_edit", {"prompt": "edit protected"})

    assert "edited" in result
    assert "CORE_PATCH_NOTICE" in result
    assert "ouroboros/contracts/plugin_api.py" in result


def test_advanced_commit_blocks_protected_staged_paths(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    (repo / "BIBLE.md").write_text("changed\n", encoding="utf-8")
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")

    result = git_mod._run_reviewed_stage_cycle(
        ctx,
        "test protected commit",
        0.0,
        paths=["BIBLE.md"],
        skip_advisory_pre_review=True,
    )

    assert result["status"] == "blocked"
    assert result["block_reason"] == "core_protection_blocked"
    assert "CORE_PROTECTION_BLOCKED" in result["message"]


def test_advanced_commit_blocks_rename_from_protected_path(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    subprocess.run(["git", "mv", "BIBLE.md", "BIBLE2.md"], cwd=repo, check=True)
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")

    result = git_mod._run_reviewed_stage_cycle(
        ctx,
        "rename protected file",
        0.0,
        skip_advisory_pre_review=True,
    )

    assert result["status"] == "blocked"
    assert result["block_reason"] == "core_protection_blocked"
    assert "BIBLE.md" in result["message"]


def test_pro_commit_runs_core_patch_gate_for_protected_paths(tmp_path, monkeypatch):
    from ouroboros.tools import git as git_mod

    repo = _git_repo(tmp_path)
    (repo / "BIBLE.md").write_text("changed\n", encoding="utf-8")
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "pro")
    monkeypatch.setenv("OUROBOROS_PRE_PUSH_TESTS", "0")

    called: list[list[str]] = []

    def fake_core_gate(_ctx, _message, _start, *, protected_paths, **_kwargs):
        called.append([p.path for p in protected_paths])
        return None

    monkeypatch.setattr(git_mod, "run_core_patch_review_gate", fake_core_gate)
    monkeypatch.setattr(git_mod, "_run_parallel_review", lambda *a, **k: (None, None, "", []))
    monkeypatch.setattr(git_mod, "_aggregate_review_verdict", lambda *a, **k: (False, None, "", [], []))

    result = git_mod._run_reviewed_stage_cycle(
        ctx,
        "test protected commit",
        0.0,
        paths=["BIBLE.md"],
        skip_advisory_pre_review=True,
    )

    assert result["status"] == "passed"
    assert called == [["BIBLE.md"]]


def test_core_patch_gate_blocks_when_scope_review_is_budget_skipped(tmp_path, monkeypatch):
    from ouroboros.tools import core_patch_gate

    repo = _git_repo(tmp_path)
    ctx = _CommitCtx(repo, tmp_path / "drive")
    recorded = []

    monkeypatch.setattr(
        core_patch_gate,
        "_run_parallel_review",
        lambda *a, **k: (
            None,
            SimpleNamespace(
                blocked=False,
                status="budget_exceeded",
                critical_findings=[],
                advisory_findings=[],
                block_message="",
            ),
            "",
            [],
        ),
    )
    monkeypatch.setattr(
        core_patch_gate,
        "_aggregate_review_verdict",
        lambda *a, **k: (False, None, "", [], []),
    )
    monkeypatch.setattr(
        core_patch_gate,
        "_record_commit_attempt",
        lambda *args, **kwargs: recorded.append((args, kwargs)),
    )

    result = core_patch_gate.run_core_patch_review_gate(
        ctx,
        "core patch",
        0.0,
        protected_paths=[ProtectedPath("BIBLE.md", "safety-critical")],
        pre_fingerprint={"fingerprint": "abc", "ok": True},
    )

    assert result is not None
    assert result["status"] == "blocked"
    assert result["block_reason"] == "core_patch_review_blocked"
    assert "did not complete" in result["message"]
    assert recorded


def test_core_patch_gate_allows_responded_pass(tmp_path, monkeypatch):
    from ouroboros.tools import core_patch_gate

    repo = _git_repo(tmp_path)
    ctx = _CommitCtx(repo, tmp_path / "drive")
    monkeypatch.setattr(
        core_patch_gate,
        "_run_parallel_review",
        lambda *a, **k: (
            None,
            SimpleNamespace(
                blocked=False,
                status="responded",
                critical_findings=[],
                advisory_findings=[],
                block_message="",
            ),
            "",
            [],
        ),
    )
    monkeypatch.setattr(
        core_patch_gate,
        "_aggregate_review_verdict",
        lambda *a, **k: (False, None, "", [], []),
    )

    result = core_patch_gate.run_core_patch_review_gate(
        ctx,
        "core patch",
        0.0,
        protected_paths=[ProtectedPath("BIBLE.md", "safety-critical")],
        pre_fingerprint={"fingerprint": "abc", "ok": True},
    )

    assert result is None


def test_light_mode_blocks_runshell_mutation(tmp_path, monkeypatch):
    """Phase 6 regression: light mode pattern-matches repo-mutating
    shell commands. A ``git commit`` invocation under ``run_shell``
    in light mode must return LIGHT_MODE_BLOCKED."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": "git commit -m 'x'"})
    assert "LIGHT_MODE_BLOCKED" in result


def test_advanced_mode_blocks_runshell_protected_python_writer(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "run_shell",
        {"cmd": "python -c \"from pathlib import Path; Path('BIBLE.md').write_text('x')\""},
    )
    assert "SAFETY_VIOLATION" in result
    assert "BIBLE.md" in result


def test_advanced_mode_blocks_runshell_protected_backslash_path(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    reg = _registry(tmp_path)
    result = reg.execute(
        "run_shell",
        {"cmd": "python -c \"open('ouroboros\\\\contracts\\\\plugin_api.py','w').write('x')\""},
    )
    assert "SAFETY_VIOLATION" in result


def test_light_mode_blocks_extension_tool_dispatch(tmp_path, monkeypatch):
    from ouroboros import extension_loader

    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    with extension_loader._lock:
        extension_loader._tools["ext.testskill.echo"] = {
            "name": "ext.testskill.echo",
            "handler": lambda ctx, **kwargs: "should not run",
            "description": "echo",
            "schema": {},
            "timeout_sec": 10,
            "skill": "testskill",
        }
    monkeypatch.setattr(extension_loader, "is_extension_live", lambda *_a, **_k: True)
    unloaded: list[str] = []
    monkeypatch.setattr(extension_loader, "unload_extension", unloaded.append)
    try:
        result = reg.execute("ext.testskill.echo", {})
        assert "LIGHT_MODE_BLOCKED" in result
        assert unloaded == ["testskill"]
    finally:
        with extension_loader._lock:
            extension_loader._tools.pop("ext.testskill.echo", None)


@pytest.mark.parametrize(
    "bad_cmd",
    [
        "sed -i 's/foo/bar/' docs/README.md",
        "perl -i -pe 's/foo/bar/' docs/README.md",
        "truncate -s 0 docs/README.md",
        "chmod 755 docs/README.md",
        "chown anton docs/README.md",
        "ln -s /tmp/x docs/link",
    ],
)
def test_light_mode_blocks_inplace_mutation_tools(bad_cmd, tmp_path, monkeypatch):
    """Final-review regression: the light-mode shell filter must cover
    in-place file-mutating Unix tools (``sed -i``, ``chmod``, …)
    alongside redirections."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_shell", {"cmd": bad_cmd})
    assert "LIGHT_MODE_BLOCKED" in result, f"cmd={bad_cmd!r}: {result[:200]}"


@pytest.mark.parametrize(
    "tool_name",
    [
        "fetch_pr_ref",
        "create_integration_branch",
        "cherry_pick_pr_commits",
        "stage_adaptations",
        "stage_pr_merge",
    ],
)
def test_light_mode_blocks_pr_integration_tools(tool_name, tmp_path, monkeypatch):
    """Final-review regression: PR integration tools mutate refs + the
    working tree and must be covered by the light-mode blanket block."""
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute(tool_name, {})
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
