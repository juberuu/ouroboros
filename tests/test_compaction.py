"""Tests for tool-history compaction protection (context_compaction.py)."""
from ouroboros.context_compaction import compact_tool_history


def _make_messages(tool_name: str, result_content: str, num_rounds: int = 8):
    """Build a message list with num_rounds of tool calls, all using the same tool."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": "{}"},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": result_content,
        })
    return messages


def _make_large_arg_messages(tool_name: str, num_rounds: int = 8):
    """Build messages whose old assistant tool-call payloads should compact."""
    messages = [{"role": "system", "content": [{"type": "text", "text": "system"}]}]
    large_args = '{"content": "' + ("x" * 1000) + '"}'
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{
                "id": tc_id,
                "function": {"name": tool_name, "arguments": large_args},
            }],
        })
        messages.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": "ok",
        })
    return messages


def test_protected_tool_results_survive_compaction():
    """repo_commit results must not be truncated even in old rounds."""
    original_result = "OK: committed to ouroboros: v3.19.0 review feedback applied"
    msgs = _make_messages("commit_reviewed", original_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    commit_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == original_result
    ]
    assert len(commit_results) == 10, "All repo_commit results must survive compaction"


def test_warning_results_survive_compaction():
    """Results starting with warning emoji must not be truncated."""
    warn_result = "\u26a0\ufe0f REVIEW_BLOCKED: tests failed, commit rejected. Fix errors first."
    msgs = _make_messages("run_command", warn_result, num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    warning_results = [
        m["content"] for m in compacted
        if m.get("role") == "tool" and m["content"] == warn_result
    ]
    assert len(warning_results) == 10, "Warning-prefixed results must survive compaction"


def test_old_assistant_tool_payloads_are_compacted():
    """Fallback compaction should compact oversized old assistant tool-call payloads."""
    msgs = _make_large_arg_messages("write_file", num_rounds=10)
    compacted = compact_tool_history(msgs, keep_recent=3)

    compacted_assistants = [
        m for m in compacted
        if m.get("role") == "assistant"
        and m.get("tool_calls")
        and "<<CONTENT_OMITTED len=" in m["tool_calls"][0]["function"]["arguments"]
    ]
    assert len(compacted_assistants) >= 4, "Old oversized assistant tool-call payloads should be compacted"


# ── Protected-content detection ──────────────────────────────────────────────
#
# v4.34.0: the structured-reflection checkpoint ceremony was retired, so
# assistant messages with `CHECKPOINT_REFLECTION` / `CHECKPOINT_ANOMALY`
# text no longer need compaction protection — they no longer exist. The
# remaining protected-content rule covers tool-result messages for
# critical tools and explicit error markers (`⚠️`-prefixed tool output).


def test_round_has_protected_content_ignores_normal_assistant_text():
    """Normal assistant messages (no tool role, no error marker) must not be protected.

    Previously the function also protected `CHECKPOINT_REFLECTION` /
    `CHECKPOINT_ANOMALY` markers; that branch was removed in v4.34.0 along
    with the audit-only checkpoint ceremony. This test guards against a
    regression that would re-introduce any checkpoint-text protection.
    """
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "Normal reasoning without any reflection marker",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "file content",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is False


def test_round_has_protected_content_does_not_protect_checkpoint_text():
    """v4.34.0 regression guard: legacy CHECKPOINT_REFLECTION text is no longer
    protected. A future edit that accidentally re-adds the assistant-content
    detection would silently bloat transcripts with stale audit artifacts.
    """
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "CHECKPOINT_REFLECTION:\n- Known: x\n- Blocker: none",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    assert _round_has_protected_content(messages, 0, 1) is False


def test_round_has_protected_content_protects_error_tool_results():
    """Tool-result messages prefixed with ⚠️ remain protected from compaction —
    this was the other half of the pre-v4.34.0 rule and is unaffected by the
    checkpoint refactor.
    """
    from ouroboros.context_compaction import _round_has_protected_content

    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "c1",
            "content": "⚠️ failed to read path: permission denied",
        },
    ]
    assert _round_has_protected_content(messages, 0, 1) is True


def _round_with_result(content: str):
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": "run_command", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": content},
    ]


def test_autocorrect_prefixed_warning_is_still_protected():
    """shell can prepend an autocorrect note BEFORE the ⚠️ line; the old
    startswith check silently unprotected such warnings."""
    from ouroboros.context_compaction import _round_has_protected_content

    messages = _round_with_result(
        "Note: autocorrected 'gti' -> 'git'\n⚠️ REVIEW_BLOCKED: tests failed"
    )
    assert _round_has_protected_content(messages, 0, 1) is True


def test_blank_line_separated_warning_is_still_protected():
    """A blank line between the autocorrect note and the ⚠️ marker must not
    defeat protection — the scan counts non-empty lines only."""
    from ouroboros.context_compaction import _round_has_protected_content

    messages = _round_with_result(
        "Note: autocorrected 'gti' -> 'git'\n\n⚠️ REVIEW_BLOCKED: tests failed"
    )
    assert _round_has_protected_content(messages, 0, 1) is True


def test_failed_text_fallback_still_carries_structured_spend(monkeypatch, tmp_path):
    """Structured-call spend survives a text-protocol fallback failure via
    _BatchSummaryError so the caller can account it."""
    import pytest

    from ouroboros import context_compaction, llm_observability

    calls = []

    def fake_chat_observed(_client, **kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            # Structured call: spend incurred, but no parseable summaries.
            return {"content": "free-form prose without round markers"}, {"prompt_tokens": 11}
        raise RuntimeError("text protocol call exploded")

    monkeypatch.setattr(llm_observability, "chat_observed", fake_chat_observed)
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)

    with pytest.raises(context_compaction._BatchSummaryError) as exc_info:
        context_compaction._summarize_round_batch(
            [(2, "TOOL_CALL x: {}")], drive_root=tmp_path, task_id="t"
        )
    assert exc_info.value.usage == {"prompt_tokens": 11}
    assert len(calls) == 2


def test_shell_exit_error_rounds_are_compactable():
    """SHELL_EXIT_ERROR rounds are trial-and-error history that MUST compact
    (the summarizer keeps the first error line); plain and autocorrect-prefixed
    shapes are both exempt from ⚠️ protection."""
    from ouroboros.context_compaction import _round_has_protected_content

    plain = _round_with_result("⚠️ SHELL_EXIT_ERROR exit=1\nTraceback (most recent call last): ...")
    assert _round_has_protected_content(plain, 0, 1) is False

    prefixed = _round_with_result(
        "Note: autocorrected 'pyhton' -> 'python'\n⚠️ SHELL_EXIT_ERROR exit=127\ncommand not found"
    )
    assert _round_has_protected_content(prefixed, 0, 1) is False


# ── LLM compaction batch isolation + structured protocol ────────────────────


def _make_llm_round_messages(num_rounds: int):
    messages = [{"role": "system", "content": "system"}]
    for i in range(num_rounds):
        tc_id = f"call_{i}"
        messages.append({
            "role": "assistant",
            "content": f"Round {i}",
            "tool_calls": [{"id": tc_id, "function": {"name": "read_file", "arguments": "{}"}}],
        })
        messages.append({"role": "tool", "tool_call_id": tc_id, "content": f"result {i}"})
    return messages


def test_failed_batch_keeps_other_batches(monkeypatch, tmp_path):
    """One failed batch leaves only ITS rounds raw; other batches still
    compact, and spend from the failed batch is still accounted."""
    from ouroboros import context_compaction

    calls = []

    def fake_batch(rendered_blocks, *, drive_root, task_id):
        calls.append([start for start, _ in rendered_blocks])
        if len(calls) == 1:
            raise context_compaction._BatchSummaryError(
                "boom", usage={"prompt_tokens": 7, "cost": 0.01}
            )
        return (
            {start: f"summary-{start}" for start, _ in rendered_blocks},
            {"prompt_tokens": 3, "cost": 0.02},
        )

    monkeypatch.setattr(context_compaction, "_summarize_round_batch", fake_batch)
    messages = _make_llm_round_messages(20)  # 16 compactable -> 2 batches of 8

    compacted, usage = context_compaction.compact_tool_history_llm(
        messages, keep_recent=4, drive_root=tmp_path, task_id="t"
    )

    assert len(calls) == 2
    summaries = [m for m in compacted if str(m.get("content") or "").startswith("[Compacted reasoning block]")]
    assert len(summaries) == 8  # second batch compacted
    raw_rounds = [m for m in compacted if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(raw_rounds) == 12  # 8 raw from failed batch + 4 kept recent
    # Spend from BOTH the failed and the successful batch is accounted.
    assert usage["prompt_tokens"] == 10
    assert abs(usage["cost"] - 0.03) < 1e-9


def test_missing_round_summary_degrades_only_that_round(monkeypatch, tmp_path):
    """A summary missing for one round leaves that round raw instead of
    failing the whole batch (the old completeness ValueError)."""
    from ouroboros import context_compaction

    def fake_batch(rendered_blocks, *, drive_root, task_id):
        starts = [start for start, _ in rendered_blocks]
        return (
            {start: f"summary-{start}" for start in starts if start != starts[0]},
            {"prompt_tokens": 1},
        )

    monkeypatch.setattr(context_compaction, "_summarize_round_batch", fake_batch)
    messages = _make_llm_round_messages(10)  # 6 compactable -> 1 batch

    compacted, _usage = context_compaction.compact_tool_history_llm(
        messages, keep_recent=4, drive_root=tmp_path, task_id="t"
    )

    summaries = [m for m in compacted if str(m.get("content") or "").startswith("[Compacted reasoning block]")]
    assert len(summaries) == 5  # all but the degraded round
    raw_rounds = [m for m in compacted if m.get("role") == "assistant" and m.get("tool_calls")]
    assert len(raw_rounds) == 5  # 1 degraded + 4 kept recent


def test_structured_protocol_parses_pinned_tool_call(monkeypatch, tmp_path):
    """The structured emit_round_summaries protocol is preferred and parsed
    from the pinned tool call."""
    import json as _json

    from ouroboros import context_compaction, llm_observability

    seen = {}

    def fake_chat_observed(_client, **kwargs):
        seen.update(kwargs)
        return (
            {
                "content": "",
                "tool_calls": [{
                    "id": "tc1",
                    "function": {
                        "name": "emit_round_summaries",
                        "arguments": _json.dumps({
                            "summaries": [
                                {"round_id": 1, "summary": "did a thing"},
                                {"round_id": 3, "summary": "did another"},
                            ]
                        }),
                    },
                }],
            },
            {"prompt_tokens": 5},
        )

    monkeypatch.setattr(llm_observability, "chat_observed", fake_chat_observed)
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)

    summary_map, usage = context_compaction._summarize_round_batch(
        [(1, "TOOL_CALL x: {}"), (3, "TOOL_CALL y: {}")],
        drive_root=tmp_path,
        task_id="t",
    )

    assert summary_map == {1: "did a thing", 3: "did another"}
    assert usage == {"prompt_tokens": 5}
    assert seen["tools"] == [context_compaction._ROUND_SUMMARIES_TOOL]
    assert seen["tool_choice"] == "required"


def test_structured_failure_falls_back_to_text_protocol(monkeypatch, tmp_path):
    """If the structured call raises (provider rejects tools), the text
    protocol retry still summarizes and usage from BOTH calls is merged."""
    from ouroboros import context_compaction, llm_observability

    calls = []

    def fake_chat_observed(_client, **kwargs):
        calls.append(kwargs)
        if "tools" in kwargs:
            raise RuntimeError("provider rejects tool_choice=required")
        return {"content": "[round:2]\nrecovered summary"}, {"prompt_tokens": 4}

    monkeypatch.setattr(llm_observability, "chat_observed", fake_chat_observed)
    monkeypatch.delenv("USE_LOCAL_LIGHT", raising=False)

    summary_map, usage = context_compaction._summarize_round_batch(
        [(2, "TOOL_CALL x: {}")],
        drive_root=tmp_path,
        task_id="t",
    )

    assert summary_map == {2: "recovered summary"}
    assert len(calls) == 2
    assert usage == {"prompt_tokens": 4}  # structured call raised before usage
