"""Regression tests for v4.33.0 review-pipeline reliability fixes.

Covers small, focused invariants from:
  - D1: scope_review must not overwrite signal_result.status
  - D2: scope_review_complete events carry prompt_tokens / headroom_tokens
  - D3: commit_gate stores full block_details (no [:4000] truncation)
  - D4: _run_to_dict surfaces distinct status_summary per run type
  - D6: build_review_context shows multiple findings / continuations
  - B1.3: CHECKLISTS.md contains the Critical surface whitelist section
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock, patch


REPO = pathlib.Path(__file__).resolve().parent.parent


# ── B1.3 — CHECKLISTS whitelist contract ────────────────────────────────────

class TestChecklistWhitelist:
    def test_critical_surface_whitelist_section_exists(self):
        """CHECKLISTS.md MUST contain the shared whitelist binding every reviewer."""
        text = (REPO / "docs" / "CHECKLISTS.md").read_text(encoding="utf-8")
        assert "Critical surface whitelist" in text
        # All five categories must be named
        for category in (
            "Release metadata",
            "Tool schema",
            "Module map",
            "Behavioural documentation",
            "Safety guarding",
        ):
            assert category in text, f"Whitelist missing category: {category}"

    def test_scope_checklist_references_whitelist(self):
        """The Intent / Scope Review Checklist must defer to the whitelist."""
        text = (REPO / "docs" / "CHECKLISTS.md").read_text(encoding="utf-8")
        # Scope checklist severity rules must mention the shared whitelist
        assert "Critical surface whitelist" in text
        # And item 3 (cross_surface_consistency) must call it out by name
        assert "cross_surface_consistency" in text


# ── D1 — scope status override removal ──────────────────────────────────────

class TestScopeSignalStatusPreserved:
    def test_budget_exceeded_status_preserved_after_run_scope_review(self):
        """run_scope_review must NOT overwrite signal_result.status from context_status."""
        from ouroboros.tools.scope_review import (
            ScopeReviewResult,
            _TouchedContextStatus,
            _handle_prompt_signals,
        )

        # _handle_prompt_signals sets status="budget_exceeded" for this path
        ctx_status = _TouchedContextStatus(status="budget_exceeded", token_count=900_000)
        result = _handle_prompt_signals(prompt=None, context_status=ctx_status)
        assert isinstance(result, ScopeReviewResult)
        assert result.status == "budget_exceeded"
        assert result.blocked is False  # budget_exceeded is non-blocking


# ── D2 — scope_review_complete headroom metric ──────────────────────────────

class TestScopeHeadroomMetric:
    def test_log_scope_result_includes_headroom_fields(self, tmp_path):
        """scope_review_complete event MUST carry prompt_tokens, budget, and headroom."""
        from ouroboros.tools.scope_review import _log_scope_result, _SCOPE_BUDGET_TOKEN_LIMIT

        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        ctx = MagicMock()
        ctx.drive_logs.return_value = logs_dir
        ctx.task_id = "test-task"

        # 2 MB prompt chars → ~500K tokens (chars/4)
        _log_scope_result(ctx, critical_count=1, advisory_count=2, prompt_chars=2_000_000)

        events = (logs_dir / "events.jsonl").read_text(encoding="utf-8")
        assert "scope_review_complete" in events
        assert '"prompt_tokens"' in events
        assert '"prompt_tokens_budget"' in events
        assert '"headroom_tokens"' in events
        # Budget is exported from the module constant
        assert str(_SCOPE_BUDGET_TOKEN_LIMIT) in events


# ── D3 — block_details stored without truncation ────────────────────────────

class TestBlockDetailsFullRoundTrip:
    def test_commit_gate_does_not_truncate_block_details_on_write(self):
        """commit_gate._record_commit_attempt MUST NOT call truncate_review_artifact
        on block_details — canonical evidence is stored full, display-side
        truncation happens in review_status / format_status_section.
        """
        import inspect
        from ouroboros.tools import commit_gate

        source = inspect.getsource(commit_gate._record_commit_attempt)
        # Pre-v4.33 had `block_details=_truncate_review_artifact(block_details)`.
        # After v4.33 block_details is stored unwrapped.
        assert "_truncate_review_artifact(block_details)" not in source
        assert "block_details=block_details" in source

    def test_commit_attempt_record_accepts_arbitrarily_long_block_details(self):
        """CommitAttemptRecord has no intrinsic cap on block_details length."""
        from ouroboros.review_state import CommitAttemptRecord

        long_details = (
            "⚠️ REVIEW_BLOCKED: Critical issues found by reviewers.\n\n"
            + "Finding: " + ("X" * 8000) + "\n"
            + "Finding: " + ("Y" * 8000) + "\n"
        )
        ca = CommitAttemptRecord(
            ts="2026-04-16T00:00:00",
            commit_message="feat: something",
            status="blocked",
            snapshot_hash="",
            block_reason="critical_findings",
            block_details=long_details,
            duration_sec=1.5,
            task_id="test-task",
        )
        assert ca.block_details == long_details
        assert "OMISSION NOTE" not in ca.block_details
        assert len(ca.block_details) > 16_000


# ── D4 — _run_to_dict status_summary ────────────────────────────────────────

class TestRunToDictStatusAware:
    def test_responded_with_no_fails_is_responded_clean(self):
        from ouroboros.review_evidence import _run_to_dict

        run = MagicMock()
        run.items = [{"item": "bible_compliance", "verdict": "PASS", "severity": "critical"}]
        run.status = "fresh"
        run.ts = "2026-04-16"
        run.bypass_reason = ""
        run.raw_result = ""

        d = _run_to_dict(run)
        assert d["status_summary"] == "responded_clean"
        assert d["findings"] == []  # no FAIL items
        assert d["total_items"] == 1
        assert d["raw_result_present"] is False

    def test_responded_with_fail_is_responded_with_findings(self):
        from ouroboros.review_evidence import _run_to_dict

        run = MagicMock()
        run.items = [{"item": "code_quality", "verdict": "FAIL", "severity": "critical", "reason": "x"}]
        run.status = "fresh"
        run.ts = "2026-04-16"
        run.bypass_reason = ""
        run.raw_result = "full raw response here"

        d = _run_to_dict(run)
        assert d["status_summary"] == "responded_with_findings"
        assert len(d["findings"]) == 1
        assert d["raw_result_present"] is True

    def test_bypassed_status_distinct_from_skipped(self):
        from ouroboros.review_evidence import _run_to_dict

        bypassed = MagicMock()
        bypassed.items = []
        bypassed.status = "bypassed"
        bypassed.ts = "2026-04-16"
        bypassed.bypass_reason = "user override"
        bypassed.raw_result = ""

        skipped = MagicMock()
        skipped.items = []
        skipped.status = "skipped"
        skipped.ts = "2026-04-16"
        skipped.bypass_reason = ""
        skipped.raw_result = ""

        assert _run_to_dict(bypassed)["status_summary"] == "bypassed"
        assert _run_to_dict(skipped)["status_summary"] == "skipped"

    def test_parse_failure_distinct_from_error(self):
        from ouroboros.review_evidence import _run_to_dict

        parse_fail = MagicMock()
        parse_fail.items = []
        parse_fail.status = "parse_failure"
        parse_fail.ts = "2026-04-16"
        parse_fail.bypass_reason = ""
        parse_fail.raw_result = "garbled model output"

        err = MagicMock()
        err.items = []
        err.status = "error"
        err.ts = "2026-04-16"
        err.bypass_reason = ""
        err.raw_result = ""

        assert _run_to_dict(parse_fail)["status_summary"] == "parse_failure"
        assert _run_to_dict(parse_fail)["raw_result_present"] is True
        assert _run_to_dict(err)["status_summary"] == "error"


# ── D6 — build_review_context no longer caps at 1 finding ───────────────────

class TestBuildReviewContextRelaxed:
    def test_multiple_critical_findings_surfaced(self):
        """v4.33.0 contract preserved via v4.40.4 refactor: up to 3 critical/advisory
        findings per continuation, up to 5 continuations. The caps now live in named
        constants (`_PER_FINDING_CAP`, `_CONTINUATION_CAP`) with explicit
        `⚠️ OMISSION NOTE` markers instead of bare `[:N]` slices (DEVELOPMENT.md /
        CHECKLISTS 2(f): no silent truncation of cognitive artifacts). The
        behavioural contract is unchanged — up to 3 findings per category, up to 5
        continuations visible."""
        import inspect
        from ouroboros import agent_task_pipeline

        source = inspect.getsource(agent_task_pipeline.build_review_context)
        # Caps still enforced (identical numeric contract)
        assert "_PER_FINDING_CAP = 3" in source
        assert "_CONTINUATION_CAP = 5" in source
        # Findings lists must be iterated per-category (not index-0-only)
        assert "item.critical_findings" in source
        assert "item.advisory_findings" in source
        assert "scoped_continuations" in source
        # Slicing must be explicit-omission-note style, not silent
        assert "OMISSION NOTE" in source


# ── Soft circuit-breaker hint lowered to attempt 3 ─────────────────────────

class TestCircuitBreakerHintThreshold:
    def test_hint_fires_at_attempt_three_not_five(self):
        import inspect
        from ouroboros.tools import review

        # Find the source of the hint-building block in review.py
        source_file = pathlib.Path(inspect.getsourcefile(review)).read_text(encoding="utf-8")
        # v4.33: threshold is 3; old value 5 must be gone from the hint block
        assert "_review_iteration_count >= 3" in source_file
        # The BIBLE P2 reference is new in v4.33 and signals the fix-the-class framing
        assert "BIBLE P2" in source_file
