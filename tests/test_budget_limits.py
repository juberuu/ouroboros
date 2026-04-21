"""Tests for _check_budget_limits (per-task soft reminder + global budget guard)."""
import os
import queue
from unittest.mock import MagicMock, patch
import pytest

from ouroboros.loop import _check_budget_limits


def _make_args(**overrides):
    """Build default kwargs for _check_budget_limits."""
    defaults = dict(
        budget_remaining_usd=100.0,
        accumulated_usage={"cost": 0.0, "prompt_tokens": 0, "completion_tokens": 0},
        round_idx=0,
        messages=[],
        llm=MagicMock(),
        active_model="test-model",
        active_effort="high",
        max_retries=1,
        drive_logs=None,
        task_id="test-task",
        event_queue=queue.Queue(),
        llm_trace={},
        task_type="task",
        use_local=False,
    )
    defaults.update(overrides)
    return defaults


# --- Per-task soft reminder ---

class TestPerTaskSoftReminder:
    """Per-task cost soft reminder (OUROBOROS_PER_TASK_COST_USD).
    
    No hard stop — agent decides whether to continue.
    """

    def test_under_limit_returns_none(self, tmp_path):
        """Below threshold → no intervention."""
        args = _make_args(accumulated_usage={"cost": 3.0}, drive_logs=tmp_path)
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "5.0"}):
            result = _check_budget_limits(**args)
        assert result is None

    def test_at_limit_no_hard_stop(self, tmp_path):
        """At or above threshold → NO hard stop, just soft reminder."""
        messages = []
        args = _make_args(
            accumulated_usage={"cost": 5.5},
            round_idx=10,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "5.0"}):
            result = _check_budget_limits(**args)
        # No hard stop
        assert result is None

    def test_soft_reminder_injected_every_10_rounds(self, tmp_path):
        """At threshold + round divisible by 10 → soft note injected."""
        messages = []
        args = _make_args(
            accumulated_usage={"cost": 6.0},
            round_idx=10,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "5.0"}):
            result = _check_budget_limits(**args)
        assert result is None
        assert any("[COST NOTE]" in m.get("content", "") for m in messages)

    def test_no_reminder_on_non_10_round(self, tmp_path):
        """At threshold but round not divisible by 10 → no message."""
        messages = []
        args = _make_args(
            accumulated_usage={"cost": 6.0},
            round_idx=7,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "5.0"}):
            result = _check_budget_limits(**args)
        assert result is None
        assert not any("[COST NOTE]" in m.get("content", "") for m in messages)

    def test_custom_env_limit(self, tmp_path):
        """Respect custom per-task threshold from env."""
        args = _make_args(accumulated_usage={"cost": 9.0}, drive_logs=tmp_path)
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        # 9.0 < 10.0 → no message at all
        assert result is None

    def test_default_limit_20_no_hard_stop(self, tmp_path):
        """Without env var, default threshold is 20.0 — but still no hard stop."""
        messages = []
        args = _make_args(
            accumulated_usage={"cost": 20.0},
            round_idx=10,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OUROBOROS_PER_TASK_COST_USD", None)
            result = _check_budget_limits(**args)
        assert result is None  # soft reminder only, no hard stop


# --- Global budget guard ---

class TestGlobalBudgetGuard:
    """Existing global budget percentage checks."""

    def test_none_budget_returns_none(self, tmp_path):
        """No budget → no checks."""
        args = _make_args(budget_remaining_usd=None, accumulated_usage={"cost": 100.0}, drive_logs=tmp_path)
        result = _check_budget_limits(**args)
        assert result is None

    def test_budget_exhausted(self, tmp_path):
        """Remaining ≤ 0 → immediate stop."""
        args = _make_args(budget_remaining_usd=0.0, accumulated_usage={"cost": 0.01}, drive_logs=tmp_path)
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "999"}):
            result = _check_budget_limits(**args)
        assert result is not None
        text, _, _ = result
        assert "budget exhausted" in text.lower()

    def test_under_50pct_passes(self, tmp_path):
        """Task cost < 50% of remaining → no stop."""
        args = _make_args(
            budget_remaining_usd=10.0,
            accumulated_usage={"cost": 4.9},  # 49% < 50%
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        assert result is None

    def test_over_50pct_triggers(self, tmp_path):
        """Task cost > 50% of remaining budget → stops."""
        llm = MagicMock()
        llm.chat.return_value = ({"content": "done"}, {"prompt_tokens": 10, "completion_tokens": 5})
        args = _make_args(
            budget_remaining_usd=8.0,
            accumulated_usage={"cost": 4.5},  # 4.5/8 = 56% > 50%
            llm=llm,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        assert result is not None

    def test_soft_nudge_at_30pct(self, tmp_path):
        """Task cost > 30% + round % 10 == 0 → soft nudge."""
        messages = []
        args = _make_args(
            budget_remaining_usd=10.0,
            accumulated_usage={"cost": 3.5},  # 35% > 30%
            round_idx=20,
            messages=messages,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            result = _check_budget_limits(**args)
        assert result is None
        assert any("[INFO]" in m.get("content", "") for m in messages)


# --- use_local propagation ---

class TestUseLocalPropagation:
    """Ensure use_local is passed to _call_llm_with_retry on global budget stop."""

    @patch("ouroboros.loop._call_llm_with_retry")
    def test_global_stop_passes_use_local(self, mock_retry, tmp_path):
        mock_retry.return_value = ({"content": "done"}, {"prompt_tokens": 10, "completion_tokens": 5})
        args = _make_args(
            budget_remaining_usd=6.0,
            accumulated_usage={"cost": 4.0},  # 67% > 50%
            use_local=True,
            drive_logs=tmp_path,
        )
        with patch.dict(os.environ, {"OUROBOROS_PER_TASK_COST_USD": "10.0"}):
            _check_budget_limits(**args)
        mock_retry.assert_called_once()
        _, kwargs = mock_retry.call_args
        assert kwargs.get("use_local") is True
