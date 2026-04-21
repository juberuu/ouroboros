"""Tests for review pipeline calibration improvements.

Covers:
- P1: New deterministic preflight checks 5-8 (version sync via staged content,
      readme changelog row, conftest test functions)
- P2: Structured self-verification in blocked message (attempt >= 2)
- P3: Obligation accumulation — findings stored separately, dedup is agent's job
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx._review_iteration_count = 1
    ctx._review_history = []
    ctx._review_advisory = []
    ctx._last_review_critical_findings = []
    ctx._last_review_advisory_findings = []
    ctx._last_review_block_reason = ""
    return ctx


def _make_attempt(commit_message: str, findings: list, repo_key: str = "/repo"):
    from ouroboros.review_state import CommitAttemptRecord
    return CommitAttemptRecord(
        ts="2026-04-08T19:00:00",
        commit_message=commit_message,
        status="blocked",
        block_reason="critical_findings",
        block_details="",
        duration_sec=10.0,
        critical_findings=findings,
        advisory_findings=[],
        tool_name="repo_commit",
        task_id="test-task",
        attempt=1,
        repo_key=repo_key,
    )


# ---------------------------------------------------------------------------
# P1: preflight check 5 — version_values_match (reads staged content)
# ---------------------------------------------------------------------------

class TestPreflightVersionValuesMatch:
    """Check 5: VERSION staged → all carriers in staged index must match version."""

    def _run(self, staged_contents: dict, staged_files: str = "M  VERSION\nM  README.md\nM  pyproject.toml\nM  docs/ARCHITECTURE.md") -> str | None:
        from ouroboros.tools.review import _preflight_check

        def fake_git_show(repo_dir, path):
            return staged_contents.get(path, "")

        with patch("ouroboros.tools.review._git_show_staged", side_effect=fake_git_show):
            return _preflight_check("v4.18.0 release", staged_files, "/repo")

    def test_all_in_sync_passes(self):
        result = self._run({
            "VERSION": "4.18.0\n",
            "pyproject.toml": 'version = "4.18.0"\n',
            "README.md": "version-4.18.0-green\n| 4.18.0 | 2026-04-08 | Change |\n",
            "docs/ARCHITECTURE.md": "# Ouroboros v4.18.0\n",
        })
        assert result is None

    def test_pyproject_mismatch_blocks(self):
        result = self._run({
            "VERSION": "4.18.0\n",
            "pyproject.toml": 'version = "4.17.9"\n',
            "README.md": "version-4.18.0-green\n| 4.18.0 |\n",
            "docs/ARCHITECTURE.md": "# Ouroboros v4.18.0\n",
        })
        assert result is not None
        assert "PREFLIGHT_BLOCKED" in result
        assert "pyproject.toml" in result

    def test_readme_badge_mismatch_blocks(self):
        result = self._run({
            "VERSION": "4.18.0\n",
            "pyproject.toml": 'version = "4.18.0"\n',
            "README.md": "version-4.17.9-green\n| 4.18.0 |\n",
            "docs/ARCHITECTURE.md": "# Ouroboros v4.18.0\n",
        })
        assert result is not None
        assert "PREFLIGHT_BLOCKED" in result
        assert "README.md badge" in result

    def test_architecture_mismatch_blocks(self):
        result = self._run({
            "VERSION": "4.18.0\n",
            "pyproject.toml": 'version = "4.18.0"\n',
            "README.md": "version-4.18.0-green\n| 4.18.0 |\n",
            "docs/ARCHITECTURE.md": "# Ouroboros v4.17.9\n",
        })
        assert result is not None
        assert "PREFLIGHT_BLOCKED" in result
        assert "ARCHITECTURE.md header" in result

    def test_no_version_staged_skips_check(self):
        """Check 5 should not fire when VERSION is not staged."""
        def fake_git_show(repo_dir, path):
            if path == "VERSION":
                return "4.18.0\n"
            if path == "pyproject.toml":
                return 'version = "4.17.9"\n'  # mismatch but should be ignored
            return ""

        with patch("ouroboros.tools.review._git_show_staged", side_effect=fake_git_show):
            from ouroboros.tools.review import _preflight_check
            result = _preflight_check("fix something", "M  README.md", "/repo")
        assert result is None  # VERSION not staged → check 5 doesn't fire

    def test_invalid_version_format_skips_gracefully(self):
        """Non-semver VERSION value skips check 5 without blocking."""
        result = self._run({
            "VERSION": "dev\n",
            "pyproject.toml": 'version = "4.18.0"\n',
            "README.md": "version-4.18.0-green\n",
            "docs/ARCHITECTURE.md": "# Ouroboros v4.18.0\n",
        })
        assert result is None  # non-semver → skip check


# ---------------------------------------------------------------------------
# P1: preflight check 6 — readme_changelog_row (staged content)
# ---------------------------------------------------------------------------

class TestPreflightReadmeChangelogRow:
    """Check 6: VERSION staged → staged README.md changelog must have a row for the version."""

    def _run(self, version: str, readme_content: str) -> str | None:
        from ouroboros.tools.review import _preflight_check

        def fake_git_show(repo_dir, path):
            if path == "VERSION":
                return version + "\n"
            if path == "README.md":
                return readme_content
            if path == "pyproject.toml":
                return f'version = "{version}"\n'
            if path == "docs/ARCHITECTURE.md":
                return f"# Ouroboros v{version}\n"
            return ""

        with patch("ouroboros.tools.review._git_show_staged", side_effect=fake_git_show):
            return _preflight_check(f"v{version}", "M  VERSION\nM  README.md", "/repo")

    def test_changelog_row_present_passes(self):
        result = self._run("4.18.0", f"version-4.18.0-green\n| 4.18.0 | 2026-04-08 | A change |\n")
        assert result is None

    def test_changelog_row_missing_blocks(self):
        result = self._run("4.18.0", "version-4.18.0-green\n| 4.17.5 | 2026-04-08 | Old |\n")
        assert result is not None
        assert "PREFLIGHT_BLOCKED" in result
        assert "changelog" in result.lower()


# ---------------------------------------------------------------------------
# P1: preflight check 8 — conftest_no_tests (reads staged content)
# ---------------------------------------------------------------------------

class TestPreflightConftestNoTests:
    """Check 8: staged conftest.py with test_ functions → block (AST-based)."""

    def _run(self, conftest_content: str, staged: str = "M  conftest.py") -> str | None:
        from ouroboros.tools.review import _preflight_check

        def fake_git_show(repo_dir, path):
            return conftest_content if path.endswith("conftest.py") else ""

        with patch("ouroboros.tools.review._git_show_staged", side_effect=fake_git_show):
            return _preflight_check("add conftest", staged, "/repo")

    def test_conftest_with_test_functions_blocks(self):
        result = self._run(
            "import pytest\n\n@pytest.fixture\ndef my_fixture(): pass\n\n"
            "def test_should_not_be_here():\n    assert True\n"
        )
        assert result is not None
        assert "PREFLIGHT_BLOCKED" in result
        assert "test_should_not_be_here" in result

    def test_conftest_fixtures_only_passes(self):
        result = self._run(
            "import pytest\n\n@pytest.fixture\ndef my_fixture():\n    return 42\n"
        )
        assert result is None

    def test_conftest_comment_not_test_passes(self):
        """AST-based check should not trigger on comments or module-level names."""
        result = self._run(
            "# test_something would go here\ntest_value = 42  # a module-level variable\n"
        )
        assert result is None

    def test_conftest_async_test_blocks(self):
        """async def test_ functions should also block."""
        result = self._run(
            "import asyncio\n\nasync def test_async_in_conftest():\n    pass\n"
        )
        assert result is not None
        assert "PREFLIGHT_BLOCKED" in result
        assert "test_async_in_conftest" in result

    def test_conftest_not_staged_skips(self):
        """If conftest.py is not in staged files, check 8 should not fire."""
        from ouroboros.tools.review import _preflight_check

        def fake_git_show(repo_dir, path):
            return "def test_leaked(): pass\n"  # would block if staged

        with patch("ouroboros.tools.review._git_show_staged", side_effect=fake_git_show):
            result = _preflight_check("fix", "M  ouroboros/foo.py\nA  tests/test_foo.py", "/repo")
        assert result is None

    def test_conftest_omission_note_on_many_tests(self):
        """More than 5 test functions → omission note in block message."""
        fns = "\n".join(f"def test_fn_{i}(): pass" for i in range(7))
        result = self._run(f"import pytest\n{fns}\n")
        assert result is not None
        assert "showing first 5 of 7" in result

    def test_conftest_parse_error_skips_gracefully(self):
        """Invalid Python should not crash preflight."""
        result = self._run("def test_broken(: syntax error\n")
        # Should not raise; parse error means check is skipped
        assert result is None

    def test_myconftest_not_treated_as_conftest(self):
        """Files named myconftest.py or foo_conftest.py must not trigger check 8."""
        from ouroboros.tools.review import _preflight_check

        def fake_git_show(repo_dir, path):
            return "def test_should_not_block(): pass\n"

        with patch("ouroboros.tools.review._git_show_staged", side_effect=fake_git_show):
            result = _preflight_check("add", "M  tests/myconftest.py\nM  foo_conftest.py", "/repo")
        assert result is None

    def test_nested_test_helper_inside_fixture_not_blocked(self):
        """test_ functions nested inside other functions should not trigger check 8."""
        content = (
            "import pytest\n\n"
            "@pytest.fixture\n"
            "def my_fixture():\n"
            "    def test_helper_inner():  # nested — pytest does NOT collect this\n"
            "        pass\n"
            "    return test_helper_inner\n"
        )
        result = self._run(content)
        assert result is None


# ---------------------------------------------------------------------------
# P2: structured self-verification in blocked message
# ---------------------------------------------------------------------------

class TestSelfVerificationInBlockedMessage:
    """From attempt 2 onwards, blocked message includes self-verification template."""

    def _build_blocked_msg(self, attempt_count: int, critical_finds: list) -> str:
        from ouroboros.tools.review import _build_critical_block_message
        ctx = _make_ctx()
        ctx._review_iteration_count = attempt_count
        ctx._last_review_critical_findings = critical_finds
        ctx._last_review_advisory_findings = []
        return _build_critical_block_message(ctx, "test commit", ["raw fail"], [], "")

    def test_attempt_1_no_self_verification(self):
        msg = self._build_blocked_msg(1, [])
        assert "Self-verification required" not in msg

    def test_attempt_2_has_self_verification(self):
        findings = [{"item": "code_quality", "reason": "Missing test", "severity": "critical", "verdict": "FAIL"}]
        msg = self._build_blocked_msg(2, findings)
        assert "Self-verification required" in msg
        assert "Finding:" in msg
        assert "Status:" in msg
        assert "Evidence:" in msg

    def test_attempt_3_still_has_self_verification(self):
        findings = [{"item": "tests_affected", "reason": "No test changes", "severity": "critical", "verdict": "FAIL"}]
        msg = self._build_blocked_msg(3, findings)
        assert "Self-verification required" in msg
        assert "tests_affected" in msg

    def test_self_verification_lists_all_finding_items(self):
        """All findings listed — no hard cap."""
        findings = [
            {"item": f"item_{i}", "reason": f"Reason {i}", "severity": "critical", "verdict": "FAIL"}
            for i in range(15)
        ]
        msg = self._build_blocked_msg(2, findings)
        for i in range(15):
            assert f"item_{i}" in msg


# ---------------------------------------------------------------------------
# P3: obligation accumulation — findings stored separately, dedup is agent's job
# ---------------------------------------------------------------------------

class TestObligationGrouping:
    """Obligation identity uses stable public ids plus fingerprints.

    Distinct same-item findings stay separate by default, while explicit
    obligation ids still let reviewers point a retry back at the same issue.
    """

    def _make_state(self):
        from ouroboros.review_state import AdvisoryReviewState
        return AdvisoryReviewState()

    def test_canonical_checklist_item_distinct_reasons_stay_separate(self):
        """Two different same-item findings must not collapse into one obligation."""
        state = self._make_state()
        attempt = _make_attempt("fix something", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Bug in foo.py"},
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Missing error handling in bar.py"},
        ])
        state._update_obligations_from_attempt(attempt)
        open_obs = state.get_open_obligations()
        cq_obs = [o for o in open_obs if o.item.lower() == "code_quality"]
        assert len(cq_obs) == 2
        assert {o.obligation_id for o in cq_obs} == {"obl-0001", "obl-0002"}
        assert all(o.fingerprint.startswith("finding:code_quality:") for o in cq_obs)
        assert {o.reason for o in cq_obs} == {
            "Bug in foo.py",
            "Missing error handling in bar.py",
        }

    def test_different_items_produce_separate_obligations(self):
        state = self._make_state()
        attempt = _make_attempt("fix things", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Bug in foo.py"},
            {"verdict": "FAIL", "severity": "critical", "item": "tests_affected", "reason": "No test changes"},
            {"verdict": "FAIL", "severity": "critical", "item": "version_bump", "reason": "No version"},
        ])
        state._update_obligations_from_attempt(attempt)
        open_obs = state.get_open_obligations()
        items = {o.item.lower() for o in open_obs}
        assert "code_quality" in items
        assert "tests_affected" in items
        assert "version_bump" in items
        assert len(open_obs) == 3

    def test_advisory_findings_not_included_in_obligations(self):
        state = self._make_state()
        attempt = _make_attempt("fix", [
            {"verdict": "FAIL", "severity": "advisory", "item": "context_building", "reason": "Consider adding"},
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Real bug"},
        ])
        state._update_obligations_from_attempt(attempt)
        open_obs = state.get_open_obligations()
        items = {o.item.lower() for o in open_obs}
        assert "code_quality" in items
        assert "context_building" not in items

    def test_same_finding_repeated_merges_into_one_obligation(self):
        """Same item + same reason across two attempts → one obligation (deduped)."""
        state = self._make_state()
        attempt1 = _make_attempt("fix v1", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Bug in foo.py"},
        ])
        state._update_obligations_from_attempt(attempt1)

        attempt2 = _make_attempt("fix v2", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Bug in foo.py"},
        ])
        state._update_obligations_from_attempt(attempt2)

        open_obs = state.get_open_obligations()
        cq_obs = [o for o in open_obs if o.item.lower() == "code_quality"]
        assert len(cq_obs) == 1
        assert "foo.py" in cq_obs[0].reason
        # Must not duplicate: "Bug in foo.py | Bug in foo.py"
        assert cq_obs[0].reason.count("Bug in foo.py") == 1

    def test_different_reason_on_retry_creates_new_obligation_without_roundtrip_id(self):
        """Without reviewer round-tripping obligation_id, a rephrased finding stays distinct."""
        state = self._make_state()
        attempt1 = _make_attempt("fix v1", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Bug in foo.py"},
        ])
        state._update_obligations_from_attempt(attempt1)

        attempt2 = _make_attempt("fix v2", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Also missing test"},
        ])
        state._update_obligations_from_attempt(attempt2)

        open_obs = state.get_open_obligations()
        cq_obs = [o for o in open_obs if o.item.lower() == "code_quality"]
        assert len(cq_obs) == 2
        assert {o.obligation_id for o in cq_obs} == {"obl-0001", "obl-0002"}
        assert {o.reason for o in cq_obs} == {
            "Bug in foo.py",
            "Also missing test",
        }

    def test_pass_finding_not_included_as_obligation(self):
        state = self._make_state()
        attempt = _make_attempt("fix", [
            {"verdict": "PASS", "severity": "critical", "item": "code_quality", "reason": "All good"},
        ])
        state._update_obligations_from_attempt(attempt)
        assert state.get_open_obligations() == []

    def test_empty_findings_returns_empty(self):
        state = self._make_state()
        attempt = _make_attempt("no findings", [])
        ids = state._update_obligations_from_attempt(attempt)
        assert ids == []
        assert state.get_open_obligations() == []

    def test_no_reason_duplication_on_identical_retry(self):
        """Same item+reason on two retries → same fingerprint → one obligation, reason not doubled."""
        state = self._make_state()
        reason = "Hardcoded truncation in foo.py"
        attempt1 = _make_attempt("fix v1", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": reason},
        ])
        state._update_obligations_from_attempt(attempt1)
        attempt2 = _make_attempt("fix v2", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": reason},
        ])
        state._update_obligations_from_attempt(attempt2)
        cq_obs = [o for o in state.get_open_obligations() if o.item.lower() == "code_quality"]
        assert len(cq_obs) == 1
        # Reason should appear exactly once, not "reason | reason"
        assert cq_obs[0].reason.count(reason) == 1

    def test_noncanonical_items_still_split_into_multiple_obligations(self):
        """Reviewer-specific bug_* items still use a separate fingerprint path."""
        state = self._make_state()
        attempt1 = _make_attempt("fix v1", [
            {"verdict": "FAIL", "severity": "critical", "item": "bug_1", "reason": "Bug in foo.py"},
        ])
        state._update_obligations_from_attempt(attempt1)

        attempt2 = _make_attempt("fix v2", [
            {"verdict": "FAIL", "severity": "critical", "item": "bug_1", "reason": "Bug in foo.py"},
            {"verdict": "FAIL", "severity": "critical", "item": "bug_2", "reason": "Missing bar.py"},
        ])
        state._update_obligations_from_attempt(attempt2)

        bug_obs = [o for o in state.get_open_obligations() if o.item.lower().startswith("bug_")]
        assert len(bug_obs) == 2
        reasons = {o.reason for o in bug_obs}
        # Each reason appears exactly once (no "A | A" or "A | B" in a single obligation)
        assert any("Bug in foo.py" in r and "Missing bar.py" not in r for r in reasons)
        assert any("Missing bar.py" in r for r in reasons)

    def test_identical_reasons_in_single_attempt_deduplicated(self):
        """Two identical findings for same item in one attempt: fingerprint dedup → one obligation."""
        state = self._make_state()
        attempt = _make_attempt("one-shot", [
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Same bug"},
            {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": "Same bug"},
        ])
        state._update_obligations_from_attempt(attempt)
        cq_obs = [o for o in state.get_open_obligations() if o.item.lower() == "code_quality"]
        # Same fingerprint → same obligation_id → only one created
        assert len(cq_obs) == 1
        assert cq_obs[0].reason.count("Same bug") == 1

    def test_reason_not_pipe_joined_on_repeat(self):
        """Repeated same finding must NOT produce 'reason | reason' strings — reason stays stable."""
        state = self._make_state()
        reason = "Bug in loop.py line 42"
        for i in range(3):
            attempt = _make_attempt(f"attempt {i}", [
                {"verdict": "FAIL", "severity": "critical", "item": "code_quality", "reason": reason},
            ])
            state._update_obligations_from_attempt(attempt)
        cq_obs = [o for o in state.get_open_obligations() if o.item.lower() == "code_quality"]
        assert len(cq_obs) == 1
        # Reason must be exactly the original string — no pipe-joined duplicates
        assert cq_obs[0].reason == reason
        assert "|" not in cq_obs[0].reason

    def test_explicit_obligation_id_wins_on_retry(self):
        state = self._make_state()
        first = _make_attempt("attempt 1", [
            {"verdict": "FAIL", "severity": "critical", "item": "bug_1", "reason": "Bug in foo.py"},
        ])
        state._update_obligations_from_attempt(first)
        existing = state.get_open_obligations()[0]

        second = _make_attempt("attempt 2", [
            {
                "verdict": "FAIL",
                "severity": "critical",
                "item": "code_quality",
                "obligation_id": existing.obligation_id,
                "reason": "Reviewer rephrased the same root cause",
            },
        ])
        state._update_obligations_from_attempt(second)

        open_obs = state.get_open_obligations()
        assert len(open_obs) == 1
        assert open_obs[0].obligation_id == existing.obligation_id
        assert open_obs[0].reason == "Bug in foo.py"


# ---------------------------------------------------------------------------
# Advisory worktree version-sync check (_check_worktree_version_sync)
# ---------------------------------------------------------------------------

def test_shared_calibration_marks_narrative_mismatch_as_advisory():
    """README/test-count style narrative drift should be calibrated as advisory, not critical."""
    from ouroboros.tools.review_helpers import CRITICAL_FINDING_CALIBRATION

    text = CRITICAL_FINDING_CALIBRATION.lower()
    assert "narrative" in text
    assert "advisory" in text
    assert "readme test counts" in text
    assert "release/version metadata" in text or "release" in text


class TestAdvisoryWorktreeVersionSync:
    """Worktree version-sync preflight in the advisory path."""

    def _write_files(self, tmp_path, version, pyproject_ver=None, readme_ver=None, arch_ver=None):
        (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")
        pv = pyproject_ver if pyproject_ver is not None else version
        (tmp_path / "pyproject.toml").write_text(f'version = "{pv}"\n', encoding="utf-8")
        rv = readme_ver if readme_ver is not None else version
        (tmp_path / "README.md").write_text(f"version-{rv}-green\n", encoding="utf-8")
        av = arch_ver if arch_ver is not None else version
        docs = tmp_path / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "ARCHITECTURE.md").write_text(f"# Ouroboros v{av}\n", encoding="utf-8")

    def test_all_in_sync_returns_empty(self, tmp_path):
        self._write_files(tmp_path, "4.18.0")
        from ouroboros.tools.claude_advisory_review import _check_worktree_version_sync
        assert _check_worktree_version_sync(tmp_path) == ""

    def test_pyproject_mismatch_warns(self, tmp_path):
        self._write_files(tmp_path, "4.18.0", pyproject_ver="4.17.9")
        from ouroboros.tools.claude_advisory_review import _check_worktree_version_sync
        result = _check_worktree_version_sync(tmp_path)
        assert "pyproject.toml" in result
        assert "4.18.0" in result

    def test_readme_badge_mismatch_warns(self, tmp_path):
        self._write_files(tmp_path, "4.18.0", readme_ver="4.17.9")
        from ouroboros.tools.claude_advisory_review import _check_worktree_version_sync
        result = _check_worktree_version_sync(tmp_path)
        assert "README.md badge" in result

    def test_architecture_mismatch_warns(self, tmp_path):
        self._write_files(tmp_path, "4.18.0", arch_ver="4.17.9")
        from ouroboros.tools.claude_advisory_review import _check_worktree_version_sync
        result = _check_worktree_version_sync(tmp_path)
        assert "ARCHITECTURE.md header" in result

    def test_missing_version_file_skips(self, tmp_path):
        from ouroboros.tools.claude_advisory_review import _check_worktree_version_sync
        assert _check_worktree_version_sync(tmp_path) == ""

    def test_non_semver_version_skips(self, tmp_path):
        (tmp_path / "VERSION").write_text("dev\n", encoding="utf-8")
        from ouroboros.tools.claude_advisory_review import _check_worktree_version_sync
        assert _check_worktree_version_sync(tmp_path) == ""
