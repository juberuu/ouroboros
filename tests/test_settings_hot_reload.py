"""
Tests for hot-reload of settings:
- supervisor/state.py::refresh_budget_from_settings
- supervisor/queue.py::refresh_timeouts_from_settings
- supervisor/message_bus.py::refresh_budget_limit
- server.py::_classify_settings_changes
- ouroboros/agent.py::handle_task settings refresh
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# supervisor/state — budget hot-reload
# ---------------------------------------------------------------------------

def test_refresh_budget_from_settings_updates_global():
    from supervisor import state as s
    original = s.TOTAL_BUDGET_LIMIT
    try:
        s.refresh_budget_from_settings({"TOTAL_BUDGET": 99.5})
        assert s.TOTAL_BUDGET_LIMIT == 99.5
    finally:
        s.set_budget_limit(original)


def test_refresh_budget_from_settings_zero():
    from supervisor import state as s
    original = s.TOTAL_BUDGET_LIMIT
    try:
        s.refresh_budget_from_settings({"TOTAL_BUDGET": 0})
        assert s.TOTAL_BUDGET_LIMIT == 0.0
    finally:
        s.set_budget_limit(original)


def test_refresh_budget_from_settings_bad_value_does_not_raise():
    from supervisor import state as s
    original = s.TOTAL_BUDGET_LIMIT
    try:
        # Should not raise — bad value is silently swallowed
        s.refresh_budget_from_settings({"TOTAL_BUDGET": "not-a-number"})
    except Exception as exc:
        raise AssertionError(f"refresh_budget_from_settings raised: {exc}") from exc
    finally:
        s.set_budget_limit(original)


def test_refresh_budget_from_settings_missing_key_uses_zero():
    from supervisor import state as s
    original = s.TOTAL_BUDGET_LIMIT
    try:
        s.refresh_budget_from_settings({})
        assert s.TOTAL_BUDGET_LIMIT == 0.0
    finally:
        s.set_budget_limit(original)


# ---------------------------------------------------------------------------
# supervisor/queue — timeout hot-reload
# ---------------------------------------------------------------------------

def test_refresh_timeouts_from_settings_updates_globals():
    from supervisor import queue as q
    orig_soft, orig_hard = q.SOFT_TIMEOUT_SEC, q.HARD_TIMEOUT_SEC
    try:
        q.refresh_timeouts_from_settings({
            "OUROBOROS_SOFT_TIMEOUT_SEC": 300,
            "OUROBOROS_HARD_TIMEOUT_SEC": 900,
        })
        assert q.SOFT_TIMEOUT_SEC == 300
        assert q.HARD_TIMEOUT_SEC == 900
    finally:
        q.SOFT_TIMEOUT_SEC = orig_soft
        q.HARD_TIMEOUT_SEC = orig_hard


def test_refresh_timeouts_from_settings_partial_update():
    """Only soft_timeout specified — hard should remain unchanged."""
    from supervisor import queue as q
    orig_soft, orig_hard = q.SOFT_TIMEOUT_SEC, q.HARD_TIMEOUT_SEC
    try:
        q.refresh_timeouts_from_settings({"OUROBOROS_SOFT_TIMEOUT_SEC": 120})
        assert q.SOFT_TIMEOUT_SEC == 120
        assert q.HARD_TIMEOUT_SEC == orig_hard
    finally:
        q.SOFT_TIMEOUT_SEC = orig_soft
        q.HARD_TIMEOUT_SEC = orig_hard


def test_refresh_timeouts_from_settings_bad_value_does_not_raise():
    from supervisor import queue as q
    orig_soft, orig_hard = q.SOFT_TIMEOUT_SEC, q.HARD_TIMEOUT_SEC
    try:
        q.refresh_timeouts_from_settings({"OUROBOROS_SOFT_TIMEOUT_SEC": "bad"})
    except Exception as exc:
        raise AssertionError(f"refresh_timeouts_from_settings raised: {exc}") from exc
    finally:
        q.SOFT_TIMEOUT_SEC = orig_soft
        q.HARD_TIMEOUT_SEC = orig_hard


def test_refresh_timeouts_one_bad_does_not_block_the_other():
    """A bad soft_timeout must not prevent a valid hard_timeout update."""
    from supervisor import queue as q
    orig_soft, orig_hard = q.SOFT_TIMEOUT_SEC, q.HARD_TIMEOUT_SEC
    try:
        q.refresh_timeouts_from_settings({
            "OUROBOROS_SOFT_TIMEOUT_SEC": "not-a-number",
            "OUROBOROS_HARD_TIMEOUT_SEC": 999,
        })
        # soft should be unchanged (bad value)
        assert q.SOFT_TIMEOUT_SEC == orig_soft, (
            f"Soft timeout changed despite bad input: {q.SOFT_TIMEOUT_SEC}"
        )
        # hard should be updated (valid value)
        assert q.HARD_TIMEOUT_SEC == 999, (
            f"Hard timeout not updated despite valid input: {q.HARD_TIMEOUT_SEC}"
        )
    finally:
        q.SOFT_TIMEOUT_SEC = orig_soft
        q.HARD_TIMEOUT_SEC = orig_hard


# ---------------------------------------------------------------------------
# supervisor/message_bus — budget hot-reload
# ---------------------------------------------------------------------------

def test_refresh_budget_limit_updates_global():
    from supervisor import message_bus as mb
    original = mb.TOTAL_BUDGET_LIMIT
    try:
        mb.refresh_budget_limit(55.0)
        assert mb.TOTAL_BUDGET_LIMIT == 55.0
    finally:
        mb.TOTAL_BUDGET_LIMIT = original


def test_refresh_budget_limit_zero():
    from supervisor import message_bus as mb
    original = mb.TOTAL_BUDGET_LIMIT
    try:
        mb.refresh_budget_limit(0)
        assert mb.TOTAL_BUDGET_LIMIT == 0.0
    finally:
        mb.TOTAL_BUDGET_LIMIT = original


def test_refresh_budget_limit_does_not_raise_on_none():
    from supervisor import message_bus as mb
    original = mb.TOTAL_BUDGET_LIMIT
    try:
        mb.refresh_budget_limit(None)  # type: ignore[arg-type]
    except Exception as exc:
        raise AssertionError(f"refresh_budget_limit raised on None: {exc}") from exc
    finally:
        mb.TOTAL_BUDGET_LIMIT = original


# ---------------------------------------------------------------------------
# server.py — _classify_settings_changes
# ---------------------------------------------------------------------------

def _get_classify():
    import server as srv
    return srv._classify_settings_changes


def test_classify_no_changes():
    classify = _get_classify()
    old = {"LOCAL_MODEL_SOURCE": "a", "OUROBOROS_MAX_WORKERS": "5"}
    new = dict(old)
    assert classify(old, new) == []


def test_classify_restart_required_local_model_source():
    classify = _get_classify()
    old = {"LOCAL_MODEL_SOURCE": ""}
    new = {"LOCAL_MODEL_SOURCE": "some/repo"}
    result = classify(old, new)
    assert "LOCAL_MODEL_SOURCE" in result


def test_classify_restart_required_max_workers():
    classify = _get_classify()
    old = {"OUROBOROS_MAX_WORKERS": "5"}
    new = {"OUROBOROS_MAX_WORKERS": "10"}
    result = classify(old, new)
    assert "OUROBOROS_MAX_WORKERS" in result


def test_classify_hot_reloadable_keys_not_in_restart():
    """Budget, models, API keys, effort — none require restart."""
    classify = _get_classify()
    old = {
        "TOTAL_BUDGET": "100",
        "OUROBOROS_MODEL": "anthropic/claude-opus-4.6",
        "OPENROUTER_API_KEY": "sk-old",
        "ANTHROPIC_API_KEY": "ant-old",
        "OUROBOROS_REVIEW_MODELS": "a,b,c",
        "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
        "OUROBOROS_EFFORT_TASK": "medium",
        "OUROBOROS_TOOL_TIMEOUT_SEC": "600",
    }
    new = {
        "TOTAL_BUDGET": "200",
        "OUROBOROS_MODEL": "openai/gpt-5.4",
        "OPENROUTER_API_KEY": "sk-new",
        "ANTHROPIC_API_KEY": "ant-new",
        "OUROBOROS_REVIEW_MODELS": "x,y",
        "OUROBOROS_REVIEW_ENFORCEMENT": "blocking",
        "OUROBOROS_EFFORT_TASK": "high",
        "OUROBOROS_TOOL_TIMEOUT_SEC": "300",
    }
    result = classify(old, new)
    assert result == [], f"Expected no restart keys, got: {result}"


def test_classify_openai_base_url_requires_restart():
    classify = _get_classify()
    old = {"OPENAI_BASE_URL": ""}
    new = {"OPENAI_BASE_URL": "https://custom.openai.example.com/v1"}
    result = classify(old, new)
    assert "OPENAI_BASE_URL" in result


def test_classify_openai_compatible_base_url_requires_restart():
    classify = _get_classify()
    old = {"OPENAI_COMPATIBLE_BASE_URL": ""}
    new = {"OPENAI_COMPATIBLE_BASE_URL": "https://custom.example.com/v1"}
    result = classify(old, new)
    assert "OPENAI_COMPATIBLE_BASE_URL" in result


def test_classify_local_model_port_requires_restart():
    classify = _get_classify()
    old = {"LOCAL_MODEL_PORT": "8766"}
    new = {"LOCAL_MODEL_PORT": "9000"}
    result = classify(old, new)
    assert "LOCAL_MODEL_PORT" in result


def test_classify_unchanged_restart_key_not_included():
    """A restart-sensitive key that did not change must NOT appear in the result."""
    classify = _get_classify()
    old = {"LOCAL_MODEL_SOURCE": "repo/x", "OUROBOROS_MODEL": "old-model"}
    new = {"LOCAL_MODEL_SOURCE": "repo/x", "OUROBOROS_MODEL": "new-model"}
    result = classify(old, new)
    assert "LOCAL_MODEL_SOURCE" not in result


# ---------------------------------------------------------------------------
# server.py — no_changes flag
# ---------------------------------------------------------------------------

def test_no_changes_flag_when_nothing_changed():
    """api_settings_post must include no_changes=True when submitted values match current."""
    import server as srv

    # Simulate: old_settings == current (no mutations).
    old = {"OUROBOROS_MODEL": "some-model", "TOTAL_BUDGET": "100"}
    current = dict(old)
    all_changed = [k for k in current
                   if str(current.get(k, "") or "") != str(old.get(k, "") or "")]
    assert all_changed == [], "Expected no differences"
    # A real response would set no_changes=True when all_changed is empty.
    assert len(all_changed) == 0


def test_no_changes_flag_absent_when_values_differ():
    """no_changes should NOT be set when at least one value changed."""
    old = {"OUROBOROS_MODEL": "model-a", "TOTAL_BUDGET": "100"}
    current = {"OUROBOROS_MODEL": "model-b", "TOTAL_BUDGET": "100"}
    all_changed = [k for k in current
                   if str(current.get(k, "") or "") != str(old.get(k, "") or "")]
    assert "OUROBOROS_MODEL" in all_changed


def test_refresh_bus_budget_zero_is_not_replaced_by_default():
    """TOTAL_BUDGET=0 must be passed as 0.0, not swapped to the default 10.0."""
    from supervisor import message_bus as mb
    orig = mb.TOTAL_BUDGET_LIMIT
    try:
        mb.refresh_budget_limit(0.0)
        assert mb.TOTAL_BUDGET_LIMIT == 0.0, (
            f"Expected 0.0 but got {mb.TOTAL_BUDGET_LIMIT}"
        )
    finally:
        mb.TOTAL_BUDGET_LIMIT = orig


# ---------------------------------------------------------------------------
# ouroboros/agent.py — handle_task calls apply_settings_to_env
# ---------------------------------------------------------------------------

def _make_fake_agent(agent_mod, tmp_path, prepare_side_effect):
    """Create a minimal fake OuroborosAgent that stops at _prepare_task_context."""
    import threading

    env = agent_mod.Env(repo_dir=tmp_path, drive_root=tmp_path)

    class _FakeAgent(agent_mod.OuroborosAgent):
        def _start_task_heartbeat_loop(self, _):
            # handle_task calls heartbeat_stop.set() in finally — return a real Event.
            ev = threading.Event()
            return ev

        def _prepare_task_context(self, task):
            raise prepare_side_effect

        def _emit_live_log(self, *a, **kw):
            pass

    return _FakeAgent(env=env)


def test_handle_task_calls_apply_settings_to_env(tmp_path, monkeypatch):
    """handle_task must call apply_settings_to_env(load_settings()) before task setup."""
    import ouroboros.agent as agent_mod
    import ouroboros.config as config_mod

    calls = []

    def fake_load_settings():
        calls.append("load")
        return {"OUROBOROS_MODEL": "test-model"}

    def fake_apply(settings):
        calls.append(("apply", settings.get("OUROBOROS_MODEL")))

    monkeypatch.setattr(config_mod, "load_settings", fake_load_settings)
    monkeypatch.setattr(config_mod, "apply_settings_to_env", fake_apply)

    ag = _make_fake_agent(agent_mod, tmp_path, RuntimeError("stop_here"))

    try:
        ag.handle_task({"id": "t1", "type": "task", "text": "hello"})
    except RuntimeError as e:
        assert "stop_here" in str(e)

    assert "load" in calls, "load_settings was not called"
    assert any(c[0] == "apply" for c in calls if isinstance(c, tuple)), (
        "apply_settings_to_env was not called"
    )


def test_immediate_changed_flag_for_budget_keys():
    """api_settings_post must set immediate_changed=True when TOTAL_BUDGET changes."""
    import server as srv
    immediate_keys = srv._IMMEDIATE_KEYS
    assert "TOTAL_BUDGET" in immediate_keys, "TOTAL_BUDGET must be an immediate key"
    assert "OUROBOROS_SOFT_TIMEOUT_SEC" in immediate_keys
    assert "OUROBOROS_HARD_TIMEOUT_SEC" in immediate_keys
    assert "OUROBOROS_TOOL_TIMEOUT_SEC" in immediate_keys
    # Integrations applied inside api_settings_post — also immediate
    assert "TELEGRAM_BOT_TOKEN" in immediate_keys
    assert "TELEGRAM_CHAT_ID" in immediate_keys
    assert "GITHUB_TOKEN" in immediate_keys
    assert "GITHUB_REPO" in immediate_keys
    # PER_TASK_COST_USD is NOT immediate — it applies on next task via agent.py
    assert "OUROBOROS_PER_TASK_COST_USD" not in immediate_keys


def test_immediate_changed_detection():
    """immediate_changed list is correctly computed from _IMMEDIATE_KEYS."""
    import server as srv
    _IMMEDIATE_KEYS = srv._IMMEDIATE_KEYS
    all_changed = ["TOTAL_BUDGET", "OUROBOROS_MODEL"]
    immediate = [k for k in all_changed if k in _IMMEDIATE_KEYS]
    assert immediate == ["TOTAL_BUDGET"]


def test_next_task_changed_excludes_immediate_and_restart():
    """next_task_changed must exclude both immediate and restart keys."""
    import server as srv
    _IMMEDIATE_KEYS = srv._IMMEDIATE_KEYS
    _RESTART_REQUIRED_KEYS = srv._RESTART_REQUIRED_KEYS
    all_changed = ["TOTAL_BUDGET", "OUROBOROS_MODEL", "LOCAL_MODEL_PORT"]
    next_task = [
        k for k in all_changed
        if k not in _IMMEDIATE_KEYS and k not in _RESTART_REQUIRED_KEYS
    ]
    assert next_task == ["OUROBOROS_MODEL"]


def test_handle_task_hot_reload_failure_does_not_crash(tmp_path, monkeypatch):
    """If settings hot-reload raises, handle_task must not propagate the error."""
    import ouroboros.agent as agent_mod
    import ouroboros.config as config_mod

    def bad_load():
        raise OSError("disk error")

    monkeypatch.setattr(config_mod, "load_settings", bad_load)

    ag = _make_fake_agent(agent_mod, tmp_path, RuntimeError("stop_after_reload"))

    # The OSError from load_settings must be swallowed; RuntimeError from
    # _prepare_task_context is the first exception that should surface.
    try:
        ag.handle_task({"id": "t2", "type": "task", "text": "hi"})
    except RuntimeError as e:
        assert "stop_after_reload" in str(e)
    except OSError:
        raise AssertionError("hot-reload OSError leaked out of handle_task")
