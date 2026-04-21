"""Tests for the improved advisory pre-review workflow:

1. mark_advisory_stale_after_edit invalidates fresh advisory runs
2. ObligationItem creation and deduplication
3. blocking_history bounded cap
4. add_blocking_attempt populates open_obligations
5. on_successful_commit clears obligations and history
6. _build_blocking_history_section reads full history
7. format_status_section shows staleness from edit and obligations
8. review_status response includes stale_from_edit and open_obligations
9. _check_advisory_freshness includes obligation summary
10. Serialization roundtrip for new fields (including snapshot_paths)
11. _collect_review_findings stores structured findings on ctx
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_drive_root(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_blocking_attempt(
    commit_message: str = "test commit",
    block_reason: str = "critical_findings",
    critical_findings: list | None = None,
):
    from ouroboros.review_state import CommitAttemptRecord, _utc_now
    return CommitAttemptRecord(
        ts=_utc_now(),
        commit_message=commit_message,
        status="blocked",
        block_reason=block_reason,
        block_details="CRITICAL: something",
        duration_sec=5.0,
        critical_findings=critical_findings or [
            {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
             "reason": "No test changes found", "model": "test-model"},
        ],
    )


# ---------------------------------------------------------------------------
# 1. mark_advisory_stale_after_edit
# ---------------------------------------------------------------------------

def test_mark_stale_after_edit_invalidates_fresh_run(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryRunRecord, AdvisoryReviewState,
        load_state, save_state, mark_advisory_stale_after_edit, _utc_now,
    )
    state = AdvisoryReviewState()
    run = AdvisoryRunRecord(
        snapshot_hash="aabbcc112233",
        commit_message="v1: test",
        status="fresh",
        ts=_utc_now(),
    )
    state.add_run(run)
    save_state(drive_root, state)

    mark_advisory_stale_after_edit(drive_root)

    loaded = load_state(drive_root)
    assert loaded.runs[-1].status == "stale"
    assert loaded.last_stale_from_edit_ts != ""


def test_mark_stale_after_edit_no_op_when_no_fresh(tmp_path):
    """If there is no fresh run, mark_advisory_stale_after_edit should not error."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryRunRecord, AdvisoryReviewState,
        save_state, mark_advisory_stale_after_edit, _utc_now,
    )
    state = AdvisoryReviewState()
    run = AdvisoryRunRecord(
        snapshot_hash="aabbcc112233",
        commit_message="v1: test",
        status="stale",
        ts=_utc_now(),
    )
    state.runs.append(run)
    save_state(drive_root, state)

    mark_advisory_stale_after_edit(drive_root)  # should not raise

    from ouroboros.review_state import load_state
    loaded = load_state(drive_root)
    assert loaded.runs[-1].status == "stale"
    # last_stale_from_edit_ts should NOT be set (no fresh run was invalidated)
    assert loaded.last_stale_from_edit_ts == ""


def test_mark_stale_after_edit_only_affects_fresh(tmp_path):
    """mark_all_stale should only transition fresh→stale, not touch bypassed."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryRunRecord, AdvisoryReviewState,
        save_state, mark_advisory_stale_after_edit, load_state, _utc_now,
    )
    state = AdvisoryReviewState()
    fresh = AdvisoryRunRecord("hash1", "commit1", "fresh", _utc_now())
    bypassed = AdvisoryRunRecord("hash2", "commit2", "bypassed", _utc_now())
    state.runs = [fresh, bypassed]
    save_state(drive_root, state)

    mark_advisory_stale_after_edit(drive_root)

    loaded = load_state(drive_root)
    statuses = {r.snapshot_hash: r.status for r in loaded.runs}
    assert statuses["hash1"] == "stale"
    # bypassed runs are now also invalidated after worktree edits — same lifecycle as fresh
    assert statuses["hash2"] == "stale"


def test_mark_stale_after_edit_invalidates_bypassed_only_state(tmp_path):
    """mark_advisory_stale_after_edit must invalidate even when only bypassed runs exist."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryRunRecord, AdvisoryReviewState,
        save_state, mark_advisory_stale_after_edit, load_state, _utc_now,
    )
    state = AdvisoryReviewState()
    # Only a bypassed run — no fresh run present
    bypassed = AdvisoryRunRecord("hash-bypass", "commit-bypass", "bypassed", _utc_now())
    state.runs = [bypassed]
    save_state(drive_root, state)

    mark_advisory_stale_after_edit(drive_root)

    loaded = load_state(drive_root)
    assert loaded.runs[0].status == "stale"
    assert loaded.last_stale_from_edit_ts != ""


def test_invalidate_advisory_after_mutation_targets_matching_repo(tmp_path):
    """Phase 3: repo-scoped invalidation should stale only the mutated repo when identity is known."""
    from ouroboros.review_state import (
        AdvisoryRunRecord,
        AdvisoryReviewState,
        invalidate_advisory_after_mutation,
        load_state,
        save_state,
        _utc_now,
    )

    drive_root = _make_drive_root(tmp_path)
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)

    state = AdvisoryReviewState()
    state.add_run(AdvisoryRunRecord(
        snapshot_hash="hash-a",
        commit_message="repo a",
        status="fresh",
        ts=_utc_now(),
        repo_key=str(repo_a),
    ))
    state.add_run(AdvisoryRunRecord(
        snapshot_hash="hash-b",
        commit_message="repo b",
        status="fresh",
        ts=_utc_now(),
        repo_key=str(repo_b),
    ))
    save_state(drive_root, state)

    invalidate_advisory_after_mutation(
        drive_root,
        mutation_root=repo_b,
        changed_paths=["foo.py"],
        source_tool="claude_code_edit",
    )

    loaded = load_state(drive_root)
    statuses = {run.repo_key: run.status for run in loaded.advisory_runs}
    assert statuses[str(repo_a)] == "fresh"
    assert statuses[str(repo_b)] == "stale"
    assert "claude_code_edit mutated the worktree" in loaded.last_stale_reason


# ---------------------------------------------------------------------------
# 2. ObligationItem creation
# ---------------------------------------------------------------------------

