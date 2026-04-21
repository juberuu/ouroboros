"""Tests for ouroboros/tools/ci.py — CI trigger and monitoring tool."""

from __future__ import annotations

import json
import os
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path):
    """Minimal ToolContext mock."""
    c = MagicMock()
    c.repo_dir = str(tmp_path)
    c.pending_events = []
    return c


@pytest.fixture
def _gh_settings(monkeypatch):
    """Ensure GITHUB_TOKEN and GITHUB_REPO are set."""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token_123")
    monkeypatch.setenv("GITHUB_REPO", "joi-lab/ouroboros-desktop")
    # Also patch load_settings to return them
    with patch("ouroboros.tools.ci.load_settings", return_value={
        "GITHUB_TOKEN": "ghp_test_token_123",
        "GITHUB_REPO": "joi-lab/ouroboros-desktop",
    }):
        yield


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------

class TestGetGithubConfig:
    def test_missing_token_raises(self):
        from ouroboros.tools.ci import _get_github_config
        with patch("ouroboros.tools.ci.load_settings", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                with pytest.raises(ValueError, match="GITHUB_TOKEN"):
                    _get_github_config()

    def test_missing_repo_raises(self):
        from ouroboros.tools.ci import _get_github_config
        with patch("ouroboros.tools.ci.load_settings", return_value={"GITHUB_TOKEN": "tok"}):
            with patch.dict(os.environ, {"GITHUB_TOKEN": "tok"}, clear=True):
                with pytest.raises(ValueError, match="GITHUB_REPO"):
                    _get_github_config()

    def test_valid_config(self, _gh_settings):
        from ouroboros.tools.ci import _get_github_config
        token, repo = _get_github_config()
        assert token == "ghp_test_token_123"
        assert repo == "joi-lab/ouroboros-desktop"


class TestExtractOs:
    def test_ubuntu(self):
        from ouroboros.tools.ci import _extract_os
        assert _extract_os("full-test (ubuntu-latest)") == "ubuntu"

    def test_windows(self):
        from ouroboros.tools.ci import _extract_os
        assert _extract_os("full-test (windows-latest)") == "windows"

    def test_macos(self):
        from ouroboros.tools.ci import _extract_os
        assert _extract_os("full-test (macos-latest)") == "macos"

    def test_unknown(self):
        from ouroboros.tools.ci import _extract_os
        assert _extract_os("some-job") == "unknown"


class TestGhApi:
    def test_success(self):
        from ouroboros.tools.ci import _gh_api
        response_data = json.dumps({"id": 1}).encode()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            status, data = _gh_api("GET", "/repos/test/test", "token123")
            assert status == 200
            assert data["id"] == 1

    def test_http_error(self):
        from ouroboros.tools.ci import _gh_api
        import urllib.error
        err = urllib.error.HTTPError(
            url="https://api.github.com/test",
            code=404,
            msg="Not Found",
            hdrs={},
            fp=MagicMock(read=lambda: b"not found"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            status, data = _gh_api("GET", "/repos/test/test", "token123")
            assert status == 404
            assert "error" in data


# ---------------------------------------------------------------------------
# Integration tests: run_ci_tests tool handler
# ---------------------------------------------------------------------------

class TestRunCiTests:
    def test_no_token_returns_unavailable(self, ctx):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci.load_settings", return_value={}):
            with patch.dict(os.environ, {}, clear=True):
                result = _run_ci_tests(ctx)
                assert "CI_UNAVAILABLE" in result

    def test_detached_head_returns_invalid(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="HEAD"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"):
            result = _run_ci_tests(ctx)
            assert "CI_BRANCH_INVALID" in result
            assert "detached HEAD" in result

    def test_remote_mismatch_returns_error(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci.run_cmd", side_effect=lambda cmd, **kw:
                   "https://github.com/other-org/other-repo.git\n" if "get-url" in cmd else "ouroboros\n"):
            result = _run_ci_tests(ctx)
            assert "CI_REMOTE_MISMATCH" in result

    def test_non_github_remote_fails_closed(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci.run_cmd", side_effect=lambda cmd, **kw:
                   "https://gitlab.com/user/repo.git\n" if "get-url" in cmd else "ouroboros\n"):
            result = _run_ci_tests(ctx)
            assert "CI_REMOTE_MISMATCH" in result
            assert "not a GitHub remote" in result

    def test_repo_with_dots_matches(self, ctx):
        """Repos with dots in their name should match correctly."""
        from ouroboros.tools.ci import _run_ci_tests
        settings = {"GITHUB_TOKEN": "test", "GITHUB_REPO": "owner/my.repo.name"}
        with patch("ouroboros.tools.ci.load_settings", return_value=settings), \
             patch.dict(os.environ, {"GITHUB_TOKEN": "test", "GITHUB_REPO": "owner/my.repo.name"}), \
             patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci.run_cmd", side_effect=lambda cmd, **kw:
                   "https://github.com/owner/my.repo.name.git\n" if "get-url" in cmd else "ouroboros\n"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._poll_workflow_run", return_value={
                 "status": "completed", "conclusion": "success",
                 "url": "https://github.com/owner/my.repo.name/actions/runs/1", "run_id": 1}):
            result = _run_ci_tests(ctx)
            assert "CI PASSED" in result  # Should not hit CI_REMOTE_MISMATCH

    def test_push_failure(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(False, "remote rejected")):
            result = _run_ci_tests(ctx)
            assert "CI_PUSH_FAILED" in result

    def test_workflow_not_found(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=None):
            result = _run_ci_tests(ctx)
            assert "CI_WORKFLOW_NOT_FOUND" in result

    def test_trigger_no_wait(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(True, "ok")):
            result = _run_ci_tests(ctx, wait=False)
            assert "CI triggered" in result
            assert "ouroboros" in result

    def test_trigger_failure(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(False, "HTTP 403")):
            result = _run_ci_tests(ctx)
            assert "CI_TRIGGER_FAILED" in result

    def test_ci_success(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        poll_result = {
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/test/actions/runs/1",
            "run_id": 1,
        }
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._poll_workflow_run", return_value=poll_result):
            result = _run_ci_tests(ctx)
            assert "CI PASSED" in result
            assert "3 platforms" in result

    def test_ci_failure_with_details(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        poll_result = {
            "status": "completed",
            "conclusion": "failure",
            "url": "https://github.com/test/actions/runs/1",
            "run_id": 1,
        }
        failed_jobs = [
            {"id": 99, "name": "full-test (windows-latest)", "os": "windows",
             "url": "https://github.com/test/jobs/1", "failed_steps": ["Run tests"]},
        ]
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._poll_workflow_run", return_value=poll_result), \
             patch("ouroboros.tools.ci._get_failed_jobs", return_value=failed_jobs), \
             patch("ouroboros.tools.ci._get_job_logs", return_value="FAILED test_x.py::test_foo"):
            result = _run_ci_tests(ctx)
            assert "CI FAILED" in result
            assert "windows" in result
            assert "FAILED test_x.py" in result  # verify log download path exercised

    def test_ci_timeout(self, ctx, _gh_settings):
        from ouroboros.tools.ci import _run_ci_tests
        poll_result = {
            "status": "timeout",
            "conclusion": None,
            "url": "",
            "run_id": None,
        }
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._poll_workflow_run", return_value=poll_result):
            result = _run_ci_tests(ctx, timeout_minutes=1)
            assert "CI_TIMEOUT" in result


class TestNetworkErrorHandling:
    def test_url_error_returns_zero(self):
        from ouroboros.tools.ci import _gh_api
        import urllib.error
        err = urllib.error.URLError("DNS lookup failed")
        with patch("urllib.request.urlopen", side_effect=err):
            status, data = _gh_api("GET", "/repos/test/test", "token123")
            assert status == 0
            assert "Network error" in data["error"]

    def test_timeout_error_returns_zero(self):
        from ouroboros.tools.ci import _gh_api
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            status, data = _gh_api("GET", "/repos/test/test", "token123")
            assert status == 0
            assert "Network error" in data["error"]


class TestToolRegistration:
    def test_get_tools_returns_entry(self):
        from ouroboros.tools.ci import get_tools
        tools = get_tools()
        assert len(tools) == 1
        assert tools[0].name == "run_ci_tests"
        schema = tools[0].schema
        assert "parameters" in schema
        assert "wait" in schema["parameters"]["properties"]
        assert "timeout_minutes" in schema["parameters"]["properties"]


class TestRedirectHandler:
    """Verify _NoAuthRedirectHandler strips Authorization on cross-domain redirects."""

    def test_strips_auth_on_cross_domain(self):
        from ouroboros.tools.ci import _NoAuthRedirectHandler
        import urllib.request

        handler = _NoAuthRedirectHandler()
        # Original request to github.com
        orig_req = urllib.request.Request("https://api.github.com/repos/o/r/actions/jobs/1/logs")
        orig_req.add_header("Authorization", "token ghp_secret123")

        # Simulate redirect to Azure Blob Storage (different domain)
        new_req = handler.redirect_request(
            orig_req, None, 302, "Found", {},
            "https://productionresults.blob.core.windows.net/logs/job-123?sig=abc"
        )
        assert new_req is not None
        assert new_req.get_header("Authorization") is None

    def test_keeps_auth_on_same_domain(self):
        from ouroboros.tools.ci import _NoAuthRedirectHandler
        import urllib.request

        handler = _NoAuthRedirectHandler()
        orig_req = urllib.request.Request("https://api.github.com/repos/o/r/actions/jobs/1/logs")
        orig_req.add_header("Authorization", "token ghp_secret123")

        # Redirect to same domain
        new_req = handler.redirect_request(
            orig_req, None, 302, "Found", {},
            "https://api.github.com/repos/o/r/actions/jobs/1/logs?redirect=true"
        )
        assert new_req is not None
        assert new_req.get_header("Authorization") == "token ghp_secret123"


class TestProgressEmission:
    def test_progress_events_emitted(self, ctx, _gh_settings):
        """Verify that progress events are emitted during workflow."""
        from ouroboros.tools.ci import _run_ci_tests
        poll_result = {
            "status": "completed",
            "conclusion": "success",
            "url": "https://github.com/test/actions/runs/1",
            "run_id": 1,
        }
        with patch("ouroboros.tools.ci._get_current_branch", return_value="ouroboros"), \
             patch("ouroboros.tools.ci._get_current_sha", return_value="abc1234567890"), \
             patch("ouroboros.tools.ci._push_branch", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._find_workflow_id", return_value=12345), \
             patch("ouroboros.tools.ci._trigger_workflow", return_value=(True, "ok")), \
             patch("ouroboros.tools.ci._poll_workflow_run", return_value=poll_result):
            _run_ci_tests(ctx)
            # At least push and trigger progress events should be emitted
            progress_events = [e for e in ctx.pending_events if e.get("type") == "progress"]
            assert len(progress_events) >= 2


class TestCheckCiStatusAfterPush:
    """Tests for _check_ci_status_after_push helper in git.py."""

    def _make_runs_response(self, runs):
        body = json.dumps({"workflow_runs": runs}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _make_jobs_response(self, jobs):
        body = json.dumps({"jobs": jobs}).encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    # run_cmd returns branch on first call, SHA on second call
    LOCAL_SHA = "abc1234def5678901234567890123456789012ab"
    STALE_SHA = "0000000000000000000000000000000000000000"

    def _mock_run_cmd(self, *args, **kwargs):
        """Returns branch name then SHA on successive calls."""
        cmd = args[0]
        if "abbrev-ref" in cmd:
            return "ouroboros\n"
        return self.LOCAL_SHA + "\n"

    def test_no_token_returns_empty(self, tmp_path, monkeypatch):
        """When GITHUB_TOKEN is absent, return '' silently."""
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        assert _check_ci_status_after_push(tmp_path) == ""

    def test_success_run_returns_checkmark(self, tmp_path, monkeypatch):
        """A completed+success run with matching SHA returns the ✅ note."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        runs = [{"status": "completed", "conclusion": "success", "run_number": 42,
                 "head_sha": self.LOCAL_SHA,
                 "html_url": "https://example.com", "jobs_url": ""}]
        runs_resp = self._make_runs_response(runs)
        with patch("urllib.request.urlopen", return_value=runs_resp):
            result = _check_ci_status_after_push(tmp_path)
        assert "✅ CI" in result

    def test_stale_sha_returns_not_registered(self, tmp_path, monkeypatch):
        """Runs with a DIFFERENT head_sha (stale push) must not be reported as ✅.
        Instead the function should return the ⏳ not-yet-registered message."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        # All runs belong to a different commit
        runs = [{"status": "completed", "conclusion": "success", "run_number": 41,
                 "head_sha": self.STALE_SHA,
                 "html_url": "https://example.com", "jobs_url": ""}]
        runs_resp = self._make_runs_response(runs)
        with patch("urllib.request.urlopen", return_value=runs_resp):
            result = _check_ci_status_after_push(tmp_path)
        # Must NOT claim pass for the stale run
        assert "✅" not in result
        assert "⏳" in result

    def test_failure_run_returns_warning(self, tmp_path, monkeypatch):
        """A completed+failure run with matching SHA returns the ⚠️ warning."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        runs = [{"status": "completed", "conclusion": "failure", "run_number": 7,
                 "head_sha": self.LOCAL_SHA,
                 "html_url": "https://github.com/run/7",
                 "jobs_url": "https://api.github.com/jobs/7"}]
        jobs = [{"name": "quick-test", "conclusion": "failure",
                 "steps": [{"name": "Run tests", "conclusion": "failure"}]}]
        runs_resp = self._make_runs_response(runs)
        jobs_resp = self._make_jobs_response(jobs)
        call_count = [0]
        def _fake_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return runs_resp
            return jobs_resp
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = _check_ci_status_after_push(tmp_path)
        assert "⚠️ CI STATUS" in result
        assert "run #7" in result
        assert "quick-test" in result

    def test_in_progress_returns_hourglass(self, tmp_path, monkeypatch):
        """An in-progress run with matching SHA returns the ⏳ note."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        runs = [{"status": "in_progress", "conclusion": None, "run_number": 10,
                 "head_sha": self.LOCAL_SHA,
                 "html_url": "https://github.com/run/10", "jobs_url": ""}]
        runs_resp = self._make_runs_response(runs)
        with patch("urllib.request.urlopen", return_value=runs_resp):
            result = _check_ci_status_after_push(tmp_path)
        assert "⏳ CI" in result

    def test_network_error_returns_empty(self, tmp_path, monkeypatch):
        """Any exception (e.g. network error) returns '' — never crashes."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        with patch("urllib.request.urlopen", side_effect=OSError("network down")):
            result = _check_ci_status_after_push(tmp_path)
        assert result == ""

    def test_no_matching_runs_returns_not_registered(self, tmp_path, monkeypatch):
        """No runs matching local SHA returns the ⏳ not-yet-registered note."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        runs_resp = self._make_runs_response([])
        with patch("urllib.request.urlopen", return_value=runs_resp):
            result = _check_ci_status_after_push(tmp_path)
        assert "⏳" in result

    def test_request_url_includes_head_sha(self, tmp_path, monkeypatch):
        """The GitHub API request URL must include head_sha=<local_sha> so
        GitHub server-side filters to only runs for the just-pushed commit.
        Without this, the first-page result (per_page) may miss the new run."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        captured_urls = []
        runs_resp = self._make_runs_response([])

        class _FakeCtx:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return runs_resp.read()

        def _fake_urlopen(req, **kwargs):
            captured_urls.append(req.full_url)
            return _FakeCtx()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            _check_ci_status_after_push(tmp_path)

        assert captured_urls, "urlopen was never called"
        assert f"head_sha={self.LOCAL_SHA}" in captured_urls[0], (
            f"head_sha not found in API URL: {captured_urls[0]}"
        )

    def test_failure_with_jobs_fetch_error_still_warns(self, tmp_path, monkeypatch):
        """If the runs request succeeds with conclusion='failure' but the jobs
        request raises, the helper must still return ⚠️ CI STATUS (not '').
        Run number and URL come from the already-fetched run object."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        runs = [{"status": "completed", "conclusion": "failure", "run_number": 5,
                 "head_sha": self.LOCAL_SHA,
                 "html_url": "https://github.com/run/5",
                 "jobs_url": "https://api.github.com/jobs/5"}]
        runs_resp = self._make_runs_response(runs)
        call_count = [0]
        def _fake_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return runs_resp
            raise OSError("jobs endpoint unreachable")
        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            result = _check_ci_status_after_push(tmp_path)
        assert "⚠️ CI STATUS" in result, f"Should warn even when jobs fetch fails: {result!r}"
        assert "run #5" in result

    def test_cancelled_run_returns_warning(self, tmp_path, monkeypatch):
        """A completed+cancelled run must surface as ⚠️ — not silently return ''.
        Covers conclusion values: cancelled, timed_out, startup_failure, stale, etc."""
        import ouroboros.tools.git as git_mod
        from ouroboros.tools.git import _check_ci_status_after_push
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("GITHUB_REPO", "owner/repo")
        monkeypatch.setattr(git_mod, "run_cmd", self._mock_run_cmd)
        runs = [{"status": "completed", "conclusion": "cancelled", "run_number": 99,
                 "head_sha": self.LOCAL_SHA,
                 "html_url": "https://github.com/run/99", "jobs_url": ""}]
        runs_resp = self._make_runs_response(runs)
        with patch("urllib.request.urlopen", return_value=runs_resp):
            result = _check_ci_status_after_push(tmp_path)
        assert "⚠️ CI STATUS" in result
        assert "CANCELLED" in result
        assert "run #99" in result


class TestCiStatusWiring:
    """Verify that _check_ci_status_after_push is correctly wired into both
    _repo_commit_push and _repo_write_commit: appended on successful push,
    skipped when push did not succeed."""

    def _make_ctx(self, tmp_path):
        """Minimal ToolContext mock for wiring tests."""
        ctx = MagicMock()
        ctx.repo_dir = str(tmp_path)
        ctx.drive_root = str(tmp_path)
        ctx.branch_dev = "ouroboros"
        ctx.last_push_succeeded = False
        ctx._review_advisory = []
        ctx._last_triad_models = []
        ctx._last_scope_model = ""
        ctx._last_triad_raw_results = []
        ctx._last_scope_raw_result = {}
        ctx._review_degraded_reasons = []
        ctx._scope_review_history = {}
        ctx.pending_events = []
        ctx.emit_progress_fn = MagicMock()
        ctx.repo_path = lambda p: tmp_path / p
        return ctx

    def test_ci_note_appended_on_successful_push(self, tmp_path, monkeypatch):
        """When push succeeds, _check_ci_status_after_push result is appended
        to the _repo_commit_push return value."""
        import ouroboros.tools.git as git_mod

        ctx = self._make_ctx(tmp_path)

        # Stub out everything except the ci_note wiring
        monkeypatch.setattr(git_mod, "_check_advisory_freshness", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_run_reviewed_stage_cycle",
                            lambda *a, **kw: {"status": "passed",
                                              "pre_fingerprint": {}, "post_fingerprint": {}})
        monkeypatch.setattr(git_mod, "_record_commit_attempt", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_post_commit_result", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_auto_tag_on_version_bump", lambda *a, **kw: "")
        monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda *a, **kw: tmp_path / "git.lock")
        monkeypatch.setattr(git_mod, "_release_git_lock", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_auto_push",
                            lambda *a, **kw: " [pushed: ouroboros]")
        monkeypatch.setattr(git_mod, "_check_ci_status_after_push",
                            lambda *a, **kw: "\n\n✅ CI: Run passed for this commit.")
        # Stub git commit subprocess
        monkeypatch.setattr(git_mod, "run_cmd",
                            lambda *a, **kw: "")

        result = git_mod._repo_commit_push(ctx, "test commit")
        assert "✅ CI" in result, f"CI note not appended on successful push: {result!r}"

    def test_ci_note_skipped_when_push_fails(self, tmp_path, monkeypatch):
        """When push fails (last_push_succeeded is False), _check_ci_status_after_push
        must NOT be called."""
        import ouroboros.tools.git as git_mod

        ctx = self._make_ctx(tmp_path)

        monkeypatch.setattr(git_mod, "_check_advisory_freshness", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_run_reviewed_stage_cycle",
                            lambda *a, **kw: {"status": "passed",
                                              "pre_fingerprint": {}, "post_fingerprint": {}})
        monkeypatch.setattr(git_mod, "_record_commit_attempt", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_post_commit_result", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_auto_tag_on_version_bump", lambda *a, **kw: "")
        monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda *a, **kw: tmp_path / "git.lock")
        monkeypatch.setattr(git_mod, "_release_git_lock", lambda *a, **kw: None)
        # Push fails
        monkeypatch.setattr(git_mod, "_auto_push",
                            lambda *a, **kw: " [push skipped: no remote]")
        ci_called = [False]
        def _ci_stub(*a, **kw):
            ci_called[0] = True
            return "\n\n✅ CI: Run passed for this commit."
        monkeypatch.setattr(git_mod, "_check_ci_status_after_push", _ci_stub)
        monkeypatch.setattr(git_mod, "run_cmd", lambda *a, **kw: "")

        result = git_mod._repo_commit_push(ctx, "test commit")
        assert not ci_called[0], "CI status must not be queried when push did not succeed"
        assert "✅ CI" not in result

    # ---- _repo_write_commit wiring ----

    def _patch_write_commit(self, git_mod, tmp_path, monkeypatch, push_ok: bool):
        """Common monkeypatches for _repo_write_commit wiring tests."""
        # Write a file so repo_write_commit has something to stage
        (tmp_path / "x.py").write_text("pass\n")
        monkeypatch.setattr(git_mod, "_check_advisory_freshness", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_run_reviewed_stage_cycle",
                            lambda *a, **kw: {"status": "passed",
                                              "pre_fingerprint": {}, "post_fingerprint": {}})
        monkeypatch.setattr(git_mod, "_record_commit_attempt", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_invalidate_advisory", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_post_commit_result", lambda *a, **kw: None)
        monkeypatch.setattr(git_mod, "_auto_tag_on_version_bump", lambda *a, **kw: "")
        monkeypatch.setattr(git_mod, "_acquire_git_lock", lambda *a, **kw: tmp_path / "git.lock")
        monkeypatch.setattr(git_mod, "_release_git_lock", lambda *a, **kw: None)
        push_str = " [pushed: ouroboros]" if push_ok else " [push skipped: no remote]"
        monkeypatch.setattr(git_mod, "_auto_push", lambda *a, **kw: push_str)
        monkeypatch.setattr(git_mod, "run_cmd", lambda *a, **kw: "")

    def test_write_commit_ci_note_appended_on_successful_push(self, tmp_path, monkeypatch):
        """_repo_write_commit appends CI note when push succeeds."""
        import ouroboros.tools.git as git_mod
        ctx = self._make_ctx(tmp_path)
        self._patch_write_commit(git_mod, tmp_path, monkeypatch, push_ok=True)
        monkeypatch.setattr(git_mod, "_check_ci_status_after_push",
                            lambda *a, **kw: "\n\n✅ CI: Run passed for this commit.")
        result = git_mod._repo_write_commit(ctx, path="x.py", content="pass\n",
                                             commit_message="wiring test")
        assert "✅ CI" in result, f"CI note not appended for _repo_write_commit: {result!r}"

    def test_write_commit_ci_note_skipped_when_push_fails(self, tmp_path, monkeypatch):
        """_repo_write_commit skips CI lookup when push fails."""
        import ouroboros.tools.git as git_mod
        ctx = self._make_ctx(tmp_path)
        self._patch_write_commit(git_mod, tmp_path, monkeypatch, push_ok=False)
        ci_called = [False]
        def _ci_stub(*a, **kw):
            ci_called[0] = True
            return "\n\n✅ CI: Run passed for this commit."
        monkeypatch.setattr(git_mod, "_check_ci_status_after_push", _ci_stub)
        git_mod._repo_write_commit(ctx, path="x.py", content="pass\n",
                                    commit_message="wiring test")
        assert not ci_called[0], "CI status must not be queried when push failed"
