"""Opt-in degraded low-context scope review: supplemental window-fitting cap."""

from ouroboros.tools import scope_review as sr


def _set(monkeypatch, mode, degraded):
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", mode)
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_DEGRADED", "true" if degraded else "false")


def test_effective_limit_defaults_to_full(monkeypatch):
    monkeypatch.delenv("OUROBOROS_CONTEXT_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_SCOPE_REVIEW_DEGRADED", raising=False)
    assert sr._effective_scope_input_limit() == sr._SCOPE_INPUT_TOKEN_LIMIT


def test_normal_scope_limit_stays_full_even_when_degraded_is_requested(monkeypatch):
    _set(monkeypatch, "low", True)
    assert sr._effective_scope_input_limit() == sr._SCOPE_INPUT_TOKEN_LIMIT


def test_supplemental_degraded_requires_both_low_mode_and_optin(monkeypatch):
    # low but no opt-in -> full (no silent degradation)
    _set(monkeypatch, "low", False)
    assert sr._effective_scope_input_limit(degraded=True) == sr._SCOPE_INPUT_TOKEN_LIMIT
    # opt-in but max mode -> full (degraded is a low-mode-only fallback)
    _set(monkeypatch, "max", True)
    assert sr._effective_scope_input_limit(degraded=True) == sr._SCOPE_INPUT_TOKEN_LIMIT
    # both + explicit supplemental pass -> degraded window-fitting cap
    _set(monkeypatch, "low", True)
    assert sr._effective_scope_input_limit(degraded=True) == sr._LOW_SCOPE_INPUT_TOKEN_LIMIT


def test_degraded_cap_plus_output_fits_a_small_reviewer_window():
    # 90K input + 100K scope output must fit a ~200K small/local reviewer window,
    # and must be strictly below the capable >=1M-reviewer cap.
    assert sr._LOW_SCOPE_INPUT_TOKEN_LIMIT + sr._SCOPE_MAX_TOKENS <= 200_000
    assert sr._LOW_SCOPE_INPUT_TOKEN_LIMIT < sr._SCOPE_INPUT_TOKEN_LIMIT


def test_explicit_degraded_scope_review_downgrades_critical_findings(monkeypatch, tmp_path):
    from unittest.mock import patch

    from ouroboros.tools.registry import ToolContext

    _set(monkeypatch, "low", True)
    ctx = ToolContext(repo_dir=tmp_path, drive_root=tmp_path)
    raw = '[{"item":"regression_surface","verdict":"FAIL","severity":"critical","reason":"breaks coupling"}]'

    with patch("ouroboros.tools.scope_review._build_scope_prompt", return_value=("prompt", None)), \
         patch("ouroboros.tools.scope_review._call_scope_llm", return_value=(raw, {"prompt_tokens": 10, "completion_tokens": 5}, None)):
        result = sr.run_scope_review(ctx, "test commit", scope_model="test-scope", degraded=True)

    assert result.blocked is False
    assert result.critical_findings == []
    assert [item["item"] for item in result.advisory_findings] == [
        "regression_surface",
        "scope_review_degraded",
    ]
    assert result.advisory_findings[0]["severity"] == "advisory"
