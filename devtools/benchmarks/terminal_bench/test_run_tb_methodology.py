"""Focused tests for the leaderboard-faithfulness invariants of run_tb.py.

Run explicitly (it lives under devtools, not tests/, to stay merge-clean):
    PYTHONPATH=<repo> python -m pytest devtools/benchmarks/terminal_bench/test_run_tb_methodology.py
"""
from __future__ import annotations

import json
import pathlib

import pytest

from devtools.benchmarks.terminal_bench import run_tb


# --- validate_methodology gates -------------------------------------------------

def test_k_below_5_raises_without_allow():
    with pytest.raises(ValueError):
        run_tb.validate_methodology(k=1, timeout_multiplier=1.0, resource_overrides=[])


def test_k_below_5_allowed_with_flag():
    run_tb.validate_methodology(k=1, timeout_multiplier=1.0, resource_overrides=[], allow_low_k=True)


def test_setup_build_multiplier_raises_without_allow():
    with pytest.raises(ValueError):
        run_tb.validate_methodology(k=5, timeout_multiplier=1.0, resource_overrides=[], setup_timeout_multiplier=4.0)
    with pytest.raises(ValueError):
        run_tb.validate_methodology(k=5, timeout_multiplier=1.0, resource_overrides=[], build_timeout_multiplier=4.0)


def test_setup_build_multiplier_allowed_with_flag():
    run_tb.validate_methodology(
        k=5, timeout_multiplier=1.0, resource_overrides=[],
        setup_timeout_multiplier=4.0, build_timeout_multiplier=4.0, allow_setup_build_multipliers=True,
    )


# --- harbor_command output ------------------------------------------------------

def _cfg(**over):
    base = dict(
        dataset="terminal-bench/terminal-bench-2-1", model="google/gemini-3.5-flash", k=5,
        jobs_dir=pathlib.Path("/tmp/jd"), harbor_bin="harbor", n_concurrent=1, task_filters=[],
        settings_path=pathlib.Path("/tmp/s.json"), execute=False, light_model="google/gemini-3.5-flash",
    )
    base.update(over)
    return run_tb.HarborCommandConfig(**base)


def test_faithful_command_omits_multiplier_flags_and_gates_web():
    cmd = run_tb.harbor_command(_cfg())
    assert "--agent-setup-timeout-multiplier" not in cmd
    assert "--environment-build-timeout-multiplier" not in cmd
    assert "disable_agent_web=true" in cmd


def test_local_override_emits_multiplier_flags():
    cmd = run_tb.harbor_command(_cfg(setup_timeout_multiplier=4.0, build_timeout_multiplier=2.0))
    assert "--agent-setup-timeout-multiplier" in cmd and "4.0" in cmd
    assert "--environment-build-timeout-multiplier" in cmd and "2.0" in cmd


def test_allow_agent_web_flips_kwarg():
    cmd = run_tb.harbor_command(_cfg(disable_agent_web=False))
    assert "disable_agent_web=false" in cmd


# --- apply_all_model + metadata -------------------------------------------------

def test_apply_all_model_sets_forwarded_slots(monkeypatch):
    for key in run_tb._ALL_MODEL_SLOT_KEYS + ("OUROBOROS_REVIEW_MODELS",):
        monkeypatch.delenv(key, raising=False)
    run_tb.apply_all_model("google/gemini-3.5-flash")
    import os
    for key in run_tb._ALL_MODEL_SLOT_KEYS:
        assert os.environ[key] == "google/gemini-3.5-flash"
    assert os.environ["OUROBOROS_REVIEW_MODELS"] == "google/gemini-3.5-flash,google/gemini-3.5-flash,google/gemini-3.5-flash"
    assert "CLAUDE_CODE_MODEL" in run_tb._ALL_MODEL_SLOT_KEYS  # claude_code_edit cannot leak a different model


def test_metadata_omits_web_search_when_web_disabled(monkeypatch):
    monkeypatch.setenv("OUROBOROS_WEBSEARCH_MODEL", "openai/gpt-5.2")
    roles_on = dict(run_tb._effective_helper_models("google/gemini-3.5-flash", "google/gemini-3.5-flash", disable_agent_web=False))
    roles_off = dict(run_tb._effective_helper_models("google/gemini-3.5-flash", "google/gemini-3.5-flash", disable_agent_web=True))
    assert any("web_search" in r for r in roles_on.values())
    assert not any("web_search" in r for r in roles_off.values())


def test_metadata_declares_claude_code_and_dedupes_in_single_model(monkeypatch):
    monkeypatch.delenv("OUROBOROS_WEBSEARCH_MODEL", raising=False)
    # ensemble: a different Claude Code model is declared honestly
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "anthropic/claude-opus-4.8")
    roles = dict(run_tb._effective_helper_models("google/gemini-3.5-flash", "google/gemini-3.5-flash", disable_agent_web=True))
    assert any("claude_code_edit" in r for r in roles.values())
    # single-model: CLAUDE_CODE_MODEL == measured -> everything dedupes to the one model
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "google/gemini-3.5-flash")
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_MODELS", "google/gemini-3.5-flash")
    monkeypatch.setenv("OUROBOROS_REVIEW_MODELS", "google/gemini-3.5-flash,google/gemini-3.5-flash,google/gemini-3.5-flash")
    roles2 = dict(run_tb._effective_helper_models("google/gemini-3.5-flash", "google/gemini-3.5-flash", disable_agent_web=True))
    assert list(roles2.keys()) == ["google/gemini-3.5-flash"]


# --- disclosure ledger ----------------------------------------------------------

def _write_trial(d: pathlib.Path, task: str, reward, exc=None):
    d.mkdir(parents=True, exist_ok=True)
    (d / "result.json").write_text(json.dumps({
        "task_name": task, "trial_name": d.name,
        "verifier_result": {"rewards": {"reward": reward}},
        "exception_info": ({"exception_type": exc} if exc else None),
        "agent_result": {"cost_usd": 0.01, "metadata": {"turns": 3}},
    }), encoding="utf-8")


def test_disclosure_ledger_counts(tmp_path):
    jobs = tmp_path / "job"
    _write_trial(jobs / "t1", "alpha", 1.0)
    _write_trial(jobs / "t2", "alpha", 0.0, exc="AgentTimeoutError")
    _write_trial(jobs / "t3", "beta", None, exc="RuntimeError")  # provider/infra, not a timeout
    led = run_tb.write_disclosure_ledger(jobs_dir=jobs, out_path=tmp_path / "led.json", run_meta={})
    assert led["n_trials"] == 3
    assert led["agent_timeout_count"] == 1
    assert led["provider_or_infra_failure_count"] == 1  # RuntimeError counts, AgentTimeoutError does not
    assert led["reward_distribution"].get("1.0") == 1  # normalized bucket (not split '1' vs '1.0')
    assert led["per_task_pass_rate"]["alpha"] == 0.5
