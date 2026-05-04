r"""Tests for the 2026-05-04 grep regex-escape hint.

Bash idiom ``grep "A\|B" file`` doesn't work in argv mode — backslashes
aren't expanded by the shell, so BSD grep on macOS treats ``\|`` as the
literal two-character sequence. Smaller models that learned the idiom
from bash scripts hit this. The hint catches it before subprocess fails
with a useless exit code.

Pinned tests so the regression class doesn't reopen.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ouroboros.tools.shell import _run_shell


def _ctx(tmp_path):
    return SimpleNamespace(repo_dir=tmp_path)


def test_grep_with_backslash_pipe_returns_hint(tmp_path):
    """Classic bash idiom ``grep -n "A\\|B" file`` — caught with hint."""
    ctx = _ctx(tmp_path)
    result = _run_shell(ctx, ["grep", "-n", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" in result
    assert "grep -E" in result
    assert "grep -e" in result


def test_grep_with_backslash_paren_passes_through(tmp_path, monkeypatch):
    """Backslash-paren is valid POSIX BRE grouping, not this hint class."""
    ctx = _ctx(tmp_path)

    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    result = _run_shell(ctx, ["grep", "\\(foo\\)", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_with_backslash_plus_passes_through(tmp_path, monkeypatch):
    """Backslash-plus is a grep BRE extension, but not the macOS \\| trap."""
    ctx = _ctx(tmp_path)

    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    result = _run_shell(ctx, ["grep", "ab\\+c", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_E_extended_regex_skips_hint(tmp_path, monkeypatch):
    """When -E is explicit, the user knows what they want; no hint."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    # -E uses ``|`` as alternation directly; ``\|`` would be a literal
    # pipe, but that's user choice.
    result = _run_shell(ctx, ["grep", "-E", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_clustered_E_flag_skips_hint(tmp_path, monkeypatch):
    """Combined short flags like -rnE are still explicit extended regex."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    result = _run_shell(ctx, ["grep", "-rnE", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_G_basic_regex_skips_hint(tmp_path, monkeypatch):
    """When -G is explicit, basic-regex with ``\\|`` is intentional (GNU)."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    result = _run_shell(ctx, ["grep", "-G", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_F_fixed_strings_skips_hint(tmp_path, monkeypatch):
    """-F means literal strings; backslash-pipe is just two chars."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    result = _run_shell(ctx, ["grep", "-F", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_legitimate_no_backslash_no_hint(tmp_path, monkeypatch):
    """Plain grep without escapes passes through unchanged."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "match\n", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    result = _run_shell(ctx, ["grep", "-n", "pattern", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in result


def test_egrep_and_fgrep_skip_hint(tmp_path, monkeypatch):
    """egrep/fgrep already choose regex/string flavor."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    assert "SHELL_REGEX_HINT" not in _run_shell(ctx, ["egrep", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" not in _run_shell(ctx, ["fgrep", "A\\|B", "/tmp/x"])


def test_non_grep_command_unaffected(tmp_path, monkeypatch):
    """The hint is grep-specific; other commands untouched."""
    ctx = _ctx(tmp_path)
    from subprocess import CompletedProcess

    def fake_run(cmd, **kwargs):
        return CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr("ouroboros.tools.shell._tracked_subprocess_run", fake_run)
    monkeypatch.setattr("ouroboros.tools.shell.load_settings", lambda: {})
    # echo with backslash-pipe is not grep — pass through.
    result = _run_shell(ctx, ["echo", "A\\|B"])
    assert "SHELL_REGEX_HINT" not in result


def test_grep_with_path_basename_caught(tmp_path):
    """Detection should match on the basename even if absolute path used."""
    ctx = _ctx(tmp_path)
    result = _run_shell(ctx, ["/usr/bin/grep", "A\\|B", "/tmp/x"])
    assert "SHELL_REGEX_HINT" in result
