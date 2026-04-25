"""Tests for git safety tools, commit gate hardening, and operational polish.

Verifies (Phase 4):
- New tools registered: pull_from_remote, restore_to_head, revert_commit
- SAFETY_CRITICAL_PATHS blocks dangerous operations
- Confirm gates prevent accidental destructive actions
- also_stage parameter in repo_write_commit
- Auto-tagging on version bump
- Credential helper in git_ops (no token in remote URL)
- New tools in CORE_TOOL_NAMES

Verifies (Phase 5):
- Auto-push wired into commit functions
- migrate_remote_credentials exists and is safe
- ARCHITECTURE.md version sync in startup checks
"""
import importlib
import inspect
import json
import os
import sys
import types

import pytest


def _ensure_sdk_mock():
    """Install a lightweight mock of claude_agent_sdk only when the package is truly absent.

    Uses importlib.util.find_spec to check real availability, not sys.modules presence,
    so an installed but not-yet-imported SDK is never masked.
    Required so gateway tests can run without the SDK installed.
    """
    import importlib.util as _ilu
    try:
        spec = _ilu.find_spec("claude_agent_sdk")
        sdk_available = spec is not None
    except (ValueError, ModuleNotFoundError):
        # find_spec raises ValueError when an already-injected mock module has __spec__=None
        sdk_available = "claude_agent_sdk" in sys.modules
    if not sdk_available:
        mock_sdk = types.ModuleType("claude_agent_sdk")
        mock_sdk.ClaudeAgentOptions = type("ClaudeAgentOptions", (), {})
        mock_sdk.ClaudeSDKClient = type("ClaudeSDKClient", (), {})
        mock_sdk.HookMatcher = type("HookMatcher", (), {"__init__": lambda self, **kw: None})
        mock_sdk.AssistantMessage = type("AssistantMessage", (), {})
        mock_sdk.ResultMessage = type("ResultMessage", (), {})
        mock_sdk.query = lambda **kw: None
        sys.modules["claude_agent_sdk"] = mock_sdk


_ensure_sdk_mock()

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_git_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.tools.git")


def _get_registry_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.tools.registry")


def _get_git_ops_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("supervisor.git_ops")


# --- Tool registration tests ---

def test_pull_from_remote_registered():
    git_mod = _get_git_module()
    names = [t.name for t in git_mod.get_tools()]
    assert "pull_from_remote" in names


def test_restore_to_head_registered():
    git_mod = _get_git_module()
    names = [t.name for t in git_mod.get_tools()]
    assert "restore_to_head" in names


def test_revert_commit_registered():
    git_mod = _get_git_module()
    names = [t.name for t in git_mod.get_tools()]
    assert "revert_commit" in names


def test_non_committing_review_cycle_exists_and_reuses_shared_stage_cycle():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._run_non_committing_review_cycle)
    assert "_run_reviewed_stage_cycle" in source
    assert '"reviewed"' in source
    assert '"review_only"' in source
    assert '["git", "reset", "HEAD"]' in source
    assert '["git", "commit"' not in source


def test_non_committing_review_cycle_runtime_unstages_on_success(monkeypatch):
    git_mod = _get_git_module()
    reset_calls = []
    recorded = []
    released = []

    monkeypatch.setattr(git_mod, "_check_overlapping_review_attempt", lambda ctx: None)
    monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: "lock-token")
    monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: released.append(lock))
    monkeypatch.setattr(
        git_mod,
        "_run_reviewed_stage_cycle",
        lambda *args, **kwargs: {
            "status": "passed",
            "message": "stage cycle passed",
            "pre_fingerprint": {"fingerprint": "pre"},
            "post_fingerprint": {"fingerprint": "post"},
        },
    )
    monkeypatch.setattr(
        git_mod,
        "_record_commit_attempt",
        lambda *args, **kwargs: recorded.append(
            {"status": args[2], "phase": kwargs.get("phase")}
        ),
    )
    monkeypatch.setattr(
        git_mod,
        "run_cmd",
        lambda cmd, cwd=None: reset_calls.append((tuple(cmd), cwd)) or "",
    )

    ctx = types.SimpleNamespace(repo_dir="/tmp/repo")
    outcome = git_mod._run_non_committing_review_cycle(ctx, "test commit")

    assert outcome["status"] == "passed"
    assert "Commit was not created" in outcome["message"]
    assert ctx._scope_review_history == {}
    assert recorded == [{"status": "reviewed", "phase": "review_only"}]
    assert released == ["lock-token"]
    assert reset_calls == [(("git", "reset", "HEAD"), "/tmp/repo")]


