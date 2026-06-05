from types import SimpleNamespace
from pathlib import Path

from scripts.run_external_review import _resolved_review_config, _scope_review_skipped


def test_external_review_script_marks_budget_exceeded_scope_as_skipped():
    source = Path("scripts/run_external_review.py").read_text(encoding="utf-8")
    assert "v6.10.0" not in source
    assert "Google Colab" not in source
    assert _scope_review_skipped(SimpleNamespace(status="budget_exceeded"), []) is True
    assert _scope_review_skipped(
        SimpleNamespace(status="responded"),
        [{"item": "scope_review_skipped", "severity": "advisory"}],
    ) is True
    assert _scope_review_skipped(SimpleNamespace(status="responded"), []) is False


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
