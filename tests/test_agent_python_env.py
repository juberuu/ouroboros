"""Regression tests for the agent interpreter handle.

The bundled app must run pytest through the same interpreter that launched
Ouroboros. Plain `pytest` or `python -m pytest` can resolve to the wrong
runtime in packaged builds.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest


def test_server_py_injects_agent_python_env_var():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    source = (repo_root / "server.py").read_text(encoding="utf-8")
    assert 'os.environ.get("OUROBOROS_AGENT_PYTHON")' in source
    assert 'os.environ["OUROBOROS_AGENT_PYTHON"]' in source
    assert "sys.executable" in source
    assert "isinstance" in source and "_agent_python" in source


def test_preflight_test_runner_uses_sys_executable():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    source = (repo_root / "ouroboros" / "tools" / "review_helpers.py").read_text(encoding="utf-8")
    assert '["pytest", "tests/"' not in source
    assert '"-m", "pytest"' in source
    assert "sys.executable" in source


def test_git_pre_push_tests_uses_sys_executable():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    source = (repo_root / "ouroboros" / "tools" / "git.py").read_text(encoding="utf-8")
    assert '["pytest", "tests/"' not in source
    assert '"-m", "pytest"' in source
    assert "sys.executable" in source
    assert "OUROBOROS_AGENT_PYTHON" in source


def test_shell_validation_uses_sys_executable():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    source = (repo_root / "ouroboros" / "tools" / "shell.py").read_text(encoding="utf-8")
    assert '["python", "-m", "pytest"' not in source
    assert '"-m", "pytest"' in source
    assert "sys.executable" in source
    assert "OUROBOROS_AGENT_PYTHON" in source


def test_sys_executable_minus_m_pytest_exits_zero():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--version"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"sys.executable -m pytest --version exited {result.returncode}; "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "pytest" in (result.stdout + result.stderr).lower()


def test_agent_python_env_var_points_to_usable_python():
    agent_python = os.environ.get("OUROBOROS_AGENT_PYTHON")
    if not agent_python:
        pytest.skip("OUROBOROS_AGENT_PYTHON is not set in this unit-test run")
    result = subprocess.run(
        [agent_python, "-c", "print('ok')"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_requirements_txt_pins_pytest():
    repo_root = pathlib.Path(__file__).resolve().parent.parent
    reqs = (repo_root / "requirements.txt").read_text(encoding="utf-8")
    for raw_line in reqs.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name = line.split(";", 1)[0].split("[", 1)[0]
        for sep in (">=", "<=", "==", "~=", ">", "<", "!="):
            if sep in name:
                name = name.split(sep, 1)[0]
                break
        if name.strip().lower() == "pytest":
            return
    raise AssertionError("requirements.txt must include pytest")
