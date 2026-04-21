"""Additional advisory workflow tests: snapshot_paths, parse_failure handling,
and obligation resolution edge cases.

Continued from test_advisory_workflow.py (split for module size compliance).

12. snapshot_paths roundtrip
13. parse_failure status: effective_status correctness
14. _check_advisory_freshness: parse_failure branch
15. _resolve_matching_obligations: edge cases
16. obligation_resolves with unrelated critical FAIL
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers (duplicated from test_advisory_workflow.py for independence)
# ---------------------------------------------------------------------------

def _make_drive_root(tmp_path: pathlib.Path) -> pathlib.Path:
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _make_blocking_attempt(
    commit_message: str = "test commit",
    block_reason: str = "critical_findings",
    critical_findings: list | None = None,
    repo_key: str = "",
):
    from ouroboros.review_state import CommitAttemptRecord, _utc_now
    return CommitAttemptRecord(
        ts=_utc_now(),
        commit_message=commit_message,
        status="blocked",
        block_reason=block_reason,
        block_details="CRITICAL: something",
        duration_sec=5.0,
        repo_key=repo_key,
        critical_findings=critical_findings or [
            {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
             "reason": "No test changes found", "model": "test-model"},
        ],
    )


# ---------------------------------------------------------------------------
# 12. snapshot_paths roundtrip
# ---------------------------------------------------------------------------

def test_snapshot_paths_roundtrip(tmp_path):
    """snapshot_paths must survive save/load cycle via _record_from_dict."""
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, load_state, _utc_now,
    )
    drive_root = _make_drive_root(tmp_path)
    state = AdvisoryReviewState()
    paths = ["ouroboros/tools/git.py", "tests/test_git.py"]
    state.runs.append(AdvisoryRunRecord(
        snapshot_hash="abc123", commit_message="test", status="fresh",
        ts=_utc_now(), snapshot_paths=paths,
    ))
    save_state(drive_root, state)

    loaded = load_state(drive_root)
    assert loaded.runs, "Run should be present after load"
    assert loaded.runs[0].snapshot_paths == paths, (
        "snapshot_paths must round-trip through save/load"
    )


def test_snapshot_paths_none_roundtrip(tmp_path):
    """snapshot_paths=None (whole-repo scope) must also survive save/load."""
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, load_state, _utc_now,
    )
    drive_root = _make_drive_root(tmp_path)
    state = AdvisoryReviewState()
    state.runs.append(AdvisoryRunRecord(
        snapshot_hash="abc123", commit_message="test", status="fresh",
        ts=_utc_now(), snapshot_paths=None,
    ))
    save_state(drive_root, state)

    loaded = load_state(drive_root)
    assert loaded.runs[0].snapshot_paths is None


# ---------------------------------------------------------------------------
# 13. parse_failure: effective_status must reflect the real run status, not "stale"
# ---------------------------------------------------------------------------

def test_review_status_parse_failure_reflected_in_effective_status(tmp_path):
    """When the matching run for the current hash has status=parse_failure,
    latest_advisory_status must be 'parse_failure', NOT 'stale'."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, _utc_now,
        compute_snapshot_hash,
    )

    # Compute the real current hash for tmp_path as repo_dir
    current_hash = compute_snapshot_hash(tmp_path, "")

    state = AdvisoryReviewState()
    # A run that matches the current hash but has status parse_failure
    state.runs.append(AdvisoryRunRecord(
        snapshot_hash=current_hash,
        commit_message="v1.0.0: test",
        status="parse_failure",
        ts=_utc_now(),
    ))
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(tmp_path)

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    # The status should reflect the actual parse_failure, not "stale"
    assert result["latest_advisory_status"] == "parse_failure", (
        f"Expected 'parse_failure', got '{result['latest_advisory_status']}'"
    )
    # And the message should not say "stale"
    assert "stale" not in result.get("message", "").lower() or "parse_failure" in result.get("message", "").lower()