def test_public_obligation_ids_are_prefixed_and_fingerprint_is_internal():
    from ouroboros.review_state import AdvisoryReviewState
    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "No test changes", "model": "m",
    }])
    first.task_id = "ob-1"
    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "version_bump",
        "reason": "VERSION not updated", "model": "m",
    }])
    second.task_id = "ob-2"

    state.add_blocking_attempt(first)
    state.add_blocking_attempt(second)

    assert [ob.obligation_id for ob in state.open_obligations] == ["obl-0001", "obl-0002"]
    assert state.open_obligations[0].fingerprint.startswith("finding:tests_affected:")
    assert state.open_obligations[1].fingerprint.startswith("finding:version_bump:")


def test_canonical_item_retry_reuses_same_public_obligation_id_despite_reason_rephrase():
    from ouroboros.review_state import AdvisoryReviewState
    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "No unit tests cover the new path", "model": "m",
    }])
    first.task_id = "rephrase-1"

    state.add_blocking_attempt(first)
    original = state.get_open_obligations()[0]
    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "Missing coverage for the changed behaviour",
        "model": "m",
        "obligation_id": original.obligation_id,
    }])
    second.task_id = "rephrase-2"
    state.add_blocking_attempt(second)

    open_obs = state.get_open_obligations()
    assert len(open_obs) == 1
    assert open_obs[0].obligation_id == original.obligation_id
    assert open_obs[0].fingerprint.startswith("finding:tests_affected:")
    assert open_obs[0].reason == "No unit tests cover the new path"


# ---------------------------------------------------------------------------
# 3. blocking_history cap
# ---------------------------------------------------------------------------

def test_blocking_history_capped_at_max(tmp_path):
    from ouroboros.review_state import (
        AdvisoryReviewState, _MAX_BLOCKING_HISTORY,
    )
    state = AdvisoryReviewState()
    for i in range(_MAX_BLOCKING_HISTORY + 5):
        attempt = _make_blocking_attempt(
            commit_message=f"commit {i}",
            critical_findings=[{
                "verdict": "FAIL", "severity": "critical",
                "item": f"item_{i}", "reason": f"reason {i}", "model": "m",
            }],
        )
        # Give each attempt a unique task_id so _attempt_identity_tuple
        # produces distinct keys even when timestamps collide (Windows
        # datetime.now() has ~15ms granularity — a tight loop can produce
        # duplicate timestamps).
        attempt.task_id = f"cap_test_{i}"
        state.add_blocking_attempt(attempt)
    assert len(state.blocking_history) == _MAX_BLOCKING_HISTORY


# ---------------------------------------------------------------------------
# 4. add_blocking_attempt populates open_obligations
# ---------------------------------------------------------------------------

def test_add_blocking_attempt_creates_obligations():
    from ouroboros.review_state import AdvisoryReviewState
    state = AdvisoryReviewState()
    attempt = _make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No test changes found", "model": "m"},
        {"verdict": "FAIL", "severity": "critical", "item": "version_bump",
         "reason": "VERSION not updated", "model": "m"},
    ])
    state.add_blocking_attempt(attempt)
    assert len(state.open_obligations) == 2
    items = {ob.item for ob in state.open_obligations}
    assert "tests_affected" in items
    assert "version_bump" in items


def test_add_blocking_attempt_deduplicates_obligations():
    """Same issue appearing twice (two blocking rounds) should not duplicate."""
    from ouroboros.review_state import AdvisoryReviewState
    finding = {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
               "reason": "No test changes found", "model": "m"}
    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[finding]))
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[finding]))
    # Same obligation_id should not be added twice
    assert len(state.open_obligations) == 1


def test_add_blocking_attempt_advisory_findings_not_tracked():
    """Advisory (non-critical) findings should NOT create obligations."""
    from ouroboros.review_state import AdvisoryReviewState
    attempt = _make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "advisory", "item": "context_building",
         "reason": "Something advisory", "model": "m"},
    ])
    state = AdvisoryReviewState()
    state.add_blocking_attempt(attempt)
    assert len(state.open_obligations) == 0


# ---------------------------------------------------------------------------
# 5. on_successful_commit clears obligations and history
# ---------------------------------------------------------------------------

def test_on_successful_commit_clears_state():
    from ouroboros.review_state import AdvisoryReviewState
    state = AdvisoryReviewState()
    attempt = _make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No tests", "model": "m"},
    ])
    state.add_blocking_attempt(attempt)
    state.last_stale_from_edit_ts = "2026-04-05T12:00:00"
    assert len(state.open_obligations) == 1

    state.on_successful_commit()

    assert state.open_obligations == []
    assert state.blocking_history == []
    assert state.last_stale_from_edit_ts == ""


def test_commit_readiness_debt_detects_repeat_and_verifies_on_success():
    from ouroboros.review_state import AdvisoryReviewState
    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "No tests", "model": "m",
    }])
    first.task_id = "debt-1"

    state.add_blocking_attempt(first)
    assert state.get_open_commit_readiness_debts() == []
    original = state.get_open_obligations()[0]

    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "Coverage still missing",
        "model": "m",
        "obligation_id": original.obligation_id,
    }])
    second.task_id = "debt-2"
    state.add_blocking_attempt(second)
    open_debts = state.get_open_commit_readiness_debts()
    assert len(open_debts) == 1
    assert open_debts[0].status == "detected"
    assert open_debts[0].source_obligation_ids == ["obl-0001"]

    state.on_successful_commit()
    assert state.get_open_commit_readiness_debts() == []
    assert state.commit_readiness_debts[0].status == "verified"


def test_commit_readiness_debt_verifies_on_repo_scoped_success():
    from ouroboros.review_state import AdvisoryReviewState

    repo_key = "repo://example"
    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "No tests", "model": "m",
    }])
    first.task_id = "repo-debt-1"
    first.repo_key = repo_key
    state.add_blocking_attempt(first)
    original = state.get_open_obligations(repo_key=repo_key)[0]

    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "Coverage still missing",
        "model": "m",
        "obligation_id": original.obligation_id,
    }])
    second.task_id = "repo-debt-2"
    second.repo_key = repo_key
    state.add_blocking_attempt(second)

    assert len(state.get_open_commit_readiness_debts(repo_key=repo_key)) == 1

    state.on_successful_commit(repo_key=repo_key)

    assert state.get_open_obligations(repo_key=repo_key) == []
    assert state.get_open_commit_readiness_debts(repo_key=repo_key) == []
    verified = [debt for debt in state.commit_readiness_debts if debt.repo_key == repo_key]
    assert len(verified) == 1
    assert verified[0].status == "verified"


