"""F1 (v6.39): model-slot role-model + 429-aware fallback chain + cooldown.

Covers the empty->Main accessors, the new comma-separated fallback chain
(dedup / drop-active / benchmark no-op / legacy-singular env), the stored-key
rename migration, the process-local cooldown, and the subagent lane resolver
(mutating-child -> heavy, read-only -> light, explicit honored, depth-cap note).
"""

from __future__ import annotations

import json
import pathlib

import pytest

import ouroboros.config as config
from ouroboros import fallback_cooldown as fcd
from ouroboros import subagents


# ---------------------------------------------------------------- accessors

def test_heavy_and_light_empty_fall_back_to_main(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main-x")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "")
    assert config.get_heavy_model() == "provider::main-x"
    assert config.get_light_model() == "provider::main-x"


def test_heavy_and_light_explicit_values_are_honored(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main-x")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "provider::strong")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "provider::cheap")
    assert config.get_heavy_model() == "provider::strong"
    assert config.get_light_model() == "provider::cheap"


# ----------------------------------------------------------- fallback chain

def test_fallback_chain_dedups_and_drops_active(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "a, b , a, c")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    assert config.get_fallback_models("b") == ["a", "c"]
    # No active model -> full deduped chain in order.
    assert config.get_fallback_models("") == ["a", "b", "c"]


def test_fallback_chain_benchmark_dedupes_to_no_op(monkeypatch):
    # Benchmark sets every slot to one model; the active model is dropped, so the
    # chain collapses to empty -> no cross-model fallback happens.
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "same::model")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    assert config.get_fallback_models("same::model") == []


def test_fallback_chain_reads_legacy_singular_env(monkeypatch):
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACKS", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "legacy::single")
    assert config.get_fallback_models("primary") == ["legacy::single"]


def test_fallback_chain_empty_means_no_fallback(monkeypatch):
    # An explicitly empty/unset Fallbacks slot must NOT silently fall back to the shipped
    # Anthropic default (which would cross an OpenAI-compatible/local owner into an
    # unconfigured provider). The default reaches a default install via the env instead.
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACKS", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    assert config.get_fallback_models("primary") == []


def test_advisory_fallback_model_uses_main_when_light_empty(monkeypatch):
    from ouroboros.tools.claude_advisory_review import _resolve_fallback_model
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main-x")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "")
    # Empty Light must resolve to Main, never "" (which would call chat with no model id).
    assert _resolve_fallback_model() == "provider::main-x"


def test_parse_fallback_chain_ssot(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "a, b , a")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    # Raw chain: parsed, whitespace-trimmed, NO dedup, NO active-drop (those belong to
    # get_fallback_models on top).
    assert config.parse_fallback_chain() == ["a", "b", "a"]
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACKS", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACK", "legacy")
    assert config.parse_fallback_chain() == ["legacy"]


def test_infer_model_category_recognizes_chain_link(monkeypatch):
    from ouroboros.pricing import infer_model_category
    monkeypatch.setenv("OUROBOROS_MODEL", "main/x")
    monkeypatch.delenv("OUROBOROS_MODEL_HEAVY", raising=False)
    monkeypatch.delenv("OUROBOROS_MODEL_LIGHT", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "fb/one, fb/two")
    monkeypatch.delenv("OUROBOROS_MODEL_FALLBACK", raising=False)
    # A model that is a LINK of the chain is categorized "fallback", not "other".
    assert infer_model_category("fb/two") == "fallback"
    assert infer_model_category("main/x") == "main"
    assert infer_model_category("unrelated/z") == "other"


# -------------------------------------------------------- stored migration

