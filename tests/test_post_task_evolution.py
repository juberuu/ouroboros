"""Phase 1 (cross-task self-evolution): budget refusal guard, V4 config envelope,
and V5 promotion (durable-signal -> gated supervisor apply)."""
from __future__ import annotations

import json
import pathlib
import types

import pytest

import ouroboros.config as config
import ouroboros.post_task_evolution as pte
import supervisor.state as state


# --- Budget refusal guard (red-team R2.1 / BIBLE P8) --------------------------

def _seed_state(d: pathlib.Path, spent: float = 7.5) -> None:
    (d / "state").mkdir(parents=True, exist_ok=True)
    (d / "state" / "state.json").write_text(
        json.dumps({"spent_usd": spent, "keep": "me"}), encoding="utf-8")


def test_budget_reset_refuses_live_dir(monkeypatch):
    live = (pathlib.Path.home() / "Ouroboros" / "data")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(live))
    assert state.reset_per_task_budget(live, confirm_isolated=True) is False


def test_budget_reset_refuses_without_confirm(tmp_path, monkeypatch):
    _seed_state(tmp_path)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    assert state.reset_per_task_budget(tmp_path, confirm_isolated=False) is False


def test_budget_reset_refuses_without_env(tmp_path, monkeypatch):
    _seed_state(tmp_path)
    monkeypatch.delenv("OUROBOROS_DATA_DIR", raising=False)
    assert state.reset_per_task_budget(tmp_path, confirm_isolated=True) is False


def test_budget_reset_allows_isolated(tmp_path, monkeypatch):
    _seed_state(tmp_path, spent=9.9)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    assert state.reset_per_task_budget(tmp_path, confirm_isolated=True) is True
    after = json.loads((tmp_path / "state" / "state.json").read_text())
    assert after["spent_usd"] == 0.0
    assert after["keep"] == "me"  # non-budget state preserved


# --- V4 config envelope -------------------------------------------------------

def test_envelope_defaults_off(monkeypatch):
    monkeypatch.delenv("OUROBOROS_POST_TASK_EVOLUTION", raising=False)
    monkeypatch.delenv("OUROBOROS_POST_TASK_EVOLUTION_CADENCE", raising=False)
    assert config.get_post_task_evolution_enabled() is False
    assert config.get_post_task_evolution_cadence() == "llm"
    assert config.get_post_task_evolution_budget_usd() == 0.0


def test_envelope_enable_parsing(monkeypatch):
    for v in ("true", "1", "yes", "on"):
        monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", v)
        assert config.get_post_task_evolution_enabled() is True
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "false")
    assert config.get_post_task_evolution_enabled() is False


# --- V5 guards ----------------------------------------------------------------

def test_v5_eligibility_and_canonical():
    assert pte._eligible({"type": "task"}) is True
    assert pte._eligible({"type": "evolution"}) is False
    assert pte._eligible({"type": "deep_self_review"}) is False
    assert pte._eligible({"type": "task", "delegation_role": "subagent"}) is False
    env = types.SimpleNamespace(drive_root=pathlib.Path("/x/data"))
    assert pte._is_canonical_run(env, {}) is True
    assert pte._is_canonical_run(env, {"budget_drive_root": "/y/data"}) is False
    assert pte._is_canonical_run(env, {"budget_drive_root": "/x/data"}) is True


def test_v5_every_n_counter(tmp_path):
    # k=2 -> due on the 2nd, 4th call
    assert pte._counter_due(tmp_path, 2) is False
    assert pte._counter_due(tmp_path, 2) is True
    assert pte._counter_due(tmp_path, 2) is False
    assert pte._counter_due(tmp_path, 2) is True


def test_v5_maybe_promote_off_returns_none(tmp_path, monkeypatch):
    monkeypatch.delenv("OUROBOROS_POST_TASK_EVOLUTION", raising=False)
    env = types.SimpleNamespace(drive_root=tmp_path)
    assert pte.maybe_promote(env, {"type": "task", "id": "t1"}, {"reflection": "x"}) is None