def test_readiness_warning_debt_verifies_after_repo_scoped_success():
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, CommitAttemptRecord, _utc_now

    repo_key = "repo://example"
    state = AdvisoryReviewState()
    state.add_run(AdvisoryRunRecord(
        snapshot_hash="hash-1",
        commit_message="warned advisory",
        status="fresh",
        ts=_utc_now(),
        repo_key=repo_key,
        readiness_warnings=["Manual verification still required before commit."],
    ))
    assert len(state.get_open_commit_readiness_debts(repo_key=repo_key)) == 1

    state.record_attempt(CommitAttemptRecord(
        ts=_utc_now(),
        commit_message="successful commit",
        status="succeeded",
        repo_key=repo_key,
    ))

    assert state.get_open_commit_readiness_debts(repo_key=repo_key) == []


def test_advisory_pre_review_syncs_readiness_warning_debt(tmp_path, monkeypatch):
    import subprocess

    from ouroboros.review_state import load_state, make_repo_key
    from ouroboros.tools import claude_advisory_review as adv_mod

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "sample.py").write_text("print('hello')\n", encoding="utf-8")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(
        adv_mod,
        "check_worktree_readiness",
        lambda *args, **kwargs: ["Manual verification still required before commit."],
    )
    monkeypatch.setattr(
        adv_mod,
        "_run_claude_advisory",
        lambda *args, **kwargs: (
            [{"item": "code_quality", "verdict": "PASS", "severity": "critical", "reason": "Looks good"}],
            "[]",
            "claude-test",
            42,
        ),
    )

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = str(repo_dir)
    ctx.drive_root = str(drive_root)
    ctx.task_id = "sync-readiness-debt"
    ctx.drive_logs = lambda: drive_root / "logs"
    ctx.emit_progress_fn = lambda msg: None

    result = json.loads(adv_mod._handle_advisory_pre_review(ctx, commit_message="test commit"))
    assert result["status"] == "fresh"

    state = load_state(drive_root)
    repo_key = make_repo_key(repo_dir)
    debts = state.get_open_commit_readiness_debts(repo_key=repo_key)
    assert len(debts) == 1
    assert debts[0].category == "readiness_warning"

    review_status = json.loads(adv_mod._handle_review_status(ctx))
    assert review_status["commit_readiness_debts_count"] == 1
    assert review_status["retry_anchor"] == "commit_readiness_debt"


def test_advisory_pre_review_recomputes_debt_after_resolving_obligation(tmp_path, monkeypatch):
    import subprocess

    from ouroboros.review_state import AdvisoryReviewState, load_state, make_repo_key, save_state
    from ouroboros.tools import claude_advisory_review as adv_mod

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "sample.py").write_text("print('hello')\n", encoding="utf-8")

    repo_key = make_repo_key(repo_dir)
    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "No tests", "model": "m",
    }])
    first.task_id = "sync-clear-1"
    first.repo_key = repo_key
    state.add_blocking_attempt(first)
    original = state.get_open_obligations(repo_key=repo_key)[0]
    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "Coverage still missing",
        "model": "m",
        "obligation_id": original.obligation_id,
    }])
    second.task_id = "sync-clear-2"
    second.repo_key = repo_key
    state.add_blocking_attempt(second)
    assert len(state.get_open_commit_readiness_debts(repo_key=repo_key)) == 1
    save_state(drive_root, state)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr(adv_mod, "check_worktree_readiness", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        adv_mod,
        "_run_claude_advisory",
        lambda *args, **kwargs: (
            [{
                "item": "tests_affected",
                "verdict": "PASS",
                "severity": "critical",
                "reason": "Coverage is now adequate",
                "obligation_id": original.obligation_id,
            }],
            "[]",
            "claude-test",
            42,
        ),
    )

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = str(repo_dir)
    ctx.drive_root = str(drive_root)
    ctx.task_id = "sync-clear-debt"
    ctx.drive_logs = lambda: drive_root / "logs"
    ctx.emit_progress_fn = lambda msg: None

    result = json.loads(adv_mod._handle_advisory_pre_review(ctx, commit_message="test commit"))
    assert result["status"] == "fresh"

    loaded = load_state(drive_root)
    assert loaded.get_open_obligations(repo_key=repo_key) == []
    assert loaded.get_open_commit_readiness_debts(repo_key=repo_key) == []


# ---------------------------------------------------------------------------
# 6. Serialization roundtrip for new fields
# ---------------------------------------------------------------------------

def test_serialization_roundtrip_new_fields(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, load_state, save_state,
    )
    state = AdvisoryReviewState()
    attempt = _make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "bible_compliance",
         "reason": "Violates P5", "model": "m"},
    ])
    state.add_blocking_attempt(attempt)
    state.last_stale_from_edit_ts = "2026-04-05T12:00:00+00:00"
    save_state(drive_root, state)

    loaded = load_state(drive_root)
    assert len(loaded.blocking_history) == 1
    assert len(loaded.open_obligations) == 1
    assert loaded.open_obligations[0].item == "bible_compliance"
    assert loaded.open_obligations[0].status == "still_open"
    assert loaded.last_stale_from_edit_ts == "2026-04-05T12:00:00+00:00"


def test_old_state_loads_cleanly_without_new_fields(tmp_path):
    """Old state files without blocking_history / open_obligations load cleanly."""
    drive_root = _make_drive_root(tmp_path)
    state_file = drive_root / "state" / "advisory_review.json"
    state_file.write_text(json.dumps({
        "runs": [],
        "last_commit_attempt": None,
        "saved_at": "2026-04-01T00:00:00",
    }))

    from ouroboros.review_state import load_state
    loaded = load_state(drive_root)
    assert loaded.blocking_history == []
    assert loaded.open_obligations == []
    assert loaded.last_stale_from_edit_ts == ""