def test_non_committing_review_cycle_runtime_unstages_on_block(monkeypatch):
    git_mod = _get_git_module()
    reset_calls = []
    released = []

    monkeypatch.setattr(git_mod, "_check_overlapping_review_attempt", lambda ctx: None)
    monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda ctx: "lock-token")
    monkeypatch.setattr(git_mod, "_release_git_lock", lambda lock: released.append(lock))
    monkeypatch.setattr(
        git_mod,
        "_run_reviewed_stage_cycle",
        lambda *args, **kwargs: {
            "status": "blocked",
            "message": "review blocked",
            "block_reason": "critical_findings",
        },
    )
    monkeypatch.setattr(
        git_mod,
        "run_cmd",
        lambda cmd, cwd=None: reset_calls.append((tuple(cmd), cwd)) or "",
    )

    ctx = types.SimpleNamespace(repo_dir="/tmp/repo")
    outcome = git_mod._run_non_committing_review_cycle(ctx, "test commit")

    assert outcome["status"] == "blocked"
    assert outcome["block_reason"] == "critical_findings"
    assert released == ["lock-token"]
    assert reset_calls == [(("git", "reset", "HEAD"), "/tmp/repo")]


def test_repo_commit_push_uses_shared_reviewed_stage_cycle():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._repo_commit_push)
    assert "_run_reviewed_stage_cycle" in source


def test_repo_write_commit_uses_shared_reviewed_stage_cycle():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._repo_write_commit)
    assert "_run_reviewed_stage_cycle" in source


# --- Protected-path checks ---

def test_restore_to_head_blocks_protected_paths():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._restore_to_head)
    assert "is_protected_runtime_path" in source or "protected_paths_in" in source
    assert "RESTORE_BLOCKED" in source


def test_revert_commit_blocks_protected_paths():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "protected_paths_in" in source
    assert "REVERT_BLOCKED" in source


# --- Confirm gates ---

def test_revert_commit_has_confirm_gate():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "confirm" in source
    assert "Call again with confirm=true" in source


def test_restore_to_head_has_confirm_gate():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._restore_to_head)
    assert "confirm" in source
    assert "Call again with confirm=true" in source


# --- also_stage ---

def test_also_stage_in_repo_write_commit():
    git_mod = _get_git_module()
    sig = inspect.signature(git_mod._repo_write_commit)
    assert "also_stage" in sig.parameters


def test_also_stage_in_schema():
    git_mod = _get_git_module()
    tools = git_mod.get_tools()
    rwc = next(t for t in tools if t.name == "repo_write_commit")
    props = rwc.schema["parameters"]["properties"]
    assert "also_stage" in props
    assert props["also_stage"]["type"] == "array"


# --- Auto-tagging ---

def test_auto_tag_function_exists():
    git_mod = _get_git_module()
    assert hasattr(git_mod, "_auto_tag_on_version_bump")
    assert callable(git_mod._auto_tag_on_version_bump)


def test_auto_tag_called_in_commit_functions():
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        assert "_auto_tag_on_version_bump" in source, (
            f"{fn_name} must call _auto_tag_on_version_bump"
        )


def test_auto_tag_not_gated_by_test_warnings():
    """Auto-tagging must run unconditionally — not skipped when tests fail."""
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        # Find the line(s) that call _auto_tag_on_version_bump
        for line in source.splitlines():
            if "_auto_tag_on_version_bump" in line:
                assert "if not test_warning" not in line, (
                    f"{fn_name}: _auto_tag_on_version_bump must not be gated "
                    f"by test_warning_ref — tags must always be created on VERSION bump"
                )


# --- Credential helper ---

def test_credential_helper_exists():
    git_ops = _get_git_ops_module()
    assert hasattr(git_ops, "_configure_credential_helper")
    assert callable(git_ops._configure_credential_helper)


def test_configure_remote_uses_clean_url():
    """configure_remote must not embed token in the remote URL."""
    git_ops = _get_git_ops_module()
    source = inspect.getsource(git_ops.configure_remote)
    assert "x-access-token" not in source, (
        "configure_remote must use credential helper, not embed token in URL"
    )
    assert "_configure_credential_helper" in source


# --- CORE_TOOL_NAMES ---

def test_new_tools_in_core_tool_names():
    registry = _get_registry_module()
    for name in ("pull_from_remote", "restore_to_head", "revert_commit"):
        assert name in registry.CORE_TOOL_NAMES, (
            f"{name} must be in CORE_TOOL_NAMES"
        )


# --- Pull tool specifics ---

def test_pull_uses_ff_only():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._ff_pull)
    assert "--ff-only" in source, "Pull must use --ff-only for safety"


def test_pull_fetches_before_merge():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._ff_pull)
    fetch_pos = source.find("git fetch")
    merge_pos = source.find("git merge")
    assert fetch_pos != -1, "Must call git fetch"
    assert merge_pos != -1, "Must call git merge"
    assert fetch_pos < merge_pos, "Fetch must come before merge"


