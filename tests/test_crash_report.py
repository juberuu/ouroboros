"""Tests for crash report lifecycle and health invariant integrity.

Verifies:
- crash_report.json is NOT deleted by _verify_system_state (stays for health_invariants)
- build_health_invariants detects crash_report.json
"""
import importlib
import inspect
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def test_verify_system_state_does_not_delete_crash_file():
    """_verify_system_state must NOT call unlink() on the crash report file.

    The crash_report.json must persist so build_health_invariants() surfaces
    it on every task until the agent investigates and removes it.
    """
    from ouroboros.agent import OuroborosAgent
    source = inspect.getsource(OuroborosAgent._verify_system_state)
    assert "unlink" not in source, (
        "_verify_system_state still deletes crash_report.json — "
        "health_invariants won't see it. File must persist until agent clears it."
    )


def test_health_invariants_detects_crash_report():
    """build_health_invariants must check for crash_report.json."""
    from ouroboros.context import build_health_invariants
    source = inspect.getsource(build_health_invariants)
    assert "crash_report.json" in source, (
        "build_health_invariants does not check for crash_report.json"
    )
    assert "CRASH ROLLBACK" in source, (
        "build_health_invariants does not produce CRASH ROLLBACK warning"
    )


def test_crash_event_logged_at_startup():
    """_verify_system_state must log crash_rollback_detected event."""
    from ouroboros.agent import OuroborosAgent
    source = inspect.getsource(OuroborosAgent._verify_system_state)
    assert "crash_rollback_detected" in source, (
        "_verify_system_state does not log crash_rollback_detected event"
    )
