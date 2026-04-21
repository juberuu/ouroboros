from ouroboros.loop_tool_execution import _is_tool_execution_failure


def test_review_blocked_is_not_treated_as_tool_failure():
    assert not _is_tool_execution_failure(True, "⚠️ REVIEW_BLOCKED: reviewers unavailable")


def test_domain_errors_are_not_treated_as_tool_failures():
    assert not _is_tool_execution_failure(True, "⚠️ GIT_ERROR (commit): hook rejected commit")
    assert not _is_tool_execution_failure(True, "⚠️ SAFETY_VIOLATION: blocked by sandbox")


def test_executor_failures_are_still_tool_failures():
    assert _is_tool_execution_failure(False, "anything")
    assert _is_tool_execution_failure(True, "⚠️ TOOL_ERROR (repo_commit): boom")
    assert _is_tool_execution_failure(True, "⚠️ TOOL_TIMEOUT (run_shell): exceeded 120s")


def test_shell_and_claude_failures_are_treated_as_tool_failures():
    assert _is_tool_execution_failure(
        True,
        "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=1.\n\nSTDERR:\nboom",
    )
    assert _is_tool_execution_failure(
        True,
        "⚠️ CLAUDE_CODE_INSTALL_ERROR: unable to install Claude Code.",
    )
    assert _is_tool_execution_failure(
        True,
        "⚠️ CLAUDE_CODE_UNAVAILABLE: ANTHROPIC_API_KEY not set.",
    )


def test_live_tool_log_payload_includes_structured_result_metadata():
    import pathlib

    source = (pathlib.Path(__file__).resolve().parents[1] / "ouroboros" / "loop_tool_execution.py").read_text(encoding="utf-8")

    assert '"status": result_meta.get("status")' in source
    assert '"exit_code": result_meta.get("exit_code")' in source
    assert '"signal": result_meta.get("signal")' in source