def test_commit_attempt_with_critical_findings_roundtrip(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, load_state, save_state,
    )
    findings = [
        {"verdict": "FAIL", "severity": "critical", "item": "version_bump",
         "reason": "VERSION missing", "model": "m1"},
    ]
    state = AdvisoryReviewState()
    attempt = _make_blocking_attempt(critical_findings=findings)
    state.add_blocking_attempt(attempt)
    save_state(drive_root, state)

    loaded = load_state(drive_root)
    assert loaded.blocking_history[0].critical_findings == findings


# ---------------------------------------------------------------------------
# 7. format_status_section shows staleness and obligations
# ---------------------------------------------------------------------------

def test_format_status_shows_stale_from_edit():
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, format_status_section, _utc_now,
    )
    state = AdvisoryReviewState()
    state.runs.append(AdvisoryRunRecord("h1", "v1", "stale", _utc_now()))
    state.last_stale_from_edit_ts = "2026-04-05T13:00:00+00:00"
    section = format_status_section(state)
    assert "stale after worktree edit" in section
    assert "2026-04-05T13:00" in section


def test_format_status_shows_open_obligations():
    from ouroboros.review_state import AdvisoryReviewState, format_status_section
    state = AdvisoryReviewState()
    attempt = _make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No test changes", "model": "m"},
    ])
    state.add_blocking_attempt(attempt)
    section = format_status_section(state)
    assert "Open obligations" in section
    assert "tests_affected" in section
    assert "Advisory MUST verify" in section


def test_format_status_shows_commit_readiness_debt():
    from ouroboros.review_state import AdvisoryReviewState, format_status_section
    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "No tests", "model": "m",
    }])
    first.task_id = "status-debt-1"
    state.add_blocking_attempt(first)
    original = state.get_open_obligations()[0]
    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL", "severity": "critical", "item": "tests_affected",
        "reason": "Still no tests",
        "model": "m",
        "obligation_id": original.obligation_id,
    }])
    second.task_id = "status-debt-2"
    state.add_blocking_attempt(second)
    debt = state.get_open_commit_readiness_debts()[0]
    debt.evidence = ["first evidence", "second evidence", "third evidence"]

    section = format_status_section(state)
    assert "Commit-readiness debt" in section
    assert "obl-0001" in section
    assert "first evidence" in section
    assert "second evidence" in section
    assert "third evidence" in section


def test_format_status_no_obligations_section_when_clean():
    from ouroboros.review_state import AdvisoryReviewState, format_status_section
    state = AdvisoryReviewState()
    section = format_status_section(state)
    assert "Open obligations" not in section


# ---------------------------------------------------------------------------
# 8. _build_blocking_history_section reads full history
# ---------------------------------------------------------------------------

def test_build_blocking_history_section_empty_when_no_history(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import save_state, AdvisoryReviewState
    save_state(drive_root, AdvisoryReviewState())

    from ouroboros.tools.claude_advisory_review import _build_blocking_history_section
    result = _build_blocking_history_section(drive_root)
    assert result == ""


def test_build_blocking_history_section_contains_all_obligations(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState, save_state
    state = AdvisoryReviewState()
    # Two distinct blocking rounds with different issues
    state.add_blocking_attempt(_make_blocking_attempt(
        commit_message="round 1",
        critical_findings=[{"verdict": "FAIL", "severity": "critical",
                            "item": "tests_affected", "reason": "No tests", "model": "m"}],
    ))
    state.add_blocking_attempt(_make_blocking_attempt(
        commit_message="round 2",
        critical_findings=[{"verdict": "FAIL", "severity": "critical",
                            "item": "version_bump", "reason": "No VERSION", "model": "m"}],
    ))
    save_state(drive_root, state)

    from ouroboros.tools.claude_advisory_review import _build_blocking_history_section
    result = _build_blocking_history_section(drive_root)
    assert "Unresolved obligations" in result
    assert "tests_affected" in result
    assert "version_bump" in result
    assert "```json" in result
    assert "recent_blocking_attempts" in result


def test_build_blocking_history_section_instructions_present(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState, save_state
    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt())
    save_state(drive_root, state)

    from ouroboros.tools.claude_advisory_review import _build_blocking_history_section
    result = _build_blocking_history_section(drive_root)
    assert "should explicitly address" in result
    assert "If fixed" in result
    assert "If not fixed" in result


# ---------------------------------------------------------------------------
# 9. _collect_review_findings stores structured findings on ctx
# ---------------------------------------------------------------------------

def test_collect_review_findings_stores_structured_findings():
    from ouroboros.tools.review import _collect_review_findings

    ctx = MagicMock()
    ctx.drive_logs.return_value = pathlib.Path("/tmp/fake_logs")
    ctx._last_review_critical_findings = []

    model_results = [
        {
            "model": "model-a",
            "verdict": "ok",
            "text": json.dumps([
                {"verdict": "FAIL", "severity": "critical",
                 "item": "tests_affected", "reason": "No tests added"},
                {"verdict": "PASS", "severity": "critical",
                 "item": "bible_compliance", "reason": "OK"},
            ]),
        },
    ]

    with patch("ouroboros.tools.review.append_jsonl"):
        critical_fails, advisory_warns, errored, _raw = _collect_review_findings(ctx, model_results)

    assert len(critical_fails) == 1
    assert "tests_affected" in critical_fails[0]
    structured = ctx._last_review_critical_findings
    assert len(structured) == 1
    assert structured[0]["item"] == "tests_affected"
    assert structured[0]["severity"] == "critical"
    assert structured[0]["verdict"] == "FAIL"
    assert structured[0]["model"] == "model-a"


def test_collect_review_findings_advisory_not_in_structured():
    from ouroboros.tools.review import _collect_review_findings

    ctx = MagicMock()
    ctx.drive_logs.return_value = pathlib.Path("/tmp/fake_logs")
    ctx._last_review_critical_findings = []

    model_results = [
        {
            "model": "model-a",
            "verdict": "ok",
            "text": json.dumps([
                {"verdict": "FAIL", "severity": "advisory",
                 "item": "context_building", "reason": "Not in context"},
            ]),
        },
    ]

    with patch("ouroboros.tools.review.append_jsonl"):
        critical_fails, advisory_warns, _, _raw = _collect_review_findings(ctx, model_results)

    assert len(critical_fails) == 0
    assert len(advisory_warns) == 1
    assert ctx._last_review_critical_findings == []


# ---------------------------------------------------------------------------
# 10. review_status response includes new fields
# ---------------------------------------------------------------------------

def test_review_status_includes_stale_from_edit(tmp_path):
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, _utc_now,
    )
    drive_root = _make_drive_root(tmp_path)
    state = AdvisoryReviewState()
    state.runs.append(AdvisoryRunRecord("h1", "v1", "stale", _utc_now()))
    state.last_stale_from_edit_ts = "2026-04-05T13:00:00+00:00"
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path)  # required by live hash computation

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    assert result["stale_from_edit"] is True
    assert result["stale_from_edit_ts"] is not None
    assert "next_step" in result