def test_stored_slot_keys_migrate_on_load(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({
        "OUROBOROS_MODEL": "provider::main",
        "OUROBOROS_MODEL_CODE": "provider::legacy-code",
        "USE_LOCAL_CODE": True,
        "OUROBOROS_MODEL_FALLBACK": "provider::legacy-fb",
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", pathlib.Path(settings_file))
    for key in ("OUROBOROS_MODEL_HEAVY", "USE_LOCAL_HEAVY", "OUROBOROS_MODEL_FALLBACKS",
                "OUROBOROS_MODEL_CODE", "USE_LOCAL_CODE", "OUROBOROS_MODEL_FALLBACK"):
        monkeypatch.delenv(key, raising=False)

    loaded = config.load_settings()

    assert loaded.get("OUROBOROS_MODEL_HEAVY") == "provider::legacy-code"
    assert loaded.get("USE_LOCAL_HEAVY") is True
    assert loaded.get("OUROBOROS_MODEL_FALLBACKS") == "provider::legacy-fb"
    # Legacy keys are dropped, not left to linger.
    assert "OUROBOROS_MODEL_CODE" not in loaded
    assert "USE_LOCAL_CODE" not in loaded
    assert "OUROBOROS_MODEL_FALLBACK" not in loaded


def test_migrate_legacy_slot_keys_ssot():
    # The shared SSOT helper preserves a stored value, drops the legacy key, and never
    # clobbers an already-set new key.
    s = {"OUROBOROS_MODEL_CODE": "x", "USE_LOCAL_CODE": True, "OUROBOROS_MODEL_FALLBACK": "y"}
    config.migrate_legacy_slot_keys(s)
    assert s == {"OUROBOROS_MODEL_HEAVY": "x", "USE_LOCAL_HEAVY": True, "OUROBOROS_MODEL_FALLBACKS": "y"}
    # An already-set new key wins; the legacy key is still dropped.
    s2 = {"OUROBOROS_MODEL_CODE": "old", "OUROBOROS_MODEL_HEAVY": "new"}
    config.migrate_legacy_slot_keys(s2)
    assert s2 == {"OUROBOROS_MODEL_HEAVY": "new"}


def test_colab_settings_migrate_legacy_drive_keys():
    # A Colab re-run with legacy Drive settings.json must keep the owner's prior
    # code/heavy + fallback customizations (not silently drop them).
    from ouroboros.colab_bootstrap import build_colab_settings
    existing = {
        "OUROBOROS_MODEL": "openai::gpt-5.5",
        "OUROBOROS_MODEL_CODE": "openai::gpt-5.5-custom-heavy",
        "OUROBOROS_MODEL_FALLBACK": "openai::gpt-5.5-mini",
        "OPENAI_API_KEY": "sk-openai-existing",
    }
    out = build_colab_settings({}, models=None, existing=existing)
    assert out.get("OUROBOROS_MODEL_HEAVY") == "openai::gpt-5.5-custom-heavy"
    assert out.get("OUROBOROS_MODEL_FALLBACKS") == "openai::gpt-5.5-mini"
    assert "OUROBOROS_MODEL_CODE" not in out
    assert "OUROBOROS_MODEL_FALLBACK" not in out


# ---------------------------------------------------------------- cooldown

def test_cooldown_marks_and_heals(monkeypatch):
    fcd.reset_for_tests()
    monkeypatch.delenv("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", raising=False)
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_SEC", "120")
    assert fcd.is_cooling_down("m1") is False
    fcd.mark_cooldown("m1")
    assert fcd.is_cooling_down("m1") is True
    # A zero-length window heals immediately on the next read (passive heal).
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_SEC", "0")
    fcd.mark_cooldown("m2")
    assert fcd.is_cooling_down("m2") is False


def test_cooldown_disabled_is_noop(monkeypatch):
    fcd.reset_for_tests()
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", "false")
    fcd.mark_cooldown("m1")
    assert fcd.is_cooling_down("m1") is False


def test_cooldown_local_and_remote_are_distinct(monkeypatch):
    fcd.reset_for_tests()
    monkeypatch.delenv("OUROBOROS_FALLBACK_COOLDOWN_ENABLED", raising=False)
    monkeypatch.setenv("OUROBOROS_FALLBACK_COOLDOWN_SEC", "120")
    fcd.mark_cooldown("m1", use_local=True)
    assert fcd.is_cooling_down("m1", use_local=True) is True
    assert fcd.is_cooling_down("m1", use_local=False) is False


def test_attempts_per_model_is_bounded(monkeypatch):
    monkeypatch.setenv("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "9")
    assert fcd.attempts_per_model() == 2
    monkeypatch.setenv("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "0")
    assert fcd.attempts_per_model() == 1
    monkeypatch.setenv("OUROBOROS_FALLBACK_ATTEMPTS_PER_MODEL", "nonsense")
    assert fcd.attempts_per_model() == 1


# ------------------------------------------------------------ lane resolver

def test_auto_mutating_child_routes_to_heavy(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "provider::strong")
    res = subagents.resolve_subagent_lane("auto", depth=1, mutating=True)
    assert res.effective_lane == "heavy"
    assert res.model == "provider::strong"
    assert res.downgrade_note == ""


def test_auto_readonly_child_routes_to_light(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "provider::cheap")
    res = subagents.resolve_subagent_lane("auto", depth=1, mutating=False)
    assert res.effective_lane == "light"
    assert res.model == "provider::cheap"


def test_explicit_main_honored_within_depth_cap(monkeypatch):
    monkeypatch.delenv("OUROBOROS_SUBAGENT_CAPABILITY_DEPTH_LIMIT", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    res = subagents.resolve_subagent_lane("main", depth=1, mutating=False)
    assert res.effective_lane == "main"
    assert res.downgrade_note == ""


def test_explicit_heavy_beyond_depth_cap_downgrades_with_note(monkeypatch):
    monkeypatch.delenv("OUROBOROS_SUBAGENT_CAPABILITY_DEPTH_LIMIT", raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "provider::cheap")
    res = subagents.resolve_subagent_lane("heavy", depth=2, mutating=True)
    assert res.effective_lane == "light"
    assert res.model == "provider::cheap"
    assert "depth 2" in res.downgrade_note and "light" in res.downgrade_note


def test_depth_cap_is_configurable(monkeypatch):
    monkeypatch.setenv("OUROBOROS_SUBAGENT_CAPABILITY_DEPTH_LIMIT", "2")
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "provider::strong")
    # depth 2 is now within the cap -> explicit heavy honored, no note.
    res = subagents.resolve_subagent_lane("heavy", depth=2, mutating=False)
    assert res.effective_lane == "heavy"
    assert res.downgrade_note == ""


def test_code_lane_is_rejected_no_legacy_alias():
    with pytest.raises(ValueError):
        subagents.normalize_subagent_model_lane("code")


def test_build_envelope_tolerates_legacy_stored_lane():
    # The PUBLIC schema rejects "code", but an envelope built from an already-ran task's
    # durable record (which may carry a pre-v6.39 "code" lane) must NOT crash — it coerces
    # the unknown stored lane to a safe default (not a "code"->"heavy" alias).
    env = subagents.build_subagent_envelope(
        task_id="t1", parent_task_id="p1", root_task_id="r1", task_group_id="",
        depth=1, role="builder", requested_lane="code", effective_lane="code",
        model="m", status="completed", usage={},
    )
    assert env["requested_lane"] == "auto"
    assert env["effective_lane"] == "light"


def test_string_false_may_mutate_does_not_route_auto_to_heavy(monkeypatch):
    # A tool-call payload may carry may_mutate as the STRING "false"; the SSOT
    # normalize_bool must treat it as falsey, so an `auto` child is NOT promoted to the
    # Heavy lane on a denied mutation intent (regression: bool("false") was truthy).
    from ouroboros.contracts.task_contract import normalize_bool
    assert normalize_bool("false") is False
    assert normalize_bool("true") is True
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "provider::cheap")
    res = subagents.resolve_subagent_lane("auto", depth=1, mutating=normalize_bool("false"))
    assert res.effective_lane == "light"


def test_use_local_empty_heavy_follows_main_flag(monkeypatch):
    monkeypatch.setenv("OUROBOROS_MODEL", "provider::main")
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "")
    monkeypatch.setenv("USE_LOCAL_MAIN", "true")
    monkeypatch.delenv("USE_LOCAL_HEAVY", raising=False)
    res = subagents.resolve_subagent_lane("heavy", depth=1, mutating=True)
    assert res.effective_lane == "heavy"
    assert res.model == "provider::main"
    # Empty Heavy -> Main, so the Main local flag governs (not silently ignored).
    assert res.use_local_model is True


# ------------------------------------------------- cooldown trigger SSOT (C1)

def test_cooldown_error_kinds_include_rate_limit_but_not_in_retry_kinds():
    from ouroboros.loop_llm_call import _COOLDOWN_ERROR_KINDS, _TRANSIENT_RETRY_KINDS
    # A body-error 429 is classified "rate_limit" -> it MUST trigger cooldown.
    assert "rate_limit" in _COOLDOWN_ERROR_KINDS
    assert _TRANSIENT_RETRY_KINDS <= _COOLDOWN_ERROR_KINDS
    # ...but the same-model transient-retry budget must NOT be widened by it.
    assert "rate_limit" not in _TRANSIENT_RETRY_KINDS


# ------------------------------------ credentialed-model resolver parses chain (C2)

def test_resolve_credentialed_model_parses_fallbacks_chain(monkeypatch):
    from ouroboros.provider_models import resolve_credentialed_model
    # Only OpenRouter is credentialed in this environment.
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-key")
    for k in ("GIGACHAT_CREDENTIALS", "GIGACHAT_USER", "GIGACHAT_PASSWORD",
              "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CLOUDRU_FOUNDATION_MODELS_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OUROBOROS_MODEL", "gigachat::GigaChat")  # uncredentialed
    monkeypatch.setenv("OUROBOROS_MODEL_HEAVY", "")
    monkeypatch.setenv("OUROBOROS_MODEL_LIGHT", "")
    # First chain entry uncredentialed (gigachat), second routes via OpenRouter.
    monkeypatch.setenv("OUROBOROS_MODEL_FALLBACKS", "gigachat::nocreds, anthropic/claude-sonnet-4.6")
    # The resolver must parse the chain and return the credentialed SECOND entry — not
    # test the raw comma-string as one (broken) model id, nor skip past it.
    assert resolve_credentialed_model("gigachat::GigaChat") == "anthropic/claude-sonnet-4.6"
