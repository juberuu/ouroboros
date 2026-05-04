"""Tests for shell tool arg contract and run_shell behavior."""
import inspect
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from ouroboros.tools.shell import (
    _run_shell,
)


class TestShellArgContract:
    """run_shell recovers string cmd via cascade, only errors on unrecoverable input."""

    def test_string_cmd_recovered_via_shlex(self, monkeypatch):
        """Plain shell-style string is recovered via shlex.split."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "hello", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, "echo hello")
        assert "SHELL_ARG_ERROR" not in result
        assert "exit_code=0" in result

    def test_json_array_string_recovered(self, monkeypatch):
        """JSON-encoded array string is recovered via json.loads."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, '["echo", "hello"]')
        assert "SHELL_ARG_ERROR" not in result
        assert "exit_code=0" in result

    def test_python_literal_string_recovered(self, monkeypatch):
        """Python literal list string is recovered via ast.literal_eval."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, "['echo', 'hello']")
        assert "SHELL_ARG_ERROR" not in result
        assert "exit_code=0" in result

    def test_unrecoverable_string_returns_error(self):
        """Completely unrecoverable string still returns SHELL_ARG_ERROR."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))
        # Empty string cannot be recovered
        result = _run_shell(ctx, "")
        assert "SHELL_ARG_ERROR" in result

    def test_string_cmd_still_validates_env_refs(self, monkeypatch):
        """Recovered string cmd still goes through ENV_REF validation."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))
        result = _run_shell(ctx, 'curl -H "x-api-key: $SECRET"')
        assert "SHELL_ENV_ERROR" in result

    # -----------------------------------------------------------------
    # JSON-shape refusal — 2026-05-03 production bug.
    # When cmd arrives as a malformed JSON/Python literal (looks like a
    # list but won't parse), the cascade used to fall through to
    # shlex.split, which strips the brackets and produces garbage argv
    # that subprocess fails to exec with a useless ``[Errno 2] '[git,'``.
    # The cascade now refuses with a targeted error before shlex runs.
    # -----------------------------------------------------------------

    def test_malformed_json_array_refused_not_shlex_split(self):
        """A string starting with `[` that fails json.loads + ast.literal_eval
        must NOT fall through to shlex.split — that produced ``'[git,'``
        argv tokens which fail at exec time with a useless error."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))
        # Trailing comma — JSON rejects, ast.literal_eval might tolerate
        # but in malformed cases neither parses; bracket prefix triggers refusal.
        result = _run_shell(ctx, '["git", "log",')  # unclosed bracket
        assert "SHELL_ARG_ERROR" in result
        assert "stringified array" in result.lower()
        # The old failure mode emitted "[Errno 2]" — make sure we don't
        # get there.
        assert "Errno" not in result

    def test_malformed_dict_literal_refused(self):
        """Same refusal for `{`-prefixed garbage so the model gets a
        clear error instead of subprocess noise."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))
        result = _run_shell(ctx, '{key: value, broken')
        assert "SHELL_ARG_ERROR" in result
        assert "Errno" not in result

    def test_valid_json_array_still_works_after_refusal_branch(self, monkeypatch):
        """Regression guard: the refusal must NOT fire when JSON parses
        cleanly. ``["echo", "ok"]`` → recovered → executed."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, '["echo", "ok"]')
        assert "SHELL_ARG_ERROR" not in result
        assert "exit_code=0" in result

    def test_legitimate_shell_string_still_recovers_via_shlex(self, monkeypatch):
        """Regression guard: the refusal must NOT fire for plain shell
        strings (no leading bracket). ``echo hello`` → shlex.split works."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "hello", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, "echo hello")
        assert "SHELL_ARG_ERROR" not in result
        assert "exit_code=0" in result

    def test_posix_bracket_test_command_still_recovers_via_shlex(self, monkeypatch):
        """POSIX `[` is a real command, not a malformed JSON list."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))

        def fake_run(cmd, **kwargs):
            assert cmd == ["[", "-f", "file.txt", "]"]
            return CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, "[ -f file.txt ]")
        assert "SHELL_ARG_ERROR" not in result
        assert "exit_code=0" in result

    def test_refusal_message_points_at_correct_usage(self):
        """The error must teach the fix, not just refuse. Contains the
        canonical example so a smaller model can pattern-match."""
        ctx = SimpleNamespace(repo_dir="/tmp", drive_logs=lambda: __import__("pathlib").Path("/tmp"))
        result = _run_shell(ctx, '["git", "log",')
        assert 'run_shell(cmd=["git"' in result

    def test_list_cmd_is_accepted(self):
        """List cmd should not trigger arg error."""
        src = inspect.getsource(_run_shell)
        # The function should proceed past the string check for list cmds
        assert "isinstance(cmd, list)" in src or "not isinstance(cmd, list)" in src


def test_run_shell_rejects_literal_env_refs_in_argv(tmp_path):
    ctx = SimpleNamespace(repo_dir=tmp_path)
    result = _run_shell(ctx, ["curl", "-H", "x-api-key: $ANTHROPIC_API_KEY"])
    assert "SHELL_ENV_ERROR" in result
    assert "$ANTHROPIC_API_KEY" in result


def test_run_shell_allows_shell_expansion_via_sh_c(tmp_path, monkeypatch):
    ctx = SimpleNamespace(repo_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "ok", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    result = _run_shell(ctx, ["sh", "-c", "printf '%s' \"$ANTHROPIC_API_KEY\""])
    assert "SHELL_ENV_ERROR" not in result
    assert "exit_code=0" in result


def test_run_shell_nonzero_exit_is_reported_as_failure(tmp_path, monkeypatch):
    ctx = SimpleNamespace(repo_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 3, "", "permission denied")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    result = _run_shell(ctx, ["npm", "install", "-g", "@anthropic-ai/claude-code"])

    assert result.startswith("⚠️ SHELL_EXIT_ERROR:")
    assert "exit_code=3" in result
    assert "permission denied" in result


def test_run_shell_timeout_uses_settings_timeout(tmp_path, monkeypatch):
    ctx = SimpleNamespace(repo_dir=tmp_path)

    def fake_run(cmd, **kwargs):
        raise TimeoutError("wrong exception")

    def fake_timeout(cmd, **kwargs):
        raise __import__("subprocess").TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {"OUROBOROS_TOOL_TIMEOUT_SEC": 42})
    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_timeout)
    result = _run_shell(ctx, ["sleep", "999"])

    assert "TOOL_TIMEOUT (run_shell)" in result
    assert "42s" in result