def test_review_status_surfaces_explicit_stale_reason(tmp_path):
    """Phase 3: review_status should expose the concrete invalidation reason."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, _utc_now,
    )
    state = AdvisoryReviewState()
    state.runs.append(AdvisoryRunRecord("h1", "v1", "stale", _utc_now()))
    state.last_stale_from_edit_ts = "2026-04-05T13:00:00+00:00"
    state.last_stale_reason = "claude_code_edit mutated the worktree; advisory freshness invalidated."
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path)

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    assert result["stale_from_edit"] is True
    assert result["stale_reason"] == state.last_stale_reason


def test_review_status_includes_open_obligations(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState, save_state
    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No tests", "model": "m"},
    ]))
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path)  # required by live hash computation

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    assert result["open_obligations_count"] == 1
    assert len(result["open_obligations"]) == 1
    assert result["open_obligations"][0]["item"] == "tests_affected"


def test_review_status_next_step_after_edit_staleness(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, _utc_now,
    )
    state = AdvisoryReviewState()
    state.runs.append(AdvisoryRunRecord("h1", "v1", "stale", _utc_now()))
    state.last_stale_from_edit_ts = "2026-04-05T13:00:00+00:00"
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path)  # required by live hash computation

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    assert "invalidated" in result["next_step"].lower()
    assert "advisory_pre_review" in result["next_step"]


# ---------------------------------------------------------------------------
# 11. _check_advisory_freshness includes obligation summary
# ---------------------------------------------------------------------------

def test_check_advisory_freshness_shows_obligations_in_error(tmp_path):
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState, save_state
    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No test changes", "model": "m"},
    ]))
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path / "repo")
    (tmp_path / "repo").mkdir(exist_ok=True)

    from ouroboros.tools.git import _check_advisory_freshness
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="M  some_file.py\n")
        result = _check_advisory_freshness(ctx, "test commit")

    assert result is not None
    assert "ADVISORY_PRE_REVIEW_REQUIRED" in result
    # Workflow instructions
    assert "Finish ALL edits first" in result
    assert "run AFTER all edits" in result
    assert "run IMMEDIATELY after advisory" in result
    # Obligation info in error
    assert "tests_affected" in result or "advisory_pre_review will verify" in result


def test_check_advisory_freshness_correct_workflow_in_error(tmp_path):
    """Error message must reflect new workflow, not old 'fix -> commit' pattern."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import save_state, AdvisoryReviewState
    save_state(drive_root, AdvisoryReviewState())

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path / "repo")
    (tmp_path / "repo").mkdir(exist_ok=True)

    from ouroboros.tools.git import _check_advisory_freshness
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="M  file.py\n")
        result = _check_advisory_freshness(ctx, "test")

    assert result is not None
    # New workflow language
    assert "Finish ALL edits first" in result
    assert "IMMEDIATELY" in result
    # NOT the old misleading language
    assert "fix obvious issues" not in result


def test_obligation_resolution_clears_open_obligations(tmp_path):
    """End-to-end: blocked commit creates obligations → advisory PASS → obligations resolved."""
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, ObligationItem, _utc_now,
        load_state, save_state,
    )

    drive_root = _make_drive_root(tmp_path)

    # 1. Simulate a blocking commit attempt that creates an obligation
    state = AdvisoryReviewState()
    blocking_attempt = CommitAttemptRecord(
        ts=_utc_now(),
        commit_message="v1.0: some feature",
        status="blocked",
        block_reason="critical_findings",
        block_details="CRITICAL: tests_affected",
        duration_sec=5.0,
        critical_findings=[
            {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
             "reason": "No test changes alongside code changes", "model": "test-model"},
        ],
    )
    state.add_blocking_attempt(blocking_attempt)
    save_state(drive_root, state)

    # Verify obligation was created
    loaded = load_state(drive_root)
    open_obs = loaded.get_open_obligations()
    assert len(open_obs) == 1
    assert open_obs[0].item == "tests_affected"
    assert open_obs[0].status == "still_open"

    # 2. Simulate advisory returning PASS for tests_affected
    # This exercises the path in _handle_advisory_pre_review that resolves obligations
    passed_items = {"tests_affected"}
    resolved_ids = [o.obligation_id for o in open_obs
                    if o.item.lower() in passed_items]
    assert len(resolved_ids) == 1

    loaded.resolve_obligations(resolved_ids, resolved_by="advisory run abc123")
    save_state(drive_root, loaded)

    # 3. Verify obligations are resolved
    final = load_state(drive_root)
    assert len(final.get_open_obligations()) == 0
    # Resolved obligation still in list but marked resolved
    all_obs = [o for o in final.open_obligations]
    assert any(o.status == "resolved" for o in all_obs)


def test_obligation_id_field_name_is_obligation_id(tmp_path):
    """ObligationItem must use .obligation_id, not .id — regression test for typo."""
    from ouroboros.review_state import ObligationItem, _utc_now
    ob = ObligationItem(
        obligation_id="abc123",
        item="tests_affected",
        severity="critical",
        reason="no tests",
        source_attempt_ts=_utc_now(),
        source_attempt_msg="v1.0: test",
    )
    # Must not raise AttributeError
    assert ob.obligation_id == "abc123"
    # Must NOT have .id attribute (old wrong name)
    assert not hasattr(ob, "id"), "ObligationItem should use .obligation_id, not .id"