def test_review_status_defaults_to_current_repo_scope(tmp_path):
    """review_status without explicit repo_key must report only the current repo's obligations."""
    drive_root = _make_drive_root(tmp_path / "drive")
    from ouroboros.review_state import AdvisoryReviewState, save_state

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    repo_a.mkdir()
    repo_b.mkdir()

    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(repo_key=str(repo_a)))
    state.add_blocking_attempt(_make_blocking_attempt(
        repo_key=str(repo_b),
        critical_findings=[{
            "verdict": "FAIL",
            "severity": "critical",
            "item": "self_consistency",
            "reason": "repo b issue",
        }],
    ))
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(repo_b)

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    assert result["filters"]["repo_key"] == str(repo_b)
    open_items = [entry["item"] for entry in result["open_obligations"]]
    assert open_items == ["self_consistency"]


# ---------------------------------------------------------------------------
# 14. _check_advisory_freshness: parse_failure for current snapshot
# ---------------------------------------------------------------------------

def test_check_advisory_freshness_parse_failure_branch(tmp_path):
    """When the matching advisory run for the current snapshot is parse_failure,
    _check_advisory_freshness must report parse_failure guidance, NOT 'snapshot changed'."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, _utc_now,
        compute_snapshot_hash,
    )

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    current_hash = compute_snapshot_hash(repo_dir, "v1.0.0: test")

    state = AdvisoryReviewState()
    state.runs.append(AdvisoryRunRecord(
        snapshot_hash=current_hash,
        commit_message="v1.0.0: test",
        status="parse_failure",
        ts=_utc_now(),
    ))
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(repo_dir)

    from ouroboros.tools.commit_gate import _check_advisory_freshness
    result = _check_advisory_freshness(ctx, "v1.0.0: test")

    assert result is not None, "Expected a freshness error for parse_failure run"
    assert "parse_failure" in result.lower(), (
        f"Expected 'parse_failure' in message, got: {result!r}"
    )
    # Must NOT say "snapshot changed" — that would be a misdiagnosis
    assert "snapshot changed" not in result.lower(), (
        f"parse_failure gate should not say 'snapshot changed', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# 15. _resolve_matching_obligations: edge cases
# ---------------------------------------------------------------------------

def test_resolve_matching_obligations_contradictory_entries_leave_open(tmp_path):
    """If advisory emits both PASS and FAIL for the same item, the obligation must
    remain open (not resolved) — only unambiguous PASS resolves an obligation."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState, save_state
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No tests", "model": "m"},
    ]))
    assert len(state.get_open_obligations()) == 1

    # Advisory emits both PASS and FAIL for tests_affected (contradictory)
    contradictory_items = [
        {"verdict": "PASS", "severity": "critical", "item": "tests_affected", "reason": "Tests found"},
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected", "reason": "Missing test X"},
    ]
    _resolve_matching_obligations(state, contradictory_items, snapshot_hash="deadbeef")

    # Obligation must remain open — contradictory entries should not resolve
    open_obs = state.get_open_obligations()
    assert len(open_obs) == 1, (
        f"Obligation should remain open with contradictory PASS+FAIL, got: {open_obs}"
    )