# --- Revert tool specifics ---

def test_revert_uses_git_lock():
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "_acquire_git_lock" in source
    assert "_release_git_lock" in source


def test_revert_aborts_on_failure():
    """On revert failure, git revert --abort must be called."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert '"--abort"' in source and '"revert"' in source


def test_revert_commit_blocks_merge_commits():
    """revert_commit must reject merge commits upfront."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._revert_commit)
    assert "merge commit" in source.lower()
    assert "rev-list" in source or "parents" in source


def test_restore_to_head_blocks_safety_critical_full_restore():
    """Full restore (no paths) must check dirty files against protected paths."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._restore_to_head)
    assert "affected_critical" in source or "dirty_files" in source, (
        "Full restore must parse dirty files and check against protected paths"
    )


def test_also_stage_blocks_safety_critical():
    """also_stage must check protected paths before staging."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._repo_write_commit)
    assert "protected_paths_in" in source, (
        "repo_write_commit must check also_stage paths against the shared protected path policy"
    )


# --- Auto-push (Phase 5) ---

def test_auto_push_function_exists():
    git_mod = _get_git_module()
    assert hasattr(git_mod, "_auto_push")
    assert callable(git_mod._auto_push)


def test_auto_push_called_in_commit_functions():
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        assert "_auto_push" in source, (
            f"{fn_name} must call _auto_push after successful commit"
        )


def test_auto_push_not_in_rollback_tools():
    """Auto-push must NOT be wired into restore_to_head or revert_commit."""
    git_mod = _get_git_module()
    for fn_name in ("_restore_to_head", "_revert_commit", "_ff_pull"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        assert "_auto_push" not in source, (
            f"{fn_name} must NOT call _auto_push"
        )


def test_auto_push_is_best_effort():
    """_auto_push must catch all exceptions and return a string (never raise)."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._auto_push)
    assert "except Exception" in source
    assert "non-fatal" in source.lower() or "non_fatal" in source.lower()


def test_auto_push_outside_git_lock():
    """Auto-push call must happen AFTER _release_git_lock, not inside the try/finally."""
    git_mod = _get_git_module()
    for fn_name in ("_repo_write_commit", "_repo_commit_push"):
        source = inspect.getsource(getattr(git_mod, fn_name))
        lock_release_pos = source.rfind("_release_git_lock")
        push_pos = source.rfind("_auto_push")
        assert lock_release_pos < push_pos, (
            f"{fn_name}: _auto_push must come after _release_git_lock"
        )


# --- Credential migration (Phase 5) ---

def test_migrate_remote_credentials_exists():
    git_ops = _get_git_ops_module()
    assert hasattr(git_ops, "migrate_remote_credentials")
    assert callable(git_ops.migrate_remote_credentials)


def test_migrate_remote_credentials_uses_configure_remote():
    git_ops = _get_git_ops_module()
    source = inspect.getsource(git_ops.migrate_remote_credentials)
    assert "configure_remote" in source


# --- ARCHITECTURE version sync (Phase 5) ---

def test_version_sync_checks_architecture_md():
    """_check_version_sync must compare VERSION with ARCHITECTURE.md header."""
    sys.path.insert(0, REPO)
    agent_mod = importlib.import_module("ouroboros.agent")
    source = inspect.getsource(agent_mod.OuroborosAgent._check_version_sync)
    assert "ARCHITECTURE" in source
    assert "architecture_version" in source


# ---------------------------------------------------------------------------
# Advisory pre-review gate (new)
# ---------------------------------------------------------------------------

def _get_advisory_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.tools.claude_advisory_review")


def _get_review_state_module():
    sys.path.insert(0, REPO)
    return importlib.import_module("ouroboros.review_state")


def test_advisory_pre_review_registered():
    """advisory_pre_review must be registered as a tool."""
    adv_mod = _get_advisory_module()
    names = [t.name for t in adv_mod.get_tools()]
    assert "advisory_pre_review" in names


def test_review_status_registered():
    """review_status must be registered as a tool."""
    adv_mod = _get_advisory_module()
    names = [t.name for t in adv_mod.get_tools()]
    assert "review_status" in names


def test_advisory_freshness_check_exists_in_git():
    """_check_advisory_freshness must be defined in git.py."""
    git_mod = _get_git_module()
    assert hasattr(git_mod, "_check_advisory_freshness")
    assert callable(git_mod._check_advisory_freshness)


def test_advisory_gate_in_repo_commit_push():
    """The shared reviewed stage must gate review on advisory freshness."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._run_reviewed_stage_cycle)
    assert "_check_advisory_freshness" in source
    # Advisory gate must come before parallel review (which contains unified review)
    advisory_pos = source.find("_check_advisory_freshness")
    review_pos = source.find("_run_parallel_review")
    assert advisory_pos != -1, "_check_advisory_freshness not found in _run_reviewed_stage_cycle"
    assert review_pos != -1, "_run_parallel_review not found in _run_reviewed_stage_cycle"
    assert advisory_pos < review_pos, "Advisory gate must precede parallel review"
    # Verify _run_parallel_review contains _run_unified_review
    parallel_source = inspect.getsource(git_mod._run_parallel_review)
    assert "_run_unified_review" in parallel_source


def test_advisory_gate_lives_in_shared_reviewed_stage_cycle():
    """Legacy repo_write_commit must inherit the advisory gate via the shared stage helper."""
    git_mod = _get_git_module()
    source = inspect.getsource(git_mod._run_reviewed_stage_cycle)
    assert "_check_advisory_freshness" in source


def test_advisory_freshness_blocks_without_fresh_run(tmp_path):
    """_check_advisory_freshness must return ADVISORY_PRE_REVIEW_REQUIRED if no fresh run."""
    import pathlib
    git_mod = _get_git_module()

    class FakeCtx:
        repo_dir = tmp_path
        drive_root = tmp_path
        task_id = "test-task"
        def drive_logs(self):
            logs = tmp_path / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            return logs

    # Initialize a bare git repo so compute_snapshot_hash works
    import subprocess
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)

    result = git_mod._check_advisory_freshness(FakeCtx(), "test commit message")
    assert result is not None
    assert "ADVISORY_PRE_REVIEW_REQUIRED" in result


