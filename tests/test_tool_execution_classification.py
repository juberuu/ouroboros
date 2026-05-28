from ouroboros.loop_tool_execution import _extract_result_metadata, _is_tool_execution_failure


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
    core = "⚠️ CORE_PROTECTION_BLOCKED: claude_code_edit attempted to modify protected files."
    skill = "⚠️ SKILL_PAYLOAD_CONTROL_BLOCKED: claude_code_edit attempted to modify sidecars."

    assert _is_tool_execution_failure(True, core)
    assert _is_tool_execution_failure(True, skill)
    assert _extract_result_metadata("claude_code_edit", core, True)["status"] == "protected_blocked"
    assert _extract_result_metadata("claude_code_edit", skill, True)["status"] == "skill_payload_control_blocked"


def test_shell_regex_autocorrect_success_is_not_tool_failure():
    result = "⚠️ SHELL_REGEX_AUTO_CORRECTED: converted grep backslash-escaped alternation\nexit_code=0\nSTDOUT:\nmatch"
    assert not _is_tool_execution_failure(True, result)
    assert _extract_result_metadata("run_command", result, False)["status"] == "ok_autocorrected"


def test_shell_regex_autocorrect_nonzero_still_fails():
    result = (
        "⚠️ SHELL_REGEX_AUTO_CORRECTED: converted grep backslash-escaped alternation\n"
        "⚠️ SHELL_EXIT_ERROR: command exited with exit_code=2.\n\nSTDERR:\nboom"
    )
    assert _is_tool_execution_failure(True, result)
    assert _extract_result_metadata("run_command", result, True)["status"] == "shell_error"


def test_live_tool_log_payload_includes_structured_result_metadata(tmp_path):
    import pathlib
    import time
    from types import SimpleNamespace
    from ouroboros.loop_tool_execution import _execute_with_timeout

    source = (pathlib.Path(__file__).resolve().parents[1] / "ouroboros" / "loop_tool_execution.py").read_text(encoding="utf-8")

    assert '"status": result_meta.get("status")' in source
    assert '"exit_code": result_meta.get("exit_code")' in source
    assert '"signal": result_meta.get("signal")' in source
    drive_logs = tmp_path / "logs"
    drive_logs.mkdir()
    live_events = []
    tools = SimpleNamespace(
        CODE_TOOLS={"claude_code_edit"},
        _ctx=SimpleNamespace(event_queue=SimpleNamespace(put_nowait=lambda envelope: live_events.append(envelope))),
        execute=lambda _name, _args: (time.sleep(0.05), "OK")[1],
    )
    result = _execute_with_timeout(
        tools,
        {"id": "call-1", "function": {"name": "claude_code_edit", "arguments": "{}"}},
        drive_logs,
        timeout_sec=0.001,
        task_id="task-1",
    )

    assert result["result"] == "OK"
    payloads = [event.get("data") or {} for event in live_events]
    assert any(payload.get("type") == "tool_call_late" for payload in payloads)
    assert any(payload.get("terminal_wait") is True for payload in payloads)
