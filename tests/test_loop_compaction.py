from types import SimpleNamespace


def _messages(count=41):
    return [{"role": "assistant", "content": f"msg-{idx}"} for idx in range(count)]


def test_routine_compaction_runs_for_low_remote_but_not_max_remote(monkeypatch, tmp_path):
    from ouroboros import loop

    calls = []

    def fake_checkpoint(messages, **kwargs):
        calls.append(("checkpoint", kwargs["reason"], kwargs["keep_recent"]))
        return True

    def fake_compact(messages, keep_recent, **kwargs):
        calls.append(("compact", keep_recent, kwargs.get("drive_root"), kwargs.get("task_id")))
        return [{"role": "system", "content": "compacted"}], {"prompt_tokens": 1}

    monkeypatch.setattr(loop, "_persist_compaction_checkpoint", fake_checkpoint)
    monkeypatch.setattr(loop, "compact_tool_history_llm", fake_compact)

    base = dict(
        tools=SimpleNamespace(_ctx=SimpleNamespace(_pending_compaction=None)),
        drive_root=tmp_path,
        drive_logs=tmp_path / "logs",
        task_id="task-1",
        round_idx=7,
        event_queue=None,
        checkpoint_injected=False,
        emit_progress=lambda _msg: None,
    )

    low_messages, low_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=False, active_context_mode="low", **base),
    )
    assert low_messages == [{"role": "system", "content": "compacted"}]
    assert low_usage == {"prompt_tokens": 1}
    assert calls == [("checkpoint", "routine", 20), ("compact", 20, tmp_path, "task-1")]

    calls.clear()
    max_messages, max_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=False, active_context_mode="max", **base),
    )
    assert len(max_messages) == 41
    assert max_usage is None
    assert calls == []

    local_messages, local_usage = loop._run_round_compaction(
        _messages(),
        loop._CompactionRoundContext(active_use_local=True, active_context_mode="max", **base),
    )
    assert local_messages == [{"role": "system", "content": "compacted"}]
    assert local_usage == {"prompt_tokens": 1}


def test_context_compaction_observability_uses_current_task_drive(monkeypatch, tmp_path):
    from ouroboros import context_compaction
    from ouroboros import llm_observability

    seen = {}

    def fake_chat_observed(_client, **kwargs):
        seen.update(kwargs)
        return {"content": "[round:1]\nsummary"}, {"prompt_tokens": 1}

    monkeypatch.setattr(llm_observability, "chat_observed", fake_chat_observed)
    monkeypatch.setattr(context_compaction, "LLMClient", lambda: object(), raising=False)

    summary, usage = context_compaction._summarize_round_batch(
        [(1, "TOOL_CALL x: {}")],
        drive_root=tmp_path,
        task_id="task-42",
    )

    assert summary == {1: "summary"}
    assert usage == {"prompt_tokens": 1}
    assert seen["drive_root"] == tmp_path
    assert seen["task_id"] == "task-42"
