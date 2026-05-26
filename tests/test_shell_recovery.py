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


# ---------------------------------------------------------------------------
# Issue #40: filesystem-geometry observability
# ---------------------------------------------------------------------------
# run_shell must echo the resolved cwd on every result header so the agent
# can recover in one round from path-mismatch failures (data_root vs
# repo_root vs invented /tmp/... paths). The cwd parameter is now strict:
# absolute and nonexistent values are rejected with SHELL_CWD_ERROR
# instead of silently routing to repo_dir.


class TestRunShellCwdObservability:
    def test_success_header_echoes_absolute_cwd(self, tmp_path, monkeypatch):
        """exit_code=0 line must name the resolved working directory."""
        ctx = SimpleNamespace(repo_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "hello", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, ["echo", "hello"])
        assert "exit_code=0" in result
        assert f"cwd={tmp_path.resolve()}" in result

    def test_failure_header_echoes_absolute_cwd(self, tmp_path, monkeypatch):
        """SHELL_EXIT_ERROR must carry the cwd so a path-mismatch is
        diagnosable in one round."""
        ctx = SimpleNamespace(repo_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 3, "", "boom")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, ["nope"])
        assert "SHELL_EXIT_ERROR" in result
        assert "exit_code=3" in result
        assert f"cwd={tmp_path.resolve()}" in result

    def test_failure_with_signal_combines_signal_and_cwd_in_one_paren(self, tmp_path, monkeypatch):
        """signal= and cwd= ride in one parenthesised suffix, not two."""
        ctx = SimpleNamespace(repo_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, -9, "", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, ["sleep", "999"])
        assert "exit_code=-9" in result
        assert "signal=SIGKILL" in result
        assert f"cwd={tmp_path.resolve()}" in result
        # Single paren: signal=...) and cwd=...) MUST NOT appear as
        # two adjacent parens, the existing exit_code regex consumer
        # still parses, but the human-readable shape stays compact.
        assert "(signal=SIGKILL, cwd=" in result

    def test_timeout_header_echoes_cwd(self, tmp_path, monkeypatch):
        ctx = SimpleNamespace(repo_dir=tmp_path)

        def fake_timeout(cmd, **kwargs):
            raise __import__("subprocess").TimeoutExpired(cmd=cmd, timeout=kwargs["timeout"])

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_timeout)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, ["sleep", "999"])
        assert "TOOL_TIMEOUT (run_shell)" in result
        assert f"cwd={tmp_path.resolve()}" in result

    def test_exit_code_regex_still_parses_with_cwd_suffix(self, tmp_path, monkeypatch):
        """Downstream consumer in loop_tool_execution._EXIT_CODE_RE must
        keep parsing the exit code from the new ``exit_code=0 (cwd=...)``
        header. Pins the regex contract from the validation report."""
        import re
        ctx = SimpleNamespace(repo_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "ok", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, ["echo", "ok"])
        from ouroboros.loop_tool_execution import _EXIT_CODE_RE
        match = _EXIT_CODE_RE.search(result)
        assert match is not None
        assert int(match.group(1)) == 0


class TestRunShellCwdRejection:
    def test_absolute_posix_cwd_is_rejected(self, tmp_path):
        """Absolute POSIX cwd must return SHELL_CWD_ERROR instead of
        silently falling back to repo_dir (the old behaviour was the root
        cause of issue #40)."""
        ctx = SimpleNamespace(repo_dir=tmp_path)
        result = _run_shell(ctx, ["echo", "x"], cwd="/data/workspace/Ouroboros/tmp")
        assert "SHELL_CWD_ERROR" in result
        assert "absolute" in result.lower()
        # The error must name the repo_root so the agent learns the geometry.
        assert f"repo_root={tmp_path.resolve()}" in result

    def test_absolute_windows_cwd_is_rejected(self, tmp_path):
        """Windows-style absolute path is rejected even on POSIX hosts."""
        ctx = SimpleNamespace(repo_dir=tmp_path)
        result = _run_shell(ctx, ["echo", "x"], cwd="C:\\Users\\foo")
        assert "SHELL_CWD_ERROR" in result
        assert "absolute" in result.lower()

    def test_nonexistent_relative_cwd_is_rejected(self, tmp_path):
        """The silent fallback to repo_dir is gone; nonexistent relative
        cwd is now a hard SHELL_CWD_ERROR with the resolved candidate
        path so the agent can spot a typo immediately."""
        ctx = SimpleNamespace(repo_dir=tmp_path)
        result = _run_shell(ctx, ["echo", "x"], cwd="does_not_exist")
        assert "SHELL_CWD_ERROR" in result
        assert "does_not_exist" in result
        assert f"repo_root={tmp_path.resolve()}" in result

    def test_valid_relative_cwd_still_works(self, tmp_path, monkeypatch):
        """Existing relative-cwd behaviour is preserved: an existing
        subdir resolves and runs from there."""
        (tmp_path / "sub").mkdir()
        ctx = SimpleNamespace(repo_dir=tmp_path)

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cwd"] = kwargs.get("cwd")
            return CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        result = _run_shell(ctx, ["echo", "x"], cwd="sub")
        assert "SHELL_CWD_ERROR" not in result
        assert "exit_code=0" in result
        assert captured["cwd"] == str((tmp_path / "sub").resolve())
        assert f"cwd={(tmp_path / 'sub').resolve()}" in result

    def test_empty_and_dot_cwd_still_route_to_repo_root(self, tmp_path, monkeypatch):
        """Empty / "." / "./" cwd values keep their original meaning:
        run at the repo root. Regression guard for the no-cwd default."""
        ctx = SimpleNamespace(repo_dir=tmp_path)

        def fake_run(cmd, **kwargs):
            return CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
        monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
        for cwd_val in ("", ".", "./"):
            result = _run_shell(ctx, ["echo", "x"], cwd=cwd_val)
            assert "SHELL_CWD_ERROR" not in result
            assert f"cwd={tmp_path.resolve()}" in result