def test_advisory_freshness_passes_with_fresh_run(tmp_path):
    """_check_advisory_freshness must return None when a fresh run exists."""
    import subprocess
    git_mod = _get_git_module()
    rs_mod = _get_review_state_module()

    # Separate repo_dir and drive_root so drive data doesn't pollute git status
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()

    # Init git repo in repo_dir
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

    commit_message = "test commit"

    class FakeCtx:
        pass
    ctx = FakeCtx()
    ctx.repo_dir = repo_dir
    ctx.drive_root = drive_root
    ctx.task_id = "test-task"
    ctx.drive_logs = lambda: drive_root / "logs"

    # advisory_review.json is excluded from snapshot hash (see _SNAPSHOT_EXCLUDE_PATHS)
    # drive_root is outside repo_dir so no git pollution
    snapshot_hash = rs_mod.compute_snapshot_hash(repo_dir, commit_message)

    # Inject a fresh run with that exact hash
    state = rs_mod.AdvisoryReviewState()
    state.add_run(rs_mod.AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status="fresh",
        ts="2026-01-01T00:00:00",
    ))
    rs_mod.save_state(drive_root, state)

    # Hash is stable — drive_root is outside repo_dir, no git status pollution
    result = git_mod._check_advisory_freshness(ctx, commit_message)
    assert result is None, f"Expected gate to pass but got: {result}"


def test_advisory_freshness_blocks_on_open_commit_readiness_debt(tmp_path):
    """Fresh advisory is not enough when commit-readiness debt remains open."""
    import subprocess

    git_mod = _get_git_module()
    rs_mod = _get_review_state_module()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

    commit_message = "test commit"
    snapshot_hash = rs_mod.compute_snapshot_hash(repo_dir, commit_message)
    repo_key = rs_mod.make_repo_key(repo_dir)

    state = rs_mod.AdvisoryReviewState()
    state.add_run(rs_mod.AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status="fresh",
        ts="2026-01-01T00:00:00",
        repo_key=repo_key,
        readiness_warnings=["Manual verification still required before commit."],
    ))
    state._sync_commit_readiness_debts(repo_key=repo_key)
    assert len(state.get_open_commit_readiness_debts(repo_key=repo_key)) == 1
    rs_mod.save_state(drive_root, state)

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = repo_dir
    ctx.drive_root = drive_root
    ctx.task_id = "test-task"
    ctx.drive_logs = lambda: drive_root / "logs"

    result = git_mod._check_advisory_freshness(ctx, commit_message)
    assert result is not None
    assert "ADVISORY_PRE_REVIEW_REQUIRED" in result
    assert "Commit-readiness debt" in result


def test_advisory_freshness_is_repo_scoped(tmp_path):
    """A fresh run for repo A must not satisfy repo B when hashes coincide."""
    import subprocess
    git_mod = _get_git_module()
    rs_mod = _get_review_state_module()

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_a), capture_output=True)
    subprocess.run(["git", "init"], cwd=str(repo_b), capture_output=True)

    commit_message = "same commit message"
    snapshot_hash = rs_mod.compute_snapshot_hash(repo_a, commit_message)
    state = rs_mod.AdvisoryReviewState()
    state.add_run(rs_mod.AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message=commit_message,
        status="fresh",
        ts="2026-01-01T00:00:00",
        repo_key=rs_mod.make_repo_key(repo_a),
    ))
    rs_mod.save_state(drive_root, state)

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = repo_b
    ctx.drive_root = drive_root
    ctx.task_id = "repo-b-task"
    ctx.drive_logs = lambda: drive_root / "logs"

    result = git_mod._check_advisory_freshness(ctx, commit_message)
    assert result is not None
    assert "ADVISORY_PRE_REVIEW_REQUIRED" in result