def test_v5_maybe_promote_light_mode_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "true")
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    env = types.SimpleNamespace(drive_root=tmp_path)
    assert pte.maybe_promote(env, {"type": "task", "id": "t1"}, {"reflection": "x"}) is None


def test_v5_apply_pending_none_when_no_request(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "true")
    assert pte.apply_pending_request(tmp_path) is False


def test_v5_apply_pending_request_activates_gated_campaign(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "true")
    # request file with requires_plan_review -> objective must carry the obligation
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "post_task_evolution_request.json").write_text(json.dumps({
        "objective": "Refactor X for clarity", "requires_plan_review": True,
        "backlog_id": "abc", "source": "post_task",
    }), encoding="utf-8")

    started = {}
    saved = {}

    import supervisor.queue as q
    import supervisor.state as st
    monkeypatch.setattr(q, "evolution_block_reason", lambda: "")
    monkeypatch.setattr(q, "start_evolution_campaign", lambda objective, source="": started.update(objective=objective, source=source))
    monkeypatch.setattr(st, "load_state", lambda: {"owner_chat_id": 7})
    monkeypatch.setattr(st, "save_state", lambda s: saved.update(s))

    assert pte.apply_pending_request(tmp_path) is True
    assert "plan_task" in started["objective"]  # requires_plan_review carried in
    assert started["source"] == "post_task"
    assert saved["evolution_mode_enabled"] is True
    assert saved["post_task_autostop"] is True
    # one-shot: request consumed
    assert not (tmp_path / "state" / "post_task_evolution_request.json").exists()


def test_budget_reset_refuses_target_mismatch(tmp_path, monkeypatch):
    _seed_state(tmp_path)
    other = tmp_path.parent / (tmp_path.name + "_other")
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(other))  # env data dir != reset target
    assert state.reset_per_task_budget(tmp_path, confirm_isolated=True) is False


def test_v5_apply_pending_refused_when_budget_floor_unmet(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "true")
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION_BUDGET_USD", "5.0")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "post_task_evolution_request.json").write_text(
        json.dumps({"objective": "x"}), encoding="utf-8")
    import supervisor.queue as q
    import supervisor.state as st
    monkeypatch.setattr(q, "evolution_block_reason", lambda: "")
    monkeypatch.setattr(st, "load_state", lambda: {"owner_chat_id": 7, "spent_usd": 9.0})
    monkeypatch.setattr(st, "budget_remaining", lambda s: 1.0)  # below the 5.0 floor
    assert pte.apply_pending_request(tmp_path) is False


def test_envelope_enable_is_owner_gated_in_settings_merge():
    """The agent-reachable generic /api/settings merge must NOT be able to enable
    the post-task self-evolution privilege (owner-only)."""
    from ouroboros.gateway.settings import _merge_settings_payload

    merged = _merge_settings_payload(
        {"OUROBOROS_POST_TASK_EVOLUTION": "false"},
        {"OUROBOROS_POST_TASK_EVOLUTION": "true"},
    )
    assert merged["OUROBOROS_POST_TASK_EVOLUTION"] == "false"


def test_apply_pending_keeps_unparseable_request(tmp_path, monkeypatch):
    """A partial/corrupt request must not be dropped (avoids the write/read race)."""
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "true")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    p = tmp_path / "state" / "post_task_evolution_request.json"
    p.write_text("{partial-json", encoding="utf-8")
    assert pte.apply_pending_request(tmp_path) is False
    assert p.exists()  # retained for the next tick, not unlinked


def test_v5_apply_pending_blocked_in_light_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_POST_TASK_EVOLUTION", "true")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state" / "post_task_evolution_request.json").write_text(
        json.dumps({"objective": "x"}), encoding="utf-8")
    import supervisor.queue as q
    monkeypatch.setattr(q, "evolution_block_reason", lambda: "light mode")
    assert pte.apply_pending_request(tmp_path) is False
    # stale request dropped
    assert not (tmp_path / "state" / "post_task_evolution_request.json").exists()