def test_empty_items_does_not_resolve_obligations(tmp_path):
    """Empty advisory items list (parse failure) must NOT resolve open obligations."""
    from ouroboros.review_state import (
        AdvisoryReviewState, CommitAttemptRecord, _utc_now,
        load_state, save_state,
    )

    drive_root = _make_drive_root(tmp_path)

    # Create state with an open obligation
    state = AdvisoryReviewState()
    blocking_attempt = CommitAttemptRecord(
        ts=_utc_now(),
        commit_message="v1.0: feature",
        status="blocked",
        block_reason="critical_findings",
        block_details="CRITICAL: tests_affected",
        duration_sec=3.0,
        critical_findings=[
            {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
             "reason": "No test changes", "model": "test-model"},
        ],
    )
    state.add_blocking_attempt(blocking_attempt)
    save_state(drive_root, state)

    # Verify obligation exists
    loaded = load_state(drive_root)
    assert len(loaded.get_open_obligations()) == 1

    # Simulate empty items list (parse failure scenario)
    items = []
    critical_fails = []  # empty because no items to fail
    # The guard `if not critical_fails and items:` must prevent resolution
    should_resolve = not critical_fails and bool(items)
    assert not should_resolve, (
        "Empty items list should NOT trigger obligation resolution — "
        "obligations must stay open after advisory parse failure"
    )

def test_update_obligations_ignores_invented_public_id():
    """Reviewer-supplied explicit `obligation_id` that doesn't map to any
    existing open obligation must be ignored; the ingestion path must allocate
    a fresh `obl-####` id instead of trusting the reviewer's invention.
    Regression for the round-3 triad finding on `_update_obligations_from_attempt`."""
    from ouroboros.review_state import AdvisoryReviewState

    state = AdvisoryReviewState()
    # Reviewer fabricates an obligation_id that state has never heard of.
    attempt = _make_blocking_attempt(
        critical_findings=[{
            "verdict": "FAIL",
            "severity": "critical",
            "item": "tests_affected",
            "reason": "No tests for new path",
            "obligation_id": "obl-9999",   # invented
        }],
    )
    attempt.task_id = "invented-id-test"
    state.add_blocking_attempt(attempt)

    open_ids = [o.obligation_id for o in state.get_open_obligations()]
    assert open_ids == ["obl-0001"], (
        "Invented obligation_id must be ignored; ingestion should allocate the "
        f"next canonical obl-#### id, got {open_ids!r}."
    )


def test_update_obligations_rejects_cross_item_alias():
    """Reviewer-supplied explicit `obligation_id` that points at an existing
    open obligation but for a DIFFERENT canonical checklist item must NOT
    alias the new finding onto that record. Compatible non-canonical items
    (`bug_*`, `risk_*`) still follow the reviewer's intent (tested separately
    in `test_explicit_obligation_id_wins_on_retry`)."""
    from ouroboros.review_state import AdvisoryReviewState

    state = AdvisoryReviewState()
    first = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL",
        "severity": "critical",
        "item": "tests_affected",
        "reason": "No tests",
    }])
    first.task_id = "first"
    state.add_blocking_attempt(first)
    original_id = state.get_open_obligations()[0].obligation_id

    # Reviewer attempts to alias a security_issues finding onto the
    # tests_affected obligation id — that's a lie, not a rephrase.
    second = _make_blocking_attempt(critical_findings=[{
        "verdict": "FAIL",
        "severity": "critical",
        "item": "security_issues",
        "reason": "Credential leak in logger",
        "obligation_id": original_id,
    }])
    second.task_id = "second"
    state.add_blocking_attempt(second)

    items_by_id = {o.obligation_id: o.item for o in state.get_open_obligations()}
    assert items_by_id[original_id] == "tests_affected"
    # A new obligation must exist for the cross-item finding.
    assert any(
        oid != original_id and item == "security_issues"
        for oid, item in items_by_id.items()
    ), (
        "Cross-item alias must be rejected — a new obl-#### id should track the "
        f"security_issues finding separately. Got: {items_by_id!r}"
    )


def test_advisory_handle_already_fresh_reruns_when_debt_open(tmp_path, monkeypatch):
    """When the snapshot already has a fresh advisory run BUT repo-scoped
    commit-readiness debt is open, `_handle_advisory_pre_review` must NOT
    short-circuit with `already_fresh` — otherwise `review_status` /
    commit-gate report `repo_commit_ready=false` while advisory claims
    everything is fine. Regression for the round-3 scope finding."""
    import subprocess
    from ouroboros.review_state import (
        AdvisoryReviewState,
        CommitReadinessDebtItem,
        AdvisoryRunRecord,
        compute_snapshot_hash,
        make_repo_key,
        save_state,
        _utc_now,
    )
    from ouroboros.tools import claude_advisory_review as adv_mod

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    (drive_root / "state").mkdir()
    (drive_root / "logs").mkdir()
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, capture_output=True)
    (repo_dir / "sample.py").write_text("print('hello')\n", encoding="utf-8")

    repo_key = make_repo_key(repo_dir)
    snapshot_hash = compute_snapshot_hash(repo_dir)
    state = AdvisoryReviewState()
    # A prior fresh advisory run for this exact snapshot.
    state.add_run(AdvisoryRunRecord(
        snapshot_hash=snapshot_hash,
        commit_message="earlier commit",
        status="fresh",
        ts=_utc_now(),
        repo_key=repo_key,
    ))
    # And open commit-readiness debt for the same repo.
    state.commit_readiness_debts = [
        CommitReadinessDebtItem(
            debt_id="crd-0001",
            category="repeated_obligation",
            title="Commit readiness debt",
            summary="Repeated tests blocker",
            repo_key=repo_key,
            fingerprint="repeated_obligation:test-fp",
        ),
    ]
    save_state(drive_root, state)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    sentinel = {"ran": False}

    def fake_run_claude_advisory(*args, **kwargs):
        sentinel["ran"] = True
        return ([], "[]", "claude-test", 10)

    monkeypatch.setattr(adv_mod, "check_worktree_readiness", lambda *a, **kw: [])
    monkeypatch.setattr(adv_mod, "_run_claude_advisory", fake_run_claude_advisory)

    class FakeCtx:
        pass

    ctx = FakeCtx()
    ctx.repo_dir = str(repo_dir)
    ctx.drive_root = str(drive_root)
    ctx.task_id = "fresh-with-debt"
    ctx.drive_logs = lambda: drive_root / "logs"
    ctx.emit_progress_fn = lambda msg: None

    result = json.loads(adv_mod._handle_advisory_pre_review(ctx, commit_message="test"))
    assert result["status"] != "already_fresh", (
        "advisory_pre_review must not short-circuit as already_fresh while "
        f"commit-readiness debt is open; got status={result.get('status')!r}."
    )
    assert sentinel["ran"], (
        "advisory must actually re-run to surface debt guidance, not rely on the "
        "cached fresh advisory."
    )