def test_open_obligations_are_repo_scoped(tmp_path):
    """Open obligations in repo A must not block a fresh advisory in repo B."""
    import subprocess
    git_mod = _get_git_module()
    rs_mod = _get_review_state_module()

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_a), capture_output=True)
    subprocess.run(["git", "init"], cwd=str(repo_b), capture_output=True)

    commit_message = "shared message"
    state = rs_mod.AdvisoryReviewState()
    state.add_run(rs_mod.AdvisoryRunRecord(
        snapshot_hash=rs_mod.compute_snapshot_hash(repo_b, commit_message),
        commit_message=commit_message,
        status="fresh",
        ts="2026-01-01T00:00:00",
        repo_key=rs_mod.make_repo_key(repo_b),
    ))
    state.add_blocking_attempt(rs_mod.CommitAttemptRecord(
        ts="2026-01-01T00:05:00",
        commit_message="repo a blocked",
        status="blocked",
        repo_key=rs_mod.make_repo_key(repo_a),
        block_reason="critical_findings",
        critical_findings=[{
            "item": "tests_affected",
            "verdict": "FAIL",
            "severity": "critical",
            "reason": "missing tests in repo a",
        }],
    ))
    rs_mod.save_state(drive_root, state)

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = repo_b
    ctx.drive_root = drive_root
    ctx.task_id = "repo-b-task"
    ctx.drive_logs = lambda: drive_root / "logs"

    result = git_mod._check_advisory_freshness(ctx, commit_message)
    assert result is None, f"Repo-scoped obligations should not block repo B: {result}"


def test_snapshot_hash_stable_on_message_change(tmp_path):
    """Snapshot hash must NOT differ when only commit_message changes.

    Hash is now based on code content only (decoupled from commit_message
    to make freshness less brittle when the message is slightly rephrased).
    """
    import subprocess
    rs_mod = _get_review_state_module()
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)

    h1 = rs_mod.compute_snapshot_hash(tmp_path, "message A")
    h2 = rs_mod.compute_snapshot_hash(tmp_path, "message B")
    assert h1 == h2


def test_bypass_is_audited(tmp_path):
    """Bypassing advisory gate must write advisory_pre_review_bypassed to events.jsonl."""
    import json
    import subprocess
    git_mod = _get_git_module()
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    class FakeCtx:
        repo_dir = tmp_path
        drive_root = tmp_path
        task_id = "bypass-task"
        def drive_logs(self):
            return tmp_path / "logs"

    result = git_mod._check_advisory_freshness(
        FakeCtx(), "bypassed commit", skip_advisory_pre_review=True
    )
    assert result is None  # bypass passes

    events_path = tmp_path / "logs" / "events.jsonl"
    assert events_path.exists(), "events.jsonl must exist after bypass"
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    bypass_events = [e for e in events if e.get("type") == "advisory_pre_review_bypassed"]
    assert len(bypass_events) == 1, "Exactly one bypass event must be logged"
    assert bypass_events[0]["task_id"] == "bypass-task"


def test_advisory_pre_review_tool_schema_has_skip_param():
    """advisory_pre_review schema must expose skip_advisory_pre_review param."""
    adv_mod = _get_advisory_module()
    tools = adv_mod.get_tools()
    adv_tool = next(t for t in tools if t.name == "advisory_pre_review")
    props = adv_tool.schema["parameters"]["properties"]
    assert "skip_advisory_pre_review" in props
    assert props["skip_advisory_pre_review"].get("default") is False


def test_repo_commit_schema_has_skip_advisory_param():
    """repo_commit schema must expose skip_advisory_pre_review param."""
    git_mod = _get_git_module()
    tools = git_mod.get_tools()
    commit_tool = next(t for t in tools if t.name == "repo_commit")
    props = commit_tool.schema["parameters"]["properties"]
    assert "skip_advisory_pre_review" in props


