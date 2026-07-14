from pathlib import Path
from scripts.run_external_review import (
    _resolved_review_config,
    _review_evidence_and_cost,
)


def test_external_review_script_delegates_verdict_to_production_gate():
    source = Path("scripts/run_external_review.py").read_text(encoding="utf-8")
    assert "v6.10.0" not in source
    assert "Google Colab" not in source
    assert "_run_non_committing_review_cycle" in source
    assert "adaptive_quorum" not in source
    assert "aggregate_review_verdict" not in source


def test_external_review_script_resolves_models_and_efforts(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
        "GIGACHAT_USER",
        "GIGACHAT_PASSWORD",
        "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OUROBOROS_MODEL",
        "OUROBOROS_MODEL_LIGHT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OUROBOROS_REVIEW_MODELS", "anthropic/claude-opus-4.8,google/gemini-3.5-flash,openai/gpt-5.5")
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_MODELS", "openai/gpt-5.5")
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "high")
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "high")

    config = _resolved_review_config()

    assert config["triad_models"] == [
        "anthropic/claude-opus-4.8",
        "google/gemini-3.5-flash",
        "openai/gpt-5.5",
    ]
    assert config["triad_effort"] == "high"
    assert config["scope_models"] == ["openai/gpt-5.5"]
    assert config["scope_effort"] == "high"


def _complete_ctx():
    triad = [
        {
            "slot_id": f"slot_{idx}",
            "model_id": f"reviewer-{idx}",
            "status": "responded",
            "tokens_in": 100,
            "cost_usd": 0.01,
            "prompt_ref": {"manifest_ref": f"prompt-{idx}"},
            "response_ref": {"manifest_ref": f"response-{idx}"},
        }
        for idx in range(1, 4)
    ]
    scope_actor = {
        "slot_id": "scope_slot_1",
        "model_id": "scope-reviewer",
        "status": "responded",
        "tokens_in": 200,
        "cost_usd": 0.0,
        "prompt_ref": {"manifest_ref": "scope-prompt"},
        "response_ref": {"manifest_ref": "scope-response"},
    }
    from types import SimpleNamespace

    return SimpleNamespace(
        _last_triad_raw_results=triad,
        _last_scope_raw_result={"raw_results": [scope_actor]},
    )


def test_external_review_cost_report_never_turns_unknown_into_zero():
    evidence, report = _review_evidence_and_cost(_complete_ctx())

    assert len(evidence) == 4
    assert report["reported_actor_cost_usd"] == 0.03
    assert report["unreported_or_unknown_cost_slots"] == ["scope_slot_1"]
    assert "not treated as $0" in report["note"]