def test_handle_review_status_old_parse_failure_different_hash_reports_stale(tmp_path):
    """latest advisory is parse_failure on OLD snapshot; current worktree has different hash.
    _handle_review_status must report stale/re-run, NOT current-snapshot parse_failure guidance."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, save_state, _utc_now,
    )

    repo_dir = tmp_path / "repo_stale_pf"
    repo_dir.mkdir()

    # Seed state: parse_failure on a hash that does NOT match the current repo_dir
    state = AdvisoryReviewState()
    state.add_run(AdvisoryRunRecord(
        snapshot_hash="definitely_old_hash_not_current",
        commit_message="old commit",
        status="parse_failure",
        ts=_utc_now(),
    ))
    save_state(drive_root, state)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(repo_dir)

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    next_step = result.get("next_step", "")
    # Must NOT say "for the current snapshot" (parse_failure guidance for current snapshot)
    assert "for the current snapshot" not in next_step, (
        f"Should not emit current-snapshot parse_failure guidance for old hash, got: {next_step!r}"
    )
    # Must say advisory is stale / needs re-run
    assert any(word in next_step.lower() for word in ("stale", "re-run", "rerun", "advisory")), (
        f"Expected stale/re-run guidance for old parse_failure, got: {next_step!r}"
    )


def test_review_status_parse_failure_after_edit_reports_parse_failure_not_stale(tmp_path):
    """Sequence: fresh advisory -> edit -> advisory parse_failure for same new snapshot.
    review_status must report parse_failure guidance, NOT 'invalidated by edit'."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import (
        AdvisoryReviewState, AdvisoryRunRecord, load_state, save_state, _utc_now,
        compute_snapshot_hash, mark_advisory_stale_after_edit,
    )

    repo_dir = tmp_path / "repo2"
    repo_dir.mkdir()

    # Step 1: fresh advisory on old snapshot
    old_hash = "aabbccdd00001111"
    state = AdvisoryReviewState()
    state.add_run(AdvisoryRunRecord(
        snapshot_hash=old_hash, commit_message="v1", status="fresh", ts=_utc_now()
    ))
    save_state(drive_root, state)

    # Step 2: edit invalidates advisory
    mark_advisory_stale_after_edit(drive_root)

    # Step 3: advisory runs on new snapshot but parse_failure
    new_hash = compute_snapshot_hash(repo_dir, "v1")
    state2 = load_state(drive_root)
    state2.add_run(AdvisoryRunRecord(
        snapshot_hash=new_hash, commit_message="v1", status="parse_failure", ts=_utc_now()
    ))
    save_state(drive_root, state2)

    ctx = MagicMock()
    ctx.drive_root = str(drive_root)
    ctx.repo_dir = str(repo_dir)

    from ouroboros.tools.claude_advisory_review import _handle_review_status
    result = json.loads(_handle_review_status(ctx))

    # Must report parse_failure, NOT stale-from-edit
    assert result["latest_advisory_status"] == "parse_failure", (
        f"Expected parse_failure, got {result['latest_advisory_status']!r}"
    )
    next_step = result.get("next_step", "")
    assert "invalidated" not in next_step.lower(), (
        f"next_step should not say 'invalidated by edit' after parse_failure run, got: {next_step!r}"
    )


def test_next_step_guidance_stale_parse_failure_reports_stale_not_parse_failure(tmp_path):
    """If the latest advisory run was parse_failure on an OLD snapshot and the worktree
    has since changed (stale_from_edit=True), _next_step_guidance must say the advisory
    is stale/invalidated — NOT that there was a parse_failure."""
    from ouroboros.tools.claude_advisory_review import _next_step_guidance
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, _utc_now

    state = AdvisoryReviewState()
    old_run = AdvisoryRunRecord(
        snapshot_hash="olddeadbeef",  # ← different from current hash
        commit_message="old commit",
        status="parse_failure",
        ts=_utc_now(),
    )
    state.runs.append(old_run)

    guidance = _next_step_guidance(
        latest=old_run,
        state=state,
        stale_from_edit=True,          # worktree was edited AFTER parse_failure
        stale_from_edit_ts="2026-04-05T10:00",
        open_obs=[],
        open_debts=[],
        effective_is_fresh=False,
    )

    # Must mention stale/invalidated, NOT mislead with parse_failure-specific guidance
    assert "invalidated" in guidance.lower() or "stale" in guidance.lower(), (
        f"Expected stale guidance for old parse_failure run, got: {guidance!r}"
    )
    # The guidance should NOT suggest the current snapshot has a parse_failure
    # (it's a different snapshot — user just needs to re-run advisory)
    assert "for the current snapshot" not in guidance, (
        f"Should not say 'for the current snapshot', got: {guidance!r}"
    )