def test_advisory_auto_bypass_on_missing_key(tmp_path, monkeypatch):
    """advisory_pre_review must auto-bypass with audit when ANTHROPIC_API_KEY is absent."""
    import json
    import subprocess
    adv_mod = _get_advisory_module()
    rs_mod = _get_review_state_module()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    progress_calls = []

    class FakeCtx:
        pass
    ctx = FakeCtx()
    ctx.repo_dir = str(repo_dir)
    ctx.drive_root = str(drive_root)
    ctx.task_id = "autobypass-task"
    ctx.drive_logs = lambda: drive_root / "logs"
    ctx.emit_progress_fn = lambda msg: progress_calls.append(msg)

    result_raw = adv_mod._handle_advisory_pre_review(ctx, commit_message="test commit")
    result = json.loads(result_raw)

    # Must be bypassed, not errored
    assert result["status"] == "bypassed"
    assert "ANTHROPIC_API_KEY" in result["bypass_reason"]

    # Must create a fresh advisory state (bypassed counts as fresh for gate)
    state = rs_mod.load_state(drive_root)
    assert state.latest() is not None
    assert state.latest().status == "bypassed"

    # Must audit bypass to events.jsonl
    events_path = drive_root / "logs" / "events.jsonl"
    assert events_path.exists(), "events.jsonl must exist after auto-bypass"
    events = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
    bypass_events = [e for e in events if e.get("type") == "advisory_pre_review_bypassed"]
    assert len(bypass_events) == 1
    assert "ANTHROPIC_API_KEY" in bypass_events[0]["bypass_reason"]


def test_advisory_prompt_contains_blocking_history_when_blocked(tmp_path):
    """Advisory prompt must include blocking history section when last commit was blocked."""
    import subprocess
    adv_mod = _get_advisory_module()
    rs_mod = _get_review_state_module()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

    # Create a blocked commit attempt with structured critical findings
    state = rs_mod.AdvisoryReviewState()
    attempt = rs_mod.CommitAttemptRecord(
        ts="2026-04-02T22:00:00",
        commit_message="test blocked commit",
        status="blocked",
        block_reason="critical_findings",
        block_details=(
            "⚠️ REVIEW_BLOCKED: Critical issues found.\n"
            "  CRITICAL: [gpt-5.4] bible_compliance: Missing BIBLE.md update\n"
            "  CRITICAL: [gpt-5.4] tests_affected: No tests for new function\n"
            "  WARN: [opus] self_consistency: Minor doc drift"
        ),
        critical_findings=[
            {"verdict": "FAIL", "severity": "critical",
             "item": "bible_compliance", "reason": "Missing BIBLE.md update", "model": "m"},
            {"verdict": "FAIL", "severity": "critical",
             "item": "tests_affected", "reason": "No tests for new function", "model": "m"},
        ],
    )
    state.add_blocking_attempt(attempt)
    rs_mod.save_state(drive_root, state)

    # Build the advisory prompt with drive_root
    prompt = adv_mod._build_advisory_prompt(
        repo_dir, "test commit", drive_root=drive_root
    )

    # Must contain obligations section (new format)
    assert "Unresolved obligations" in prompt
    assert "bible_compliance" in prompt
    assert "tests_affected" in prompt
    assert "should explicitly address" in prompt


def test_advisory_prompt_no_blocking_history_when_succeeded(tmp_path):
    """Advisory prompt must NOT include blocking history when last commit succeeded."""
    import subprocess
    adv_mod = _get_advisory_module()
    rs_mod = _get_review_state_module()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

    state = rs_mod.AdvisoryReviewState()
    state.last_commit_attempt = rs_mod.CommitAttemptRecord(
        ts="2026-04-02T22:00:00",
        commit_message="test commit",
        status="succeeded",
    )
    rs_mod.save_state(drive_root, state)

    prompt = adv_mod._build_advisory_prompt(
        repo_dir, "test commit", drive_root=drive_root
    )

    assert "## Unresolved obligations from previous blocking rounds" not in prompt


def test_advisory_prompt_no_blocking_history_without_drive_root(tmp_path):
    """Advisory prompt must gracefully skip blocking history when no drive_root."""
    import subprocess
    adv_mod = _get_advisory_module()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

    prompt = adv_mod._build_advisory_prompt(repo_dir, "test commit")
    assert "## Unresolved obligations from previous blocking rounds" not in prompt


