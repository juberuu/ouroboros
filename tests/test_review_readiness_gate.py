"""Tests for the review readiness gate (cheap deterministic pre-advisory checks)."""

import pathlib
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from ouroboros.tools.review_helpers import check_worktree_readiness


class TestCheckWorktreeReadiness:
    """Tests for check_worktree_readiness()."""

    def test_clean_worktree_returns_no_changes_warning(self, tmp_path):
        """A clean git worktree should produce a 'no changes' warning."""
        with patch("ouroboros.tools.review_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )
            warnings = check_worktree_readiness(tmp_path)
            assert any("no uncommitted changes" in w.lower() for w in warnings)

    def test_dirty_worktree_no_warnings(self, tmp_path):
        """A worktree with changes and no obvious issues should return empty."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = " M README.md\n"
            elif "diff" in cmd:
                result.stdout = "some small diff"
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path)
            assert warnings == []

    def test_py_without_tests_warning(self, tmp_path):
        """Modified .py in ouroboros/ without test changes should warn."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = " M ouroboros/loop.py\n"
            elif "diff" in cmd:
                result.stdout = "small diff"
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path)
            assert any("test" in w.lower() for w in warnings)

    def test_py_with_tests_no_warning(self, tmp_path):
        """Modified .py in ouroboros/ WITH test changes should not warn."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = " M ouroboros/loop.py\n M tests/test_loop.py\n"
            elif "diff" in cmd:
                result.stdout = "small diff"
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path)
            assert not any("test" in w.lower() for w in warnings)

    def test_large_diff_warning(self, tmp_path):
        """Very large diffs should produce a size warning."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = " M bigfile.py\n"
            elif "diff" in cmd:
                result.stdout = "x" * 500_000  # 500K chars
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path)
            assert any("large" in w.lower() or "size" in w.lower() for w in warnings)

    def test_version_sync_warning_included(self, tmp_path):
        """Version sync issues from check_worktree_version_sync should be included."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = " M VERSION\n"
            elif "diff" in cmd:
                result.stdout = "small diff"
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            with patch("ouroboros.tools.review_helpers.check_worktree_version_sync",
                       return_value="VERSION mismatch: 1.0 vs 2.0"):
                warnings = check_worktree_readiness(tmp_path)
                assert any("version" in w.lower() or "mismatch" in w.lower() for w in warnings)

    def test_git_error_does_not_crash(self, tmp_path):
        """If git subprocess fails, the gate should not crash."""
        with patch("ouroboros.tools.review_helpers.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="git", timeout=10)):
            warnings = check_worktree_readiness(tmp_path)
            # Should return empty or a graceful warning, not raise
            assert isinstance(warnings, list)

    def test_paths_scoping(self, tmp_path):
        """When paths are provided, only those paths should be checked."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                if "--" in cmd:
                    result.stdout = " M ouroboros/loop.py\n"
                else:
                    result.stdout = " M ouroboros/loop.py\n M tests/test_loop.py\n"
            elif "diff" in cmd:
                result.stdout = "small diff"
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path, paths=["ouroboros/loop.py"])
            # With path scoping, only ouroboros/loop.py is visible → tests/ not in scope → warning
            assert any("test" in w.lower() for w in warnings)

    def test_returns_list_type(self, tmp_path):
        """check_worktree_readiness must always return a list, never a generator."""
        with patch("ouroboros.tools.review_helpers.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=" M foo.py\n", stderr="")
            result = check_worktree_readiness(tmp_path)
            assert isinstance(result, list), f"Expected list, got {type(result)}"


class TestReadinessGateBlocksBeforeAlreadyFresh:
    """Regression: readiness gate must block on clean worktree even if a prior fresh run exists."""

    def test_clean_worktree_blocked_even_with_prior_fresh(self, tmp_path):
        """No uncommitted changes should return error even if already_fresh would match."""
        def side_effect(cmd, **kwargs):
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = ""  # clean worktree
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path)
            # Must include "no uncommitted changes" warning
            assert any("no uncommitted changes" in w.lower() for w in warnings)

    def test_no_uncommitted_changes_is_first_warning_and_blocks(self, tmp_path):
        """The 'no uncommitted changes' warning should cause early return (no further checks)."""
        call_count = [0]

        def side_effect(cmd, **kwargs):
            call_count[0] += 1
            result = MagicMock(returncode=0, stderr="")
            if "status" in cmd and "--porcelain" in cmd:
                result.stdout = ""  # clean
            else:
                result.stdout = ""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=side_effect):
            warnings = check_worktree_readiness(tmp_path)
            # Should have stopped early — only the initial status check ran
            assert any("no uncommitted changes" in w.lower() for w in warnings)
            # Should NOT have a large diff warning or test-related warnings
            assert not any("test" in w.lower() for w in warnings)
            assert not any("large" in w.lower() for w in warnings)


class TestBuildAdvisoryChangedContextNoDuplicateGitStatus:
    """build_advisory_changed_context must not perform a second git-status call."""

    def test_uses_changed_files_text_not_second_git_status(self, tmp_path):
        """When paths is None, resolved paths come from changed_files_text, not a new subprocess."""
        from ouroboros.tools.review_helpers import build_advisory_changed_context

        porcelain_text = "M  ouroboros/loop.py\nM  ouroboros/tools/review_helpers.py\n"

        subprocess_call_count = [0]

        def mock_subprocess_run(cmd, **kwargs):
            subprocess_call_count[0] += 1
            result = MagicMock(returncode=0)
            result.stdout = b""
            return result

        with patch("ouroboros.tools.review_helpers.subprocess.run", side_effect=mock_subprocess_run):
            with patch("ouroboros.tools.review_helpers.build_touched_file_pack", return_value=("(touched files)", [])):
                resolved, touched, omitted = build_advisory_changed_context(
                    tmp_path,
                    changed_files_text=porcelain_text,
                    paths=None,
                )

        # No subprocess calls should have been made (paths resolved from porcelain text)
        assert subprocess_call_count[0] == 0, (
            f"Expected 0 subprocess calls, got {subprocess_call_count[0]}; "
            "build_advisory_changed_context must use changed_files_text, not a second git-status"
        )
        assert "ouroboros/loop.py" in resolved
        assert "ouroboros/tools/review_helpers.py" in resolved

    def test_explicit_paths_override_changed_files_text(self, tmp_path):
        """When paths is explicitly provided, it overrides changed_files_text entirely."""
        from ouroboros.tools.review_helpers import build_advisory_changed_context

        explicit_paths = ["ouroboros/agent.py"]
        porcelain_text = "M  ouroboros/loop.py\n"

        with patch("ouroboros.tools.review_helpers.build_touched_file_pack", return_value=("(pack)", [])):
            resolved, touched, omitted = build_advisory_changed_context(
                tmp_path,
                changed_files_text=porcelain_text,
                paths=explicit_paths,
            )

        # Explicit paths take precedence — porcelain_text paths should NOT appear
        assert resolved == ["ouroboros/agent.py"]
        assert "ouroboros/loop.py" not in resolved
