"""Block 3 tests: scope reviewer role expansion (TestScopeChecklist, TestScopeReviewerPrompt, TestScopeBudgetGate, TestBudgetStatusHandling, TestTouchedOmissionPrecedence)."""

import importlib
import inspect
import json
import os
import pathlib
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_module(name):
    sys.path.insert(0, REPO)
    return importlib.import_module(name)


# ---------------------------------------------------------------------------
# Block 3: scope reviewer role expansion tests
# ---------------------------------------------------------------------------

class TestScopeChecklist:
    """Verify new checklist items are present in docs/CHECKLISTS.md."""

    def test_cross_module_bugs_item_present(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.load_checklist_section("Intent / Scope Review Checklist")
        assert "cross_module_bugs" in section

    def test_implicit_contracts_item_present(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.load_checklist_section("Intent / Scope Review Checklist")
        assert "implicit_contracts" in section

    def test_section_description_no_longer_says_supplemental(self):
        """Scope reviewer is now a full-codebase reviewer, not 'supplemental'."""
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.load_checklist_section("Intent / Scope Review Checklist")
        # Description line should reference full-codebase role
        assert "full-codebase" in section or "ENTIRE repository" in section

    def test_checklist_has_eight_items(self):
        mod = _get_module("ouroboros.tools.review_helpers")
        section = mod.load_checklist_section("Intent / Scope Review Checklist")
        # Count rows that start with "| <number>"
        import re
        rows = re.findall(r"^\|\s*\d+\s*\|", section, re.MULTILINE)
        assert len(rows) == 8, f"Expected 8 checklist items, found {len(rows)}"


class TestScopeReviewerPrompt:
    """Verify the scope reviewer prompt emphasises whole-codebase role."""

    def _get_source(self):
        mod = _get_module("ouroboros.tools.scope_review")
        return inspect.getsource(mod._build_scope_prompt)

    def test_prompt_mentions_entire_codebase(self):
        src = self._get_source()
        assert "ENTIRE codebase" in src or "entire codebase" in src

    def test_prompt_mentions_cross_module_bugs(self):
        src = self._get_source()
        assert "Cross-module bugs" in src or "cross-module" in src.lower()

    def test_prompt_mentions_implicit_contracts(self):
        src = self._get_source()
        assert "implicit contract" in src.lower() or "implicit_contract" in src.lower()

    def test_prompt_mentions_hidden_regressions(self):
        src = self._get_source()
        assert "Hidden regression" in src or "hidden regression" in src.lower()

    def test_prompt_does_not_say_supplemental(self):
        """Old phrasing 'supplemental blocking scope reviewer' should be gone from role section."""
        src = self._get_source()
        # The old text was: "You are the supplemental blocking scope reviewer"
        assert "You are the supplemental blocking scope reviewer" not in src

    def test_module_docstring_describes_full_codebase_role(self):
        mod = _get_module("ouroboros.tools.scope_review")
        doc = mod.__doc__ or ""
        # Docstring must reflect the full-codebase reviewer role
        assert "cross-module" in doc.lower() or "entire repository" in doc.lower() or "full-codebase" in doc.lower()


class TestScopeBudgetGate:
    """Verify the budget gate skips scope review non-blockingly when the full prompt is too large."""

    def test_budget_constant_is_reasonable(self):
        mod = _get_module("ouroboros.tools.scope_review")
        assert hasattr(mod, "_SCOPE_BUDGET_TOKEN_LIMIT")
        # Should be between 500K and 2M — big enough to be useful, small enough to be safe
        assert 500_000 <= mod._SCOPE_BUDGET_TOKEN_LIMIT <= 2_000_000

    def test_build_scope_prompt_returns_none_when_budget_exceeded(self, tmp_path):
        """When full prompt tokens exceed budget, _build_scope_prompt returns (None, _TouchedContextStatus)."""
        import subprocess as sp
        # Minimal git repo
        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        f.write_text("x = 2\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)

        mod = _get_module("ouroboros.tools.scope_review")

        # Patch _SCOPE_BUDGET_TOKEN_LIMIT to 1 so any assembled prompt exceeds it
        original = mod._SCOPE_BUDGET_TOKEN_LIMIT
        mod._SCOPE_BUDGET_TOKEN_LIMIT = 1
        try:
            prompt, context_status = mod._build_scope_prompt(tmp_path, "test commit")
        finally:
            mod._SCOPE_BUDGET_TOKEN_LIMIT = original

        assert prompt is None
        assert context_status is not None
        assert context_status.status == "budget_exceeded"
        assert context_status.token_count > 0

    def test_build_scope_prompt_budget_counts_full_prompt_not_just_repo_pack(self, tmp_path):
        """A small repo pack must still skip when the assembled prompt exceeds budget."""
        import subprocess as sp
        from unittest.mock import patch

        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        f.write_text("x = '" + ("A" * 800) + "'\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)

        mod = _get_module("ouroboros.tools.scope_review")

        original = mod._SCOPE_BUDGET_TOKEN_LIMIT
        mod._SCOPE_BUDGET_TOKEN_LIMIT = 100
        try:
            with patch.object(mod, "_gather_scope_packs", return_value="tiny"):
                prompt, context_status = mod._build_scope_prompt(tmp_path, "test commit")
        finally:
            mod._SCOPE_BUDGET_TOKEN_LIMIT = original

        assert prompt is None
        assert context_status is not None
        assert context_status.status == "budget_exceeded"
        assert context_status.token_count > mod.estimate_tokens("tiny")

    def test_run_scope_review_non_blocking_on_budget_exceeded(self, tmp_path):
        """run_scope_review returns non-blocked advisory result when budget exceeded."""
        import subprocess as sp
        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        f.write_text("x = 2\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)

        mod = _get_module("ouroboros.tools.scope_review")

        class FakeCtx:
            repo_dir = str(tmp_path)
            task_id = "test"
            event_queue = None
            def drive_logs(self): return tmp_path

        original = mod._SCOPE_BUDGET_TOKEN_LIMIT
        mod._SCOPE_BUDGET_TOKEN_LIMIT = 1
        try:
            result = mod.run_scope_review(FakeCtx(), "test commit")
        finally:
            mod._SCOPE_BUDGET_TOKEN_LIMIT = original

        assert not result.blocked
        assert result.block_message == ""
        assert len(result.advisory_findings) == 1
        finding = result.advisory_findings[0]
        assert finding["item"] == "scope_review_skipped"
        assert "SCOPE_REVIEW_SKIPPED" in finding["reason"]
        assert "tokens" in finding["reason"]

    def test_budget_exceeded_token_count_in_reason(self, tmp_path):
        """The advisory finding reason includes the actual token count."""
        import subprocess as sp
        sp.run(["git", "init"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.name", "T"], cwd=tmp_path, capture_output=True)
        f = tmp_path / "a.py"
        f.write_text("x = 1\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
        sp.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
        f.write_text("x = 2\n")
        sp.run(["git", "add", "."], cwd=tmp_path, capture_output=True)

        mod = _get_module("ouroboros.tools.scope_review")

        class FakeCtx:
            repo_dir = str(tmp_path)
            task_id = "test"
            event_queue = None
            def drive_logs(self): return tmp_path

        original = mod._SCOPE_BUDGET_TOKEN_LIMIT
        mod._SCOPE_BUDGET_TOKEN_LIMIT = 1
        try:
            result = mod.run_scope_review(FakeCtx(), "test commit")
        finally:
            mod._SCOPE_BUDGET_TOKEN_LIMIT = original

        reason = result.advisory_findings[0]["reason"]
        # Should contain a numeric token count > 0 (not "~0 tokens" from parse failure)
        import re
        match = re.search(r"~?(\d+)\s*tokens", reason)
        assert match, f"No token count in: {reason}"
        assert int(match.group(1)) > 0, f"Token count parsed as 0 (parse failure): {reason}"


class TestBudgetStatusHandling:
    """Unit tests for _TouchedContextStatus budget_exceeded path.

    _parse_budget_sentinel was removed when the API moved from magic strings
    to _TouchedContextStatus dataclass. These tests verify _handle_prompt_signals
    correctly processes structured budget status objects.
    """

    def setup_method(self):
        sys.path.insert(0, REPO)
        self.mod = importlib.import_module("ouroboros.tools.scope_review")

    def _make_budget_status(self, token_count):
        return self.mod._TouchedContextStatus(
            status="budget_exceeded",
            token_count=token_count,
        )

    def test_budget_exceeded_large_token_count(self):
        """Large token count is correctly surfaced in advisory finding reason."""
        status = self._make_budget_status(800000)
        result = self.mod._handle_prompt_signals(None, status)
        assert result is not None
        assert not result.blocked
        assert len(result.advisory_findings) == 1
        reason = result.advisory_findings[0]["reason"]
        assert "800000" in reason

    def test_budget_exceeded_million_token_count(self):
        """Token counts > 1M are surfaced correctly."""
        status = self._make_budget_status(1234567)
        result = self.mod._handle_prompt_signals(None, status)
        assert not result.blocked
        assert "1234567" in result.advisory_findings[0]["reason"]

    def test_budget_exceeded_single_digit(self):
        """Single-digit token counts (edge case) are surfaced without error."""
        status = self._make_budget_status(1)
        result = self.mod._handle_prompt_signals(None, status)
        assert not result.blocked
        assert "1" in result.advisory_findings[0]["reason"]

    def test_budget_exceeded_zero_token_count(self):
        """Zero token count (malformed estimation) is handled without crashing."""
        status = self._make_budget_status(0)
        result = self.mod._handle_prompt_signals(None, status)
        assert result is not None
        assert not result.blocked

    def test_budget_exceeded_finding_item_name(self):
        """Advisory finding uses 'scope_review_skipped' item name."""
        status = self._make_budget_status(900000)
        result = self.mod._handle_prompt_signals(None, status)
        assert result.advisory_findings[0]["item"] == "scope_review_skipped"

    def test_budget_exceeded_finding_severity(self):
        """Advisory finding severity is 'advisory', not 'critical'."""
        status = self._make_budget_status(900000)
        result = self.mod._handle_prompt_signals(None, status)
        assert result.advisory_findings[0]["severity"] == "advisory"

    def test_handle_prompt_signals_budget_exceeded_contains_limit(self):
        """Advisory reason includes the configured budget limit token count."""
        status = self._make_budget_status(900000)
        result = self.mod._handle_prompt_signals(None, status)
        reason = result.advisory_findings[0]["reason"]
        limit = str(self.mod._SCOPE_BUDGET_TOKEN_LIMIT)
        assert limit in reason

    def test_handle_prompt_signals_none_status_returns_none(self):
        """None context_status means 'proceed with LLM call'."""
        result = self.mod._handle_prompt_signals("prompt text", None)
        assert result is None


class TestTouchedOmissionPrecedence:
    """Touched-file omission must take precedence over the budget gate.

    Uses structured _TouchedContextStatus instead of magic strings so that
    real filenames cannot accidentally collide with control sentinels.
    """

    def setup_method(self):
        sys.path.insert(0, REPO)
        self.mod = importlib.import_module("ouroboros.tools.scope_review")

    def _make_status(self, status, omitted_paths=None, token_count=0):
        return self.mod._TouchedContextStatus(
            status=status,
            omitted_paths=omitted_paths or [],
            token_count=token_count,
        )

    def test_touched_omission_wins_over_budget_exceeded(self):
        """When touched files are unreadable AND the assembled prompt would be too large,
        the result must be SCOPE_REVIEW_BLOCKED (fail-closed), not scope_review_skipped.
        """
        from unittest.mock import patch
        import pathlib

        omitted_status = self._make_status("omitted", omitted_paths=["some_file.py"])

        with patch.object(
            self.mod, "_parse_staged_name_status",
            return_value=[("M", "some_file.py", "some_file.py")],
        ), patch.object(
            self.mod, "build_touched_file_pack",
            return_value=("", ["some_file.py"]),
        ), patch.object(
            self.mod, "_inline_deleted_file_pack",
            return_value="",
        ), patch.object(
            self.mod, "_compute_touched_status",
            return_value=omitted_status,
        ), patch.object(
            self.mod, "_gather_scope_packs",
        ) as mock_gather:
            prompt, status = self.mod._build_scope_prompt(
                pathlib.Path("/fake/repo"), "test commit"
            )
            # _gather_scope_packs must NOT have been called — we returned early
            mock_gather.assert_not_called()

        assert prompt is None
        assert status is not None
        assert status.status == "omitted"
        assert status.omitted_paths == ["some_file.py"]

    def test_empty_touched_omission_wins_over_budget_exceeded(self):
        """When no touched files could be read AND the assembled prompt would be too large,
        the result must be SCOPE_REVIEW_BLOCKED (empty status), not scope_review_skipped.
        """
        from unittest.mock import patch
        import pathlib

        empty_status = self._make_status("empty")

        with patch.object(
            self.mod, "_parse_staged_name_status",
            return_value=[("M", "file.py", "file.py")],
        ), patch.object(
            self.mod, "build_touched_file_pack",
            return_value=("", ["file.py"]),
        ), patch.object(
            self.mod, "_inline_deleted_file_pack",
            return_value="",
        ), patch.object(
            self.mod, "_compute_touched_status",
            return_value=empty_status,
        ), patch.object(
            self.mod, "_gather_scope_packs",
        ) as mock_gather:
            prompt, status = self.mod._build_scope_prompt(
                pathlib.Path("/fake/repo"), "test commit"
            )
            mock_gather.assert_not_called()

        assert prompt is None
        assert status is not None
        assert status.status == "empty"

    def test_handle_prompt_signals_blocks_on_omitted_status(self):
        """_handle_prompt_signals must block when context_status.status == 'omitted'."""
        status = self._make_status("omitted", omitted_paths=["binary_file.png"])
        result = self.mod._handle_prompt_signals(None, status)
        assert result is not None
        assert result.blocked
        assert "SCOPE_REVIEW_BLOCKED" in result.block_message
        assert "binary_file.png" in result.block_message

    def test_handle_prompt_signals_blocks_on_empty_status(self):
        """_handle_prompt_signals must block when context_status.status == 'empty'."""
        status = self._make_status("empty")
        result = self.mod._handle_prompt_signals(None, status)
        assert result is not None
        assert result.blocked
        assert "SCOPE_REVIEW_BLOCKED" in result.block_message

    def test_handle_prompt_signals_skips_on_budget_exceeded(self):
        """_handle_prompt_signals must NOT block when context_status.status == 'budget_exceeded'."""
        status = self._make_status("budget_exceeded", token_count=900000)
        result = self.mod._handle_prompt_signals(None, status)
        assert result is not None
        assert not result.blocked
        assert result.advisory_findings
        assert result.advisory_findings[0]["item"] == "scope_review_skipped"

    def test_handle_prompt_signals_returns_none_on_no_status(self):
        """_handle_prompt_signals must return None when context_status is None (proceed)."""
        result = self.mod._handle_prompt_signals("some prompt", None)
        assert result is None

    def test_handle_prompt_signals_blocks_on_unknown_status(self):
        """_handle_prompt_signals must block (fail-closed) for any unrecognised status.

        This is a regression guard: if a new status value is accidentally introduced
        without a corresponding handler, scope review must block rather than
        silently proceeding with an LLM call (fail-open).
        """
        unknown_status = self._make_status("future_unknown_status_xyz")
        result = self.mod._handle_prompt_signals(None, unknown_status)
        assert result is not None, "Unknown status must not return None (fail-closed required)"
        assert result.blocked, "Unknown status must block the commit (fail-closed)"
        assert "SCOPE_REVIEW_BLOCKED" in result.block_message
        assert "future_unknown_status_xyz" in result.block_message