def test_next_step_guidance_stale_with_open_obligations_requires_reaudit(tmp_path):
    """Even when advisory is stale, open obligations should still trigger re-audit guidance."""
    from ouroboros.tools.claude_advisory_review import _next_step_guidance
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, ObligationItem, _utc_now

    state = AdvisoryReviewState()
    old_run = AdvisoryRunRecord(
        snapshot_hash="olddeadbeef",
        commit_message="old commit",
        status="fresh",
        ts=_utc_now(),
    )
    state.runs.append(old_run)
    open_obs = [ObligationItem(
        obligation_id="ob-1",
        item="code_quality",
        severity="critical",
        reason="Need broader fix",
        source_attempt_ts=_utc_now(),
        source_attempt_msg="blocked",
        repo_key="repo",
    )]

    guidance = _next_step_guidance(
        latest=old_run,
        state=state,
        stale_from_edit=True,
        stale_from_edit_ts="2026-04-05T10:00",
        open_obs=open_obs,
        open_debts=[],
        effective_is_fresh=False,
    )

    lowered = guidance.lower()
    assert "invalidated" in lowered or "stale" in lowered
    assert "re-read the full diff" in lowered
    assert "group obligations by root cause" in lowered
    assert "rewrite the plan" in lowered


def test_next_step_guidance_parse_failure_with_open_obligations_preserves_parse_failure():
    """Current-snapshot parse_failure plus open obligations must preserve the parse_failure diagnosis."""
    from ouroboros.tools.claude_advisory_review import _next_step_guidance
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, ObligationItem, _utc_now

    state = AdvisoryReviewState()
    parse_run = AdvisoryRunRecord(
        snapshot_hash="currenthash",
        commit_message="current commit",
        status="parse_failure",
        ts=_utc_now(),
    )
    state.runs.append(parse_run)
    open_obs = [ObligationItem(
        obligation_id="ob-2",
        item="code_quality",
        severity="critical",
        reason="Need broader fix",
        source_attempt_ts=_utc_now(),
        source_attempt_msg="blocked",
        repo_key="repo",
    )]

    guidance = _next_step_guidance(
        latest=parse_run,
        state=state,
        stale_from_edit=False,
        stale_from_edit_ts=None,
        open_obs=open_obs,
        open_debts=[],
        effective_is_fresh=False,
    )

    lowered = guidance.lower()
    assert "parse_failure" in lowered
    assert "re-read the full diff" in lowered
    assert "group obligations by root cause" in lowered
    assert "rewrite the plan" in lowered


def test_next_step_guidance_fresh_with_only_open_debts_mentions_debt_not_zero_obligations():
    """Debt-only guidance should describe unresolved debt directly."""
    from ouroboros.tools.claude_advisory_review import _next_step_guidance
    from ouroboros.review_state import AdvisoryReviewState, AdvisoryRunRecord, CommitReadinessDebtItem, _utc_now

    state = AdvisoryReviewState()
    fresh_run = AdvisoryRunRecord(
        snapshot_hash="currenthash",
        commit_message="current commit",
        status="fresh",
        ts=_utc_now(),
    )
    state.runs.append(fresh_run)
    open_debts = [CommitReadinessDebtItem(
        debt_id="crd-0001",
        category="readiness_warning",
        title="Readiness warning debt",
        summary="Manual verification still required before commit.",
        severity="warning",
        repo_key="repo",
        fingerprint="readiness_warning:attempt:abc",
        source="review_state",
        source_obligation_ids=[],
        status="detected",
    )]

    guidance = _next_step_guidance(
        latest=fresh_run,
        state=state,
        stale_from_edit=False,
        stale_from_edit_ts=None,
        open_obs=[],
        open_debts=open_debts,
        effective_is_fresh=True,
    )

    lowered = guidance.lower()
    assert "commit-readiness debt item" in lowered
    assert "repo_commit will be blocked" in lowered
    assert "0 open obligation" not in lowered


# ---------------------------------------------------------------------------
# 16. obligation_resolves with unrelated critical FAIL
# ---------------------------------------------------------------------------

