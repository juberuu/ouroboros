"""Tests for the periodic self-check mechanism (`_maybe_inject_self_check`).

v4.34.0 redesign: the checkpoint is no longer a structured-reflection ceremony.
It is a plain `user` message inserted every 15 rounds carrying round/cost
summary, the last-N tool-call trace, and a short directed self-check prompt.
Tools remain enabled, reasoning effort is unchanged, and the message flows
through normal compaction — so the previous `_handle_checkpoint_response`,
`_is_valid_checkpoint_reflection`, `_record_checkpoint_artifact`, and
`_emit_checkpoint_{reflection,anomaly}_event` helpers are no longer needed
and were removed together with the `CHECKPOINT_*` constants and both
`task_checkpoint_reflection` / `task_checkpoint_anomaly` event types.

See docs/ARCHITECTURE.md (Loop checkpoint section) for the rationale
(0 valid reflections and 37 anomalies in production logs before rewrite).
"""

import json
import pathlib
import queue
import tempfile

from ouroboros.loop import (
    _build_recent_tool_trace,
    _maybe_inject_self_check,
)


class TestBuildRecentToolTrace:
    """Tests for _build_recent_tool_trace helper (P3 LLM-First: factual trace)."""

    def test_empty_messages(self):
        assert _build_recent_tool_trace([]) == ""

    def test_no_tool_calls(self):
        assert _build_recent_tool_trace([{"role": "user", "content": "hello"}]) == ""

    def test_builds_trace(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "repo_read", "arguments": '{"path": "a.py"}'}}
            ]},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "code_search", "arguments": '{"query": "foo"}'}}
            ]},
        ]
        result = _build_recent_tool_trace(messages)
        assert "repo_read" in result
        assert "code_search" in result
        assert "Recent tool calls" in result

    def test_repeated_calls_shown_factually(self):
        tc = {"function": {"name": "repo_read", "arguments": '{"path": "same.py"}'}}
        messages = [{"role": "assistant", "tool_calls": [tc]}] * 5
        result = _build_recent_tool_trace(messages)
        # All shown, no Python-side classification — the LLM decides what
        # "repeated calls" means for its current task (P3 LLM-First).
        assert result.count("repo_read") == 5

    def test_window_limit(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"function": {"name": f"tool_{i}", "arguments": "{}"}}
            ]} for i in range(30)
        ]
        result = _build_recent_tool_trace(messages, window=5)
        assert result.count("tool_") == 5