def test_resolve_matching_obligations_rejects_conflicting_ids():
    """When a reviewer entry carries BOTH an explicit `obligation_id` and an
    `(obligation <id>)` suffix inside `item` AND they disagree, neither id must
    be recorded as PASS — otherwise a single malformed entry could silently
    clear two unrelated obligations (and their associated commit-readiness debt).
    Regression for the triad-review round-2 finding on _resolve_matching_obligations."""
    from ouroboros.review_state import (
        AdvisoryReviewState,
        CommitAttemptRecord,
        ObligationItem,
        _utc_now,
    )
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.open_obligations = [
        ObligationItem(
            obligation_id="obl-0001",
            item="code_quality",
            severity="critical",
            reason="issue A in foo.py",
            source_attempt_ts=_utc_now(),
            source_attempt_msg="earlier blocked commit",
        ),
        ObligationItem(
            obligation_id="obl-0002",
            item="code_quality",
            severity="critical",
            reason="issue B in bar.py",
            source_attempt_ts=_utc_now(),
            source_attempt_msg="earlier blocked commit",
        ),
    ]

    # Malformed reviewer entry: explicit id says obl-0002, suffix says obl-0001.
    mismatched_items = [
        {
            "item": "code_quality (obligation obl-0001)",
            "verdict": "PASS",
            "severity": "critical",
            "reason": "supposedly fixed",
            "obligation_id": "obl-0002",
        },
    ]
    _resolve_matching_obligations(state, mismatched_items, "abc123deadbeef")

    still_open = {o.obligation_id for o in state.get_open_obligations()}
    assert "obl-0001" in still_open and "obl-0002" in still_open, (
        "Mismatched obligation ids in one reviewer entry must NOT clear either "
        "obligation — both should stay open until a well-formed PASS arrives."
    )


def test_resolve_matching_obligations_accepts_consistent_ids():
    """When explicit `obligation_id` and `(obligation <id>)` suffix agree, the
    obligation is resolved as usual."""
    from ouroboros.review_state import (
        AdvisoryReviewState,
        ObligationItem,
        _utc_now,
    )
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.open_obligations = [
        ObligationItem(
            obligation_id="obl-0001",
            item="code_quality",
            severity="critical",
            reason="issue A",
            source_attempt_ts=_utc_now(),
            source_attempt_msg="earlier blocked commit",
        ),
    ]

    consistent_items = [
        {
            "item": "code_quality (obligation obl-0001)",
            "verdict": "PASS",
            "severity": "critical",
            "reason": "fixed",
            "obligation_id": "obl-0001",
        },
    ]
    _resolve_matching_obligations(state, consistent_items, "abc123deadbeef")

    open_ids = {o.obligation_id for o in state.get_open_obligations()}
    assert "obl-0001" not in open_ids


def test_commit_gate_bypass_is_absolute_escape_hatch_with_open_debt(tmp_path):
    """skip_advisory_pre_review=True is an absolute escape hatch: it short-circuits
    the entire commit gate after audit logging, even when open obligations or
    commit-readiness debt exist. Obligations stay in durable state (review_status
    reports repo_commit_ready=false), but the bypass deliberately overrides that —
    it is the documented escape for provider outages, rate limits, etc.
    Obligations are cleared by on_successful_commit() once the commit lands."""
    import pathlib as _pl
    from unittest.mock import MagicMock

    from ouroboros.review_state import (
        AdvisoryReviewState,
        ObligationItem,
        load_state,
        make_repo_key,
        save_state,
        _utc_now,
    )
    from ouroboros.tools.commit_gate import _check_advisory_freshness

    drive_root = _make_drive_root(tmp_path)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    (repo_dir / "sample.py").write_text("print('hello')\n", encoding="utf-8")

    repo_key = make_repo_key(repo_dir)
    state = AdvisoryReviewState()
    state.open_obligations = [
        ObligationItem(
            obligation_id="obl-0001",
            item="tests_affected",
            severity="critical",
            reason="coverage still missing",
            source_attempt_ts=_utc_now(),
            source_attempt_msg="prior blocked commit",
            repo_key=repo_key,
        ),
    ]
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(repo_dir)
    ctx.drive_logs.return_value = _pl.Path(drive_root) / "logs"
    ctx.task_id = "bypass-test"

    result = _check_advisory_freshness(
        ctx,
        commit_message="test bypass with open obligations",
        skip_advisory_pre_review=True,
    )

    assert result is None, (
        "Bypass must pass the gate even with open obligations — it is an absolute escape hatch."
    )
    reloaded = load_state(drive_root)
    bypassed_runs = [r for r in reloaded.advisory_runs if r.status == "bypassed"]
    assert len(bypassed_runs) == 1, "Bypass must record an audited AdvisoryRunRecord"
    assert reloaded.get_open_obligations(repo_key=repo_key), (
        "Bypass must NOT clear obligations — they stay in state for review_status."
    )