def test_obligation_resolves_even_when_advisory_has_unrelated_critical_fail(tmp_path):
    """Obligation for item X must resolve when advisory emits unambiguous PASS for X,
    even if the same advisory result contains a CRITICAL FAIL for an unrelated item Y."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState, save_state
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    # Open obligation for tests_affected
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No tests provided", "model": "m"},
    ]))
    assert len(state.get_open_obligations()) == 1

    # Advisory: PASS for tests_affected (obligation item), FAIL for unrelated code_quality
    mixed_items = [
        {"verdict": "PASS", "severity": "critical", "item": "tests_affected",
         "reason": "Tests now present"},
        {"verdict": "FAIL", "severity": "critical", "item": "code_quality",
         "reason": "Some unrelated bug"},
    ]
    _resolve_matching_obligations(state, mixed_items, snapshot_hash="deadbeef2")

    # tests_affected obligation should be resolved even though code_quality failed
    open_obs = state.get_open_obligations()
    assert len(open_obs) == 0, (
        f"tests_affected obligation should be resolved by unambiguous PASS, "
        f"even with unrelated code_quality FAIL. Got open: {open_obs}"
    )


def test_resolve_matching_obligations_unambiguous_pass_resolves(tmp_path):
    """Unambiguous PASS (no FAIL for same item) must resolve the obligation."""
    drive_root = _make_drive_root(tmp_path)
    from ouroboros.review_state import AdvisoryReviewState
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No tests", "model": "m"},
    ]))
    assert len(state.get_open_obligations()) == 1

    # Only PASS for tests_affected — unambiguous
    _resolve_matching_obligations(
        state,
        [{"verdict": "PASS", "severity": "critical", "item": "tests_affected", "reason": "Tests present"}],
        snapshot_hash="deadbeef",
    )

    open_obs = state.get_open_obligations()
    assert len(open_obs) == 0, f"Obligation should be resolved by unambiguous PASS, got: {open_obs}"


def test_resolve_matching_obligations_suffix_id_resolves_same_repo(tmp_path):
    """PASS items suffixed with `(obligation <id>)` must resolve the matching obligation."""
    from ouroboros.review_state import AdvisoryReviewState
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(
        repo_key="repo-a",
        critical_findings=[{
            "verdict": "FAIL",
            "severity": "critical",
            "item": "self_consistency",
            "reason": "README and docs drift",
        }],
    ))
    open_obs = state.get_open_obligations(repo_key="repo-a")
    assert len(open_obs) == 1
    obligation = open_obs[0]

    _resolve_matching_obligations(
        state,
        [{
            "verdict": "PASS",
            "severity": "critical",
            "item": f"self_consistency (obligation {obligation.obligation_id})",
            "reason": "README and docs are now aligned",
        }],
        snapshot_hash="feedbeef",
        repo_key="repo-a",
    )

    assert state.get_open_obligations(repo_key="repo-a") == []


def test_resolve_matching_obligations_does_not_cross_repo_boundaries(tmp_path):
    """Resolving repo B must not close an obligation in repo A with the same item name."""
    from ouroboros.review_state import AdvisoryReviewState
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(repo_key="repo-a"))
    state.add_blocking_attempt(_make_blocking_attempt(repo_key="repo-b"))

    _resolve_matching_obligations(
        state,
        [{
            "verdict": "PASS",
            "severity": "critical",
            "item": "tests_affected",
            "reason": "Repo B now has tests",
        }],
        snapshot_hash="cafefeed",
        repo_key="repo-b",
    )

    assert len(state.get_open_obligations(repo_key="repo-a")) == 1
    assert len(state.get_open_obligations(repo_key="repo-b")) == 0


# ---------------------------------------------------------------------------
# 17. compute_snapshot_hash: empty paths normalized to None (whole-repo scope)
# ---------------------------------------------------------------------------

def test_compute_snapshot_hash_empty_paths_equals_whole_repo(tmp_path):
    """paths=[] must be normalized to None (whole-repo scope).

    An empty-paths advisory must NOT produce a hash that represents
    'no changed files', which would give a trivially-fresh gate bypass.
    """
    from ouroboros.review_state import compute_snapshot_hash

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / "a.py").write_text("print('hello')")

    hash_none = compute_snapshot_hash(repo_dir, "")
    hash_empty = compute_snapshot_hash(repo_dir, "", paths=[])
    # Both should produce the same hash — empty list is treated as whole-repo
    assert hash_none == hash_empty, (
        f"paths=[] should equal paths=None (whole-repo), got: {hash_empty!r} vs {hash_none!r}"
    )


# ---------------------------------------------------------------------------
# 18. truncate_review_artifact: None input must not raise TypeError
# ---------------------------------------------------------------------------

def test_truncate_review_artifact_handles_none():
    """truncate_review_artifact(None) must return '' without raising TypeError."""
    from ouroboros.utils import truncate_review_artifact, truncate_review_reason

    result = truncate_review_artifact(None)
    assert result == "", f"Expected empty string for None input, got {result!r}"

    result2 = truncate_review_reason(None)
    assert result2 == "", f"Expected empty string for None reason, got {result2!r}"


def test_format_status_section_null_reason_does_not_crash(tmp_path):
    """format_status_section must not crash when an obligation has reason=null/None."""
    from ouroboros.review_state import AdvisoryReviewState, format_status_section
    from ouroboros.review_state import CommitAttemptRecord, _utc_now

    state = AdvisoryReviewState()
    # Simulate a commit attempt where a reviewer emitted {"reason": null}
    blocking_attempt = CommitAttemptRecord(
        ts=_utc_now(),
        commit_message="v1.0: test",
        status="blocked",
        block_reason="critical_findings",
        block_details="CRITICAL: something",
        duration_sec=5.0,
        critical_findings=[
            {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
             "reason": None, "model": "test-model"},  # null reason
        ],
    )
    state.add_blocking_attempt(blocking_attempt)

    # Must not raise TypeError
    section = format_status_section(state)
    assert "tests_affected" in section


def test_format_status_section_repo_scopes_history_and_obligations(tmp_path):
    from ouroboros.review_state import (
        AdvisoryReviewState,
        AdvisoryRunRecord,
        CommitAttemptRecord,
        compute_snapshot_hash,
        format_status_section,
        make_repo_key,
    )

    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    (repo_a / ".git").mkdir(parents=True)
    (repo_b / ".git").mkdir(parents=True)
    (repo_a / "tracked.py").write_text("print('repo a')\n", encoding="utf-8")
    (repo_b / "tracked.py").write_text("print('repo b')\n", encoding="utf-8")

    repo_a_key = make_repo_key(repo_a)
    repo_b_key = make_repo_key(repo_b)
    state = AdvisoryReviewState()
    state.add_run(AdvisoryRunRecord(
        snapshot_hash=compute_snapshot_hash(repo_a),
        commit_message="repo-a fresh",
        status="fresh",
        ts="2026-04-07T10:00:00+00:00",
        repo_key=repo_a_key,
    ))
    state.record_attempt(CommitAttemptRecord(
        ts="2026-04-07T10:01:00+00:00",
        commit_message="repo-b blocked",
        status="blocked",
        repo_key=repo_b_key,
        tool_name="repo_commit",
        task_id="task-b",
        attempt=1,
        block_reason="critical_findings",
        critical_findings=[{
            "item": "foreign_issue",
            "reason": "other repo only",
            "severity": "critical",
            "verdict": "FAIL",
        }],
    ))

    section = format_status_section(state, repo_dir=repo_a)
    assert "repo-a fresh" in section
    assert "repo-b blocked" not in section
    assert "foreign_issue" not in section


# ---------------------------------------------------------------------------
# 20. _resolve_matching_obligations: per-finding fingerprint keying
# ---------------------------------------------------------------------------

def test_resolve_item_pass_does_not_clear_other_same_item_obligation(tmp_path):
    """With multiple open obligations for the same item, a generic item-name PASS
    must NOT clear all of them — only an explicit obligation_id PASS may target
    a specific one.  Item-name fallback applies only when exactly one obligation
    exists for that item."""
    from ouroboros.review_state import AdvisoryReviewState, ObligationItem
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    # Simulate a legacy/migrated state with two open obligations under the same checklist item.
    state.open_obligations.extend([
        ObligationItem(
            obligation_id="obl-0001",
            fingerprint="finding:legacy-a",
            item="code_quality",
            severity="critical",
            reason="Bug in foo.py line 42",
            source_attempt_ts="2026-04-18T00:00:00Z",
            source_attempt_msg="legacy attempt 1",
        ),
        ObligationItem(
            obligation_id="obl-0002",
            fingerprint="finding:legacy-b",
            item="code_quality",
            severity="critical",
            reason="Race condition in bar.py",
            source_attempt_ts="2026-04-18T00:01:00Z",
            source_attempt_msg="legacy attempt 2",
        ),
    ])
    open_obs = state.get_open_obligations()
    assert len(open_obs) == 2, f"Expected 2 obligations, got {len(open_obs)}"

    # Generic item-name PASS for code_quality — must NOT clear both obligations
    _resolve_matching_obligations(
        state,
        [{"verdict": "PASS", "severity": "critical", "item": "code_quality",
          "reason": "Fixed foo.py"}],
        snapshot_hash="aabbcc",
    )
    # Still 2 open — item-name fallback blocked because item_open_count > 1
    still_open = state.get_open_obligations()
    assert len(still_open) == 2, (
        f"Generic item-name PASS must not clear multiple same-item obligations; "
        f"got {len(still_open)} open: {[o.reason for o in still_open]}"
    )


def test_resolve_obligation_id_pass_clears_only_targeted_obligation(tmp_path):
    """Explicit obligation_id in the advisory PASS clears only that specific obligation."""
    from ouroboros.review_state import AdvisoryReviewState, ObligationItem
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.open_obligations.extend([
        ObligationItem(
            obligation_id="obl-0101",
            fingerprint="finding:legacy-1",
            item="code_quality",
            severity="critical",
            reason="Bug in foo.py line 42",
            source_attempt_ts="2026-04-18T00:00:00Z",
            source_attempt_msg="legacy attempt 1",
        ),
        ObligationItem(
            obligation_id="obl-0102",
            fingerprint="finding:legacy-2",
            item="code_quality",
            severity="critical",
            reason="Race condition in bar.py",
            source_attempt_ts="2026-04-18T00:01:00Z",
            source_attempt_msg="legacy attempt 2",
        ),
    ])
    open_obs = state.get_open_obligations()
    assert len(open_obs) == 2
    target = open_obs[0]

    # PASS with explicit obligation_id for just the first obligation
    _resolve_matching_obligations(
        state,
        [{"verdict": "PASS", "severity": "critical",
          "item": f"code_quality (obligation {target.obligation_id})",
          "reason": "foo.py fixed"}],
        snapshot_hash="deadf00d",
    )
    still_open = state.get_open_obligations()
    assert len(still_open) == 1, f"Only the targeted obligation should be cleared"
    assert still_open[0].obligation_id != target.obligation_id


def test_resolve_item_pass_clears_single_obligation_legacy_fallback(tmp_path):
    """When exactly one open obligation exists for an item, item-name PASS still
    resolves it (legacy fallback for single-obligation cases)."""
    from ouroboros.review_state import AdvisoryReviewState
    from ouroboros.tools.claude_advisory_review import _resolve_matching_obligations

    state = AdvisoryReviewState()
    state.add_blocking_attempt(_make_blocking_attempt(critical_findings=[
        {"verdict": "FAIL", "severity": "critical", "item": "tests_affected",
         "reason": "No tests staged"},
    ]))
    assert len(state.get_open_obligations()) == 1

    _resolve_matching_obligations(
        state,
        [{"verdict": "PASS", "severity": "critical", "item": "tests_affected",
          "reason": "Tests present now"}],
        snapshot_hash="c0ffeee0",
    )
    assert state.get_open_obligations() == []
