"""v6.37.0 guard (C6.6): a shell redirect GLUED into one argv element
(["find", ..., "2>/dev/null"]) must be caught with the actionable [sh,-c,...] hint
before subprocess runs — the old standalone-operator set only caught a bare ">"
element, so "2>/dev/null" reached `find` as a literal arg and died cryptically.
A '>' inside a sed/awk/grep expression must NOT be misflagged."""

import pathlib
from types import SimpleNamespace

import pytest

from ouroboros.tools.shell import _GLUED_REDIRECT_RE, _run_shell


def _ctx(tmp_path):
    return SimpleNamespace(repo_dir=tmp_path, drive_logs=lambda: pathlib.Path(str(tmp_path)))


@pytest.mark.parametrize(
    "arg",
    ["2>/dev/null", "2>&1", ">out.log", ">>app.log", "<in.txt", "&>all.log", ">&2", "1>x", "2>>err"],
)
def test_glued_redirect_detected(arg):
    assert _GLUED_REDIRECT_RE.match(arg)


@pytest.mark.parametrize(
    "arg",
    ["s/a>b/c/g", "find", "-name", "*.txt", "foo|bar", "x>y", "> hi", "report2024", "-->flag", "2", "."],
)
def test_legit_args_not_flagged(arg):
    assert not _GLUED_REDIRECT_RE.match(arg)


def test_run_shell_blocks_glued_redirect(tmp_path):
    out = _run_shell(_ctx(tmp_path), cmd=["find", ".", "-name", "*.py", "2>/dev/null"])
    assert "SHELL_CMD_ERROR" in out
    assert "2>/dev/null" in out
    assert "sh" in out  # points to the ["sh","-c",...] escape hatch