def test_advisory_prompt_strictness_formulations():
    """Advisory prompt must contain the same strictness language as blocking reviewers."""
    import subprocess
    adv_mod = _get_advisory_module()

    import pathlib as _pl
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        repo_dir = _pl.Path(d)
        (repo_dir / "BIBLE.md").write_text("test bible", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

        prompt = adv_mod._build_advisory_prompt(repo_dir, "test commit")

        # Key strictness formulations that must be present
        assert "same rigor" in prompt.lower() or "same severity threshold" in prompt.lower()
        assert "do not stop after finding the first issue" in prompt.lower()
        assert "distinct problem" in prompt.lower()
        assert "read the full content of every changed file" in prompt.lower()
        assert "all bugs, logic errors" in prompt.lower()
        # Must NOT contain the old relaxing language
        assert "findings do not directly block" not in prompt.lower()


def test_advisory_prompt_references_architecture_doc_via_read_tool():
    """Advisory prompt must inline ARCHITECTURE.md content when available.

    The v4.15.1 prompt restores ARCHITECTURE.md directly into the advisory context so
    the reviewer always sees version-sync and module-structure facts without an extra
    read step. The touched-file pack must avoid duplicating it separately.
    """
    import subprocess
    adv_mod = _get_advisory_module()

    import pathlib as _pl
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        repo_dir = _pl.Path(d)
        (repo_dir / "BIBLE.md").write_text("test bible", encoding="utf-8")
        (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
        (repo_dir / "docs" / "ARCHITECTURE.md").write_text(
            "# Ouroboros v99.0.0 — Architecture", encoding="utf-8"
        )
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

        prompt = adv_mod._build_advisory_prompt(repo_dir, "test commit")

        assert "ARCHITECTURE.md" in prompt, "Prompt must include an ARCHITECTURE.md section"
        assert "## ARCHITECTURE.md" in prompt, "Prompt should expose ARCHITECTURE.md as a first-class section"
        assert "Ouroboros v99.0.0" in prompt, (
            "ARCHITECTURE.md content should now be inlined for advisory review"
        )


def test_advisory_prompt_strictness_concrete_fix_requirement():
    """Advisory prompt must require concrete fix suggestions for FAIL findings."""
    import subprocess
    adv_mod = _get_advisory_module()

    import pathlib as _pl
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        repo_dir = _pl.Path(d)
        subprocess.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

        prompt = adv_mod._build_advisory_prompt(repo_dir, "test commit")

        # Must require actionable fix suggestions
        assert "concrete" in prompt.lower()
        assert "fix" in prompt.lower()
        assert "how to fix" in prompt.lower() or "how to change" in prompt.lower() or "what to change" in prompt.lower()


def test_blocking_history_section_with_scope_blocked(tmp_path):
    """Blocking history should also work for scope_blocked commits."""
    adv_mod = _get_advisory_module()
    rs_mod = _get_review_state_module()

    drive_root = tmp_path
    (drive_root / "state").mkdir(parents=True)

    state = rs_mod.AdvisoryReviewState()
    attempt = rs_mod.CommitAttemptRecord(
        ts="2026-04-02T22:00:00",
        commit_message="scope blocked commit",
        status="blocked",
        block_reason="scope_blocked",
        block_details=(
            "⚠️ SCOPE_REVIEW_BLOCKED: Missing touchpoint.\n"
            "CRITICAL: [opus] forgotten_touchpoints: ARCHITECTURE.md not updated"
        ),
        critical_findings=[
            {"verdict": "FAIL", "severity": "critical",
             "item": "forgotten_touchpoints", "reason": "ARCHITECTURE.md not updated", "model": "opus"},
        ],
    )
    state.add_blocking_attempt(attempt)
    rs_mod.save_state(drive_root, state)

    section = adv_mod._build_blocking_history_section(drive_root)
    assert "Unresolved obligations" in section
    assert "scope_blocked" in section
    assert "ARCHITECTURE.md" in section


def test_review_blocked_message_prefers_fix_over_rebuttal():
    """v4.9.2: REVIEW_BLOCKED message directs agent to fix first, rebuttal only for factual errors."""
    from ouroboros.tools.review import _build_critical_block_message

    class FakeCtx:
        _review_iteration_count = 1
        _review_history = []

    msg = _build_critical_block_message(
        FakeCtx(), "test commit", ["bible_compliance: violation"], [], ""
    )
    assert "factually incorrect" in msg.lower()
    assert "not to argue" in msg.lower() or "not to argue against" in msg.lower()


def test_review_blocked_5plus_hint_suggests_split():
    """v4.9.2: After 5+ attempts, hint suggests implementing the fix or splitting."""
    from ouroboros.tools.review import _build_critical_block_message

    class FakeCtx:
        # v4.33.0 lowered the threshold from 5 to 3 — 5 still triggers but
        # the phrasing changed from "report the blockage" to "send_user_message
        # to escalate" which carries the same semantic weight.
        _review_iteration_count = 5
        _review_history = []

    msg = _build_critical_block_message(
        FakeCtx(), "test commit", ["tests_affected: missing tests"], [], ""
    )
    lowered = msg.lower()
    assert "split" in lowered, f"missing split-the-diff guidance: {msg!r}"
    assert ("send_user_message" in lowered or "escalate" in lowered
            or "report" in lowered), (
        f"missing escalation guidance: {msg!r}"
    )


def test_review_blocked_message_requires_reaudit_after_first_block():
    """Blocked-review guidance should explicitly require a full-diff re-audit after the first block."""
    from ouroboros.tools.review import _build_critical_block_message

    class FakeCtx:
        _review_iteration_count = 2
        _review_history = []
        _last_review_critical_findings = [{"item": "code_quality"}]
        _last_review_advisory_findings = []

    msg = _build_critical_block_message(
        FakeCtx(), "test commit", ["code_quality: review mismatch"], [], ""
    )
    lowered = msg.lower()
    assert "re-read the full diff" in lowered
    assert "group obligations by root cause" in lowered
    assert "rewrite the plan" in lowered


def test_self_consistency_listed_as_critical_in_severity_rules():
    """self_consistency (item 13) must be treated as conditionally critical, not always advisory."""
    import pathlib
    checklists_path = pathlib.Path(__file__).parent.parent / "docs" / "CHECKLISTS.md"
    content = checklists_path.read_text(encoding="utf-8")

    # The severity rules section must describe self_consistency as conditionally critical
    assert "self_consistency" in content
    # Must NOT say items 11-13 are ALL advisory
    lines = content.split("\n")
    for line in lines:
        if "items 11-13 are advisory" in line.lower():
            raise AssertionError(
                f"Found old 'items 11-13 are advisory' rule — self_consistency "
                f"must now be conditionally critical:\n  {line}"
            )
    # Must say item 13 is conditionally critical
    assert "item 13" in content.lower() and "critical" in content.lower()
    # v4.33.0: the old "README test counts" example was folded into the
    # broader Critical surface whitelist. Narrative / prose / commentary
    # mismatches outside the whitelist must be explicitly advisory.
    assert "Critical surface whitelist" in content
    assert "advisory" in content.lower()
    # And the "narrative" framing of commit-message / doc wording remains.
    assert "narrative" in content.lower()


def test_development_compliance_checklist_expanded():
    """development_compliance description must include specific concrete checks."""
    import pathlib
    checklists_path = pathlib.Path(__file__).parent.parent / "docs" / "CHECKLISTS.md"
    content = checklists_path.read_text(encoding="utf-8")

    # All these concrete checks must appear in the checklist
    required_terms = [
        "snake_case",
        "PascalCase",
        "Gateway",
        "LLMClient",
        "[:N]",
        "ToolEntry",
    ]
    for term in required_terms:
        assert term in content, (
            f"development_compliance checklist must mention '{term}' for concrete checks, "
            f"but it's missing from CHECKLISTS.md"
        )


def test_triad_review_prompt_has_thoroughness_instructions():
    """Triad review prompt must include thoroughness instructions."""
    from ouroboros.tools.review import _REVIEW_PROMPT_TEMPLATE

    prompt_lower = _REVIEW_PROMPT_TEMPLATE.lower()
    required_phrases = [
        "read the entire",
        "all bugs, logic errors",
        "do not stop after finding",
        "each distinct problem",
        "pass reasons may be brief",
        "fail reasons must be detailed",
        "how-to-fix",
    ]
    for phrase in required_phrases:
        assert phrase in prompt_lower, (
            f"Triad review prompt missing required thoroughness instruction: '{phrase}'"
        )


def test_triad_review_reasoning_effort_is_medium_not_low():
    """Triad review models must use at least medium reasoning effort, not 'low'."""
    import inspect
    from ouroboros.tools.review import _query_model

    source = inspect.getsource(_query_model)
    # Must NOT contain reasoning_effort="low"
    assert 'reasoning_effort="low"' not in source, (
        "_query_model uses reasoning_effort='low' — must be 'medium' or higher"
    )
    # Must contain medium or higher
    assert 'reasoning_effort="medium"' in source or 'reasoning_effort="high"' in source, (
        "_query_model must use reasoning_effort='medium' or 'high'"
    )


def test_advisory_prompt_contains_obligation_targeting_instructions(tmp_path):
    """_build_advisory_prompt must instruct the reviewer how to target a specific
    obligation when multiple open obligations share the same checklist item.
    Without this, a generic item-name PASS cannot disambiguate which obligation
    was resolved, and the resolution logic leaves all same-item obligations open.
    """
    import tempfile
    import pathlib as _pl
    import subprocess as _sp
    adv_mod = _get_advisory_module()

    with tempfile.TemporaryDirectory() as d:
        repo_dir = _pl.Path(d)
        _sp.run(["git", "init"], cwd=str(repo_dir), capture_output=True)

        prompt = adv_mod._build_advisory_prompt(repo_dir, "test commit")

        # Must explain the (obligation <id>) suffix mechanism
        assert "obligation" in prompt.lower(), (
            "Prompt must mention 'obligation' targeting to allow per-finding resolution"
        )
        assert "(obligation" in prompt, (
            "Prompt must show the '(obligation <id>)' suffix syntax for targeting specific obligations"
        )
        # Must warn that a generic PASS won't resolve all same-item obligations
        assert "will NOT resolve" in prompt or "will not resolve" in prompt.lower(), (
            "Prompt must warn that generic item-name PASS won't resolve all same-item obligations"
        )