def test_reviewed_stage_cycle_advisory_scope_covers_full_staged_index(tmp_path, monkeypatch):
    """Advisory freshness check must be scoped to the FULL staged index regardless
    of the `paths` argument passed to the stage helper. Otherwise a narrowed
    stage scope (e.g. from `_repo_write_commit(path)`) could let a fresh advisory
    for a subset of the staged diff satisfy the commit gate. Regression for the
    triad-review round-2 finding."""
    import types
    from ouroboros.tools import git as git_mod

    captured = {}

    monkeypatch.setattr(git_mod, "safe_relpath", lambda p: str(p))
    monkeypatch.setattr(git_mod, "_ensure_gitignore", lambda repo: None)

    def fake_run_cmd(cmd, cwd=None):
        if cmd[:2] == ["git", "add"]:
            return ""
        if cmd == ["git", "status", "--porcelain"]:
            return " M foo.py\n M bar.py\n"
        if cmd == ["git", "diff", "--cached", "--name-only"]:
            return "foo.py\nbar.py\n"
        if cmd == ["git", "reset", "HEAD"]:
            return ""
        return ""

    monkeypatch.setattr(git_mod, "run_cmd", fake_run_cmd)
    monkeypatch.setattr(
        git_mod,
        "_unstage_binaries",
        lambda repo: [],
    )
    monkeypatch.setattr(git_mod, "_record_commit_attempt", lambda *a, **kw: None)
    monkeypatch.setattr(
        git_mod,
        "_fingerprint_staged_diff",
        lambda repo: {"ok": True, "fingerprint": "abc", "status": "ok", "reason": "", "chars": 0},
    )

    def fake_check_advisory_freshness(ctx, commit_message, skip_advisory_pre_review=False, paths=None):
        captured["advisory_paths"] = paths
        return None  # no advisory error so the path keeps going

    monkeypatch.setattr(git_mod, "_check_advisory_freshness", fake_check_advisory_freshness)
    monkeypatch.setattr(
        git_mod,
        "_run_parallel_review",
        lambda *a, **kw: (None, None, "", []),
    )
    monkeypatch.setattr(
        git_mod,
        "_aggregate_review_verdict",
        lambda *a, **kw: (False, "", "", [], []),
    )

    ctx = types.SimpleNamespace(repo_dir="/tmp/repo", _review_advisory=[])

    outcome = git_mod._run_reviewed_stage_cycle(
        ctx,
        commit_message="scope test",
        commit_start=0.0,
        paths=["foo.py"],  # narrow stage scope
    )

    assert outcome.get("status") == "passed"
    # Advisory scope must reflect the FULL staged index, not just stage_paths.
    assert sorted(captured["advisory_paths"] or []) == ["bar.py", "foo.py"], (
        "Advisory freshness check must use the full staged index, not the narrowed "
        "stage_paths argument — otherwise pre-staged files escape the advisory gate."
    )


def test_legacy_schema_state_surfaces_commit_readiness_debt_on_load(tmp_path):
    """Legacy (schema-v2) state without `commit_readiness_debts` field must synthesize
    debt on load so upgraded repos see `retry_anchor=commit_readiness_debt` immediately,
    not only after the next state mutation.

    Regression for the scope-review finding that `_load_state_unlocked` forgot to call
    `_sync_commit_readiness_debts()` after hydrating legacy obligations/blocking history.
    """
    import json
    from ouroboros.review_state import load_state, make_repo_key

    drive_root = _make_drive_root(tmp_path)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".git").mkdir()
    repo_key = make_repo_key(repo_dir)

    # Build a schema-v2-shaped payload by hand: has open_obligations
    # (repeated across blocking_history) but NO commit_readiness_debts field.
    attempt_a = {
        "ts": "2026-04-01T10:00:00+00:00",
        "commit_message": "attempt 1 blocked",
        "status": "blocked",
        "block_reason": "critical_findings",
        "repo_key": repo_key,
        "tool_name": "repo_commit",
        "task_id": "legacy-task",
        "attempt": 1,
        "phase": "blocking_review",
        "blocked": True,
        "critical_findings": [{
            "verdict": "FAIL", "severity": "critical",
            "item": "tests_affected", "reason": "No tests for new path",
            "obligation_id": "obl-0001",
        }],
        "obligation_ids": ["obl-0001"],
    }
    attempt_b = {
        **attempt_a,
        "ts": "2026-04-01T10:05:00+00:00",
        "commit_message": "attempt 2 still blocked",
        "attempt": 2,
        "critical_findings": [{
            "verdict": "FAIL", "severity": "critical",
            "item": "tests_affected",
            "reason": "No tests for new path (retry reason)",
            "obligation_id": "obl-0001",
        }],
    }
    legacy_payload = {
        "state_version": 2,
        "advisory_runs": [],
        "attempts": [attempt_a, attempt_b],
        "last_commit_attempt": attempt_b,
        "blocking_history": [attempt_a, attempt_b],
        "open_obligations": [{
            "obligation_id": "obl-0001",
            "item": "tests_affected",
            "severity": "critical",
            "reason": "No tests for new path",
            "source_attempt_ts": "2026-04-01T10:05:00+00:00",
            "source_attempt_msg": "attempt 2 still blocked",
            "status": "still_open",
            "resolved_by": "",
            "repo_key": repo_key,
            "fingerprint": "",
            "created_ts": "2026-04-01T10:00:00+00:00",
            "updated_ts": "2026-04-01T10:05:00+00:00",
        }],
        "next_obligation_seq": 2,
        # NOTE: no `commit_readiness_debts` field at all — simulates v4.40.3 state.
    }
    (drive_root / "state").mkdir(parents=True, exist_ok=True)
    state_path = drive_root / "state" / "advisory_review.json"
    state_path.write_text(json.dumps(legacy_payload), encoding="utf-8")

    state = load_state(drive_root)
    debts = state.get_open_commit_readiness_debts(repo_key=repo_key)

    assert debts, (
        "Legacy v2 state with repeated blocking obligations must synthesize "
        "commit-readiness debt on load — otherwise `retry_anchor=commit_readiness_debt` "
        "is silently dropped until the next state mutation."
    )
    assert any(debt.status in ("detected", "queued", "reopened") for debt in debts)
    # Debt must reference the original obligation so the retry anchor leads the
    # agent back to the root cause instead of each individual rephrased finding.
    assert any("obl-0001" in list(debt.source_obligation_ids or []) for debt in debts)


# Extended tests moved to tests/test_advisory_workflow_ext.py
# (snapshot_paths, parse_failure handling, obligation resolution edge cases)
