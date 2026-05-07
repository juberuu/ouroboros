"""Tests for crash report lifecycle and health invariant integrity.

Verifies:
- crash_report.json is NOT deleted during startup verification
- build_health_invariants detects crash_report.json
"""
import importlib
import inspect
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def test_verify_system_state_does_not_delete_crash_file():
    """Startup verification must NOT call unlink() on the crash report file.

    The crash_report.json must persist so build_health_invariants() surfaces
    it on every task until the agent investigates and removes it.
    """
    from ouroboros import agent_startup_checks
    source = inspect.getsource(agent_startup_checks.verify_system_state)
    source += inspect.getsource(agent_startup_checks.inject_crash_report)
    assert "unlink" not in source, (
        "startup verification still deletes crash_report.json — "
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
    """Startup crash-report injection must log crash_rollback_detected event."""
    from ouroboros.agent_startup_checks import inject_crash_report
    source = inspect.getsource(inject_crash_report)
    assert "crash_rollback_detected" in source, (
        "startup crash-report injection does not log crash_rollback_detected event"
    )
