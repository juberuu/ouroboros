"""Regression tests for the host-service rate limiter (#27/#30).

The `_RateLimiter._hits` defaultdict previously grew unbounded: keys (one per
`{skill}:{endpoint}`) were never deleted, only their stale timestamps popped. A
periodic sweep now frees keys that have gone idle past the window, without
changing any rate-limit decision.
"""

from __future__ import annotations

import time

from ouroboros.gateway.host_service import _RateLimiter


def test_sweep_frees_idle_keys():
    rl = _RateLimiter(limit=10, window_sec=0.01)
    for i in range(5):
        assert rl.allow(f"k{i}") is True
    assert len(rl._hits) == 5

    # Sweep with a timestamp well past the window → every key is now idle/empty.
    rl._sweep(time.monotonic() + 1.0)
    assert len(rl._hits) == 0


def test_allow_triggers_amortized_sweep():
    rl = _RateLimiter(limit=10, window_sec=0.01)
    for i in range(5):
        rl.allow(f"k{i}")
    assert len(rl._hits) == 5

    # Make the existing keys stale and mark a sweep overdue, then a single allow()
    # must reclaim the idle keys (only the new key remains).
    rl._last_sweep = time.monotonic() - 100
    time.sleep(0.02)  # > window_sec, so k0..k4 timestamps are stale
    assert rl.allow("fresh") is True
    assert set(rl._hits.keys()) == {"fresh"}


def test_rate_limit_decisions_unchanged():
    rl = _RateLimiter(limit=3, window_sec=60.0)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False  # 4th hit within the window is blocked
    # A different key has its own independent budget.
    assert rl.allow("other") is True


def test_swept_key_is_recreated_cleanly():
    rl = _RateLimiter(limit=2, window_sec=0.01)
    rl.allow("k")
    rl._sweep(time.monotonic() + 1.0)
    assert "k" not in rl._hits
    # Re-using a swept key works (defaultdict recreates it); budget is fresh.
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False
