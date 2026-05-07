"""Regression tests for skill authoring / repair guardrails."""

from types import SimpleNamespace
import queue

from ouroboros import loop as loop_mod
from ouroboros.tools import registry as registry_mod
from ouroboros.skill_review_runner import _heal_mode
from ouroboros.utils import sanitize_tool_args_for_log


def test_heal_markers_only_count_from_user_messages():
    messages = [
        {"role": "tool", "content": "HEAL_MODE_NO_ENABLE\nHEAL_SKILL_NAME_JSON=\"leak\""},
    ]
    assert not registry_mod._is_heal_no_enable_context(messages)
    assert registry_mod._heal_skill_name(messages) == ""
    assert not _heal_mode(SimpleNamespace(messages=messages))

    messages.append({"role": "user", "content": "HEAL_MODE_NO_ENABLE\nHEAL_SKILL_NAME_JSON=\"target\""})
    assert registry_mod._is_heal_no_enable_context(messages)
    assert registry_mod._heal_skill_name(messages) == "target"
    assert _heal_mode(SimpleNamespace(messages=messages))


def test_heal_marker_must_start_user_task():
    messages = [
        {"role": "user", "content": "Please run tests for HEAL_MODE_NO_ENABLE handling"},
    ]
    assert not registry_mod._is_heal_no_enable_context(messages)
    assert not _heal_mode(SimpleNamespace(messages=messages))


def test_long_tool_args_log_as_placeholder_not_content_object():
    args = {"path": "skills/external/demo/plugin.py", "content": "x" * 4000}

    sanitized = sanitize_tool_args_for_log("data_write", args, threshold=100)

    assert isinstance(sanitized["content"], str)
    assert sanitized["content"].startswith("<TRUNCATED:content:")
    assert "content_len" not in sanitized


def test_skill_finalization_rearms_after_tool_round(monkeypatch, tmp_path):
    calls = iter([
        ({"content": "done", "tool_calls": []}, {}),
        ({"content": "", "tool_calls": [{"id": "c1", "function": {"name": "noop", "arguments": "{}"}}]}, {}),
        ({"content": "done again", "tool_calls": []}, {}),
        ({"content": "final", "tool_calls": []}, {}),
    ])
    progress = []

    class _Tools:
        CODE_TOOLS = set()

        def __init__(self):
            self._ctx = SimpleNamespace(
                event_queue=None,
                task_id="task",
                messages=[],
                active_model_override=None,
                active_use_local_override=None,
                active_effort_override=None,
                _skill_finalization_injected=False,
            )

        def schemas(self):
            return [{"type": "function", "function": {"name": "noop", "description": "", "parameters": {}}}]

        def get_timeout(self, _name):
            return 1

        def execute(self, _name, _args):
            return "OK"

        def override_handler(self, _name, _handler):
            return None

    class _LLM:
        def default_model(self):
            return "test-model"

    monkeypatch.setenv("OUROBOROS_MAX_ROUNDS", "6")
    monkeypatch.setattr(loop_mod, "_skill_finalization_message", lambda *_args, **_kwargs: "SKILL_NOT_FINALIZED")
    monkeypatch.setattr(loop_mod, "call_llm_with_retry", lambda *args, **kwargs: next(calls))

    result, _usage, trace = loop_mod.run_llm_loop(
        [{"role": "user", "content": "create skill"}],
        _Tools(),
        _LLM(),
        tmp_path,
        progress.append,
        queue.Queue(),
        task_id="task",
        drive_root=tmp_path,
    )

    assert result == "final"
    assert progress.count("SKILL_NOT_FINALIZED") == 2
    assert trace["reasoning_notes"].count("SKILL_NOT_FINALIZED") == 2