class TestMaybeInjectSelfCheck:
    """Cadence, content, and observability of the periodic self-check."""

    def test_injection_at_round_15(self):
        # Seed with a tool result as the last message so the injected
        # checkpoint is appended (rather than merged into an adjacent user
        # turn — see test_merges_into_previous_user_message_instead_of_appending
        # for that path).
        messages = [
            {"role": "user", "content": "initial task"},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "repo_read", "arguments": '{"path":"x.py"}'}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        usage = {"cost": 1.5}
        progress = []
        result = _maybe_inject_self_check(15, 200, messages, usage, progress.append)
        assert result is True
        assert len(messages) == 4
        assert messages[-1]["role"] == "user"
        assert "CHECKPOINT" in messages[-1]["content"]
        assert len(progress) == 1

    def test_injection_at_round_30(self):
        messages = [
            {"role": "user", "content": "initial task"},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        result = _maybe_inject_self_check(30, 200, messages, {"cost": 3.0}, lambda x: None)
        assert result is True
        assert "CHECKPOINT 2" in messages[-1]["content"]

    def test_no_injection_on_early_rounds(self):
        for r in [1, 2, 14, 16, 29, 31]:
            messages = []
            result = _maybe_inject_self_check(r, 200, messages, {"cost": 0}, lambda x: None)
            assert result is False, f"Should not inject at round {r}"
            assert len(messages) == 0

    def test_no_injection_when_no_spare_round_left(self):
        """Cadence guard: never inject on the last possible round."""
        messages = []
        result = _maybe_inject_self_check(15, 15, messages, {"cost": 0}, lambda x: None)
        assert result is False
        assert messages == []

    def test_message_role_is_user(self):
        """v4.34.0: self-check is a plain user message, not role=system.

        The previous system-role injection was absorbed into the top-level
        system prompt on Anthropic-via-OpenRouter, so the last message in
        the transcript was still the previous tool_result and the model
        continued by inertia. A user-role message forces the next turn to
        actually respond to the self-check.
        """
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        assert messages[-1]["role"] == "user"

    def test_prompt_has_self_check_language(self):
        """Prompt must frame itself as a periodic self-check (not ritual).

        Regression: the prompt must NOT mention a `finalize` tool — Ouroboros
        has no such tool in the registry; tasks end when the model replies
        with plain text and no tool call. Referring to a nonexistent tool
        would teach the model a false completion contract.
        """
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        lowered = content.lower()
        assert "periodic self-check" in lowered
        # Must explicitly disavow "please stop now" semantics so the model
        # doesn't mistake the checkpoint for a request to stop prematurely.
        assert "not a command to stop" in lowered
        # Must explain how to actually finish (plain-text response, no tool call).
        assert "final answer" in lowered
        assert "no tool call" in lowered
        # Must NOT refer to a nonexistent `finalize` tool anywhere in the prompt.
        assert "call finalize" not in lowered
        assert " finalize" not in lowered  # space-prefixed to allow unrelated words

    def test_prompt_asks_the_three_self_check_questions(self):
        """Prompt must surface the three directed self-check questions."""
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        lowered = messages[-1]["content"].lower()
        # Progress / repetition check.
        assert "making progress" in lowered or "repeating" in lowered
        # Approach / scope check.
        assert "current approach" in lowered or "narrow scope" in lowered
        # No format ritual — the new contract explicitly says so.
        assert "no special format required" in lowered

    def test_prompt_has_no_legacy_ritual(self):
        """The old four-field structured-reflection ceremony is gone.

        Regression guard: a future edit that accidentally re-introduces
        `CHECKPOINT_REFLECTION:`, `Known/Blocker/Decision/Next`, or the
        100-word limit will silently break the new minimalist contract.
        """
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "CHECKPOINT_REFLECTION:" not in content
        assert "100 words" not in content
        assert "anomaly" not in content.lower()

    def test_prompt_contains_cost_and_round(self):
        messages = [{"role": "user", "content": "test"}]
        _maybe_inject_self_check(15, 200, messages, {"cost": 7.42}, lambda x: None)
        content = messages[-1]["content"]
        assert "7.42" in content
        assert "15" in content

    def test_prompt_includes_tool_trace_when_calls_exist(self):
        """The last-N tool call trace must be present in the self-check body.

        Observing recent tool calls is the main signal the model uses to
        decide "am I still making progress or looping?".
        """
        messages = [
            {"role": "user", "content": "test"},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "repo_read", "arguments": '{"path": "a.py"}'}}
            ]},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "code_search", "arguments": '{"query": "x"}'}}
            ]},
        ]
        _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        content = messages[-1]["content"]
        assert "Recent tool calls" in content
        assert "repo_read" in content
        assert "code_search" in content

    def test_merges_into_previous_user_message_instead_of_appending(self):
        """Defense against consecutive user messages.

        If the previous message is already a `user` turn (e.g. an owner
        message drained by `_drain_incoming_messages` between rounds), the
        checkpoint reminder must be merged into that turn. Anthropic's
        Messages API rejects two consecutive user messages with a 400,
        so a naive append could drop the whole checkpoint round's LLM call.
        """
        messages = [
            {"role": "user", "content": "initial task"},
            {"role": "assistant", "tool_calls": [
                {"function": {"name": "repo_read", "arguments": '{"path":"x.py"}'}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": "owner follow-up mid-task"},
        ]
        result = _maybe_inject_self_check(15, 200, messages, {"cost": 0.5}, lambda x: None)
        assert result is True
        # Still four messages, not five — the checkpoint was merged.
        assert len(messages) == 4
        merged = messages[-1]
        assert merged["role"] == "user"
        # Prior content preserved.
        assert "owner follow-up mid-task" in merged["content"]
        # Checkpoint content present.
        assert "CHECKPOINT 1" in merged["content"]
        # Separator keeps the two turns visually distinct.
        assert "---" in merged["content"]

    def test_event_emitted_to_queue(self):
        messages = [{"role": "user", "content": "test"}]
        eq = queue.Queue()
        _maybe_inject_self_check(
            15, 200, messages, {"cost": 2.0}, lambda x: None,
            event_queue=eq, task_id="t1",
        )
        assert not eq.empty()
        event = eq.get_nowait()
        assert event["data"]["type"] == "task_checkpoint"
        assert event["data"]["task_id"] == "t1"
        assert eq.empty(), "must not emit twice"

    def test_event_fallback_to_file_when_no_queue(self):
        messages = [{"role": "user", "content": "test"}]
        with tempfile.TemporaryDirectory() as tmp:
            drive_logs = pathlib.Path(tmp)
            _maybe_inject_self_check(
                15, 200, messages, {"cost": 1.0}, lambda x: None,
                event_queue=None, task_id="t2", drive_logs=drive_logs,
            )
            ef = drive_logs / "events.jsonl"
            assert ef.exists()
            entry = json.loads(ef.read_text().strip())
            assert entry["type"] == "task_checkpoint"


class TestAdversarialPaths:
    """v4.34.0: adversarial paths around checkpoint injection.

    The old ceremony had `_handle_checkpoint_response` to classify malformed
    or empty model output. The new design has no such handler — the checkpoint
    is a plain user message and the model's reply is handled by the normal
    tool-loop. These tests pin the contract under adversarial loop states
    that `CHECKLISTS.md` flags as mandatory for loop changes (malformed /
    empty output, adjacent loop-state interaction).
    """

    def test_injection_merges_through_multiple_trailing_user_turns(self):
        """Two consecutive user messages already present — injection still
        merges cleanly without stacking a third one."""
        messages = [
            {"role": "user", "content": "initial task"},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": "first drained owner msg"},
            {"role": "user", "content": "second drained owner msg"},
        ]
        result = _maybe_inject_self_check(15, 200, messages, {"cost": 1.0}, lambda x: None)
        assert result is True
        # No new message — merged into the last one.
        assert len(messages) == 4
        assert messages[-1]["role"] == "user"
        assert "second drained owner msg" in messages[-1]["content"]
        assert "CHECKPOINT 1" in messages[-1]["content"]
        # The earlier user msg at index 2 is untouched.
        assert messages[2]["content"] == "first drained owner msg"

    def test_injection_preserves_multipart_list_content_on_trailing_user_turn(self):
        """Merge path must PRESERVE list-shaped content (appending a new text
        block) rather than flattening to a string.

        Regression guard for v4.34.0 (critical, found by scope review in
        simulation): `ouroboros/context.py::build_user_content` emits
        multipart `[{type: text, ...}, {type: image_url, ...}]` blocks for
        tasks with images from the photo bridge. Cache_control annotations
        also live on individual blocks. A naive `_extract_plain_text_from_content`
        flatten path would silently drop image_url blocks and any
        cache_control markers, breaking downstream multimodal handling in
        `ouroboros/llm.py::_anthropic_blocks_from_content`.
        """
        messages = [
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
            {"role": "user", "content": [
                {"type": "text", "text": "owner msg with a photo"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,FAKE=="}},
                {"type": "text", "text": "second text block"},
            ]},
        ]
        result = _maybe_inject_self_check(15, 200, messages, {"cost": 0.5}, lambda x: None)
        assert result is True
        assert len(messages) == 2
        merged = messages[-1]
        assert merged["role"] == "user"
        # Content must still be a list — not flattened to a string.
        assert isinstance(merged["content"], list), (
            "multipart prior content must be preserved as a list; "
            "flattening would drop image_url and cache_control blocks"
        )
        # Original blocks still present and in order.
        assert merged["content"][0] == {"type": "text", "text": "owner msg with a photo"}
        assert merged["content"][1]["type"] == "image_url"
        assert merged["content"][1]["image_url"]["url"].startswith("data:image/png")
        assert merged["content"][2] == {"type": "text", "text": "second text block"}
        # Checkpoint appended as a new text block at the end.
        assert merged["content"][-1]["type"] == "text"
        assert "CHECKPOINT 1" in merged["content"][-1]["text"]

    def test_injection_on_empty_messages_list(self):
        """Empty `messages` at injection time (defensive — should never happen
        in a real loop but must not crash)."""
        messages: list = []
        result = _maybe_inject_self_check(15, 200, messages, {"cost": 0}, lambda x: None)
        assert result is True
        # Single user message appended (no prior turn to merge with).
        assert len(messages) == 1
        assert messages[0]["role"] == "user"
        assert "CHECKPOINT 1" in messages[0]["content"]


class TestLegacyCeremonyRemoved:
    """Regression: the retired checkpoint ceremony must stay removed.

    These names were part of the v4.33.x audit-only protocol and were
    retired in v4.34.0 after 0/37 reflection/anomaly ratio in production.
    If a future change re-imports or re-adds them, this test fails loudly.
    """

    def test_legacy_helpers_not_importable(self):
        from ouroboros import loop
        for name in (
            "CHECKPOINT_REFLECTION_HEADER",
            "CHECKPOINT_ANOMALY_HEADER",
            "CHECKPOINT_CONTINUE_PROMPT",
            "_is_valid_checkpoint_reflection",
            "_handle_checkpoint_response",
            "_record_checkpoint_artifact",
            "_emit_checkpoint_reflection_event",
            "_emit_checkpoint_anomaly_event",
        ):
            assert not hasattr(loop, name), (
                f"ouroboros.loop.{name} was retired in v4.34.0 and must not be "
                "re-introduced. See docs/ARCHITECTURE.md Loop checkpoint section."
            )
