"""Regression tests for PR-A: event/state robustness + temp cleanup.

  * #10/#13 — supervisor.state.update_state exists (toggle-consciousness import no
    longer crashes) and is an ATOMIC read-modify-write under a single lock.
  * #12     — dispatch_event surfaces handler exceptions / unknown events at
    WARNING (with traceback) instead of silently burying them.
  * #32     — sweep_stale_temp_files reaps orphaned atomic-write temp files.
"""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace


# ───────────────────────── #10 / #13: atomic update_state ────────────────────

def test_update_state_is_exported_and_atomic(tmp_path):
    # The exact import that crashed toggling background consciousness (#10).
    from supervisor.state import update_state  # noqa: F401
    from supervisor import state

    state.init(tmp_path)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    (tmp_path / "locks").mkdir(parents=True, exist_ok=True)

    out = state.update_state(lambda st: st.__setitem__("bg_consciousness_enabled", True))
    assert out.get("bg_consciousness_enabled") is True
    assert state.load_state().get("bg_consciousness_enabled") is True

    # A second RMW preserves the first write (load+mutate+save is one critical
    # section, so updates can't clobber each other).
    state.update_state(lambda st: st.__setitem__("marker", 42))
    reloaded = state.load_state()
    assert reloaded.get("bg_consciousness_enabled") is True
    assert reloaded.get("marker") == 42


# ───────────────────────── #12: handler-failure visibility ───────────────────

def test_dispatch_event_logs_handler_exception(tmp_path, caplog):
    from supervisor import events

    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

    def _boom(evt, ctx):
        raise RuntimeError("kaboom")

    events.EVENT_HANDLERS["__test_boom__"] = _boom
    try:
        ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, append_jsonl=lambda p, o: None)
        with caplog.at_level(logging.WARNING, logger="supervisor.events"):
            events.dispatch_event({"type": "__test_boom__"}, ctx)
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "__test_boom__" in msgs and "kaboom" in msgs
    finally:
        events.EVENT_HANDLERS.pop("__test_boom__", None)


def test_dispatch_event_logs_unknown_event(tmp_path, caplog):
    from supervisor import events

    ctx = SimpleNamespace(DRIVE_ROOT=tmp_path, append_jsonl=lambda p, o: None)
    with caplog.at_level(logging.WARNING, logger="supervisor.events"):
        events.dispatch_event({"type": "__never_registered_handler__"}, ctx)
    assert any("__never_registered_handler__" in r.getMessage() for r in caplog.records)


# ───────────────────────── #32: stale temp sweep ─────────────────────────────

def test_sweep_stale_temp_files(tmp_path):
    from ouroboros.utils import sweep_stale_temp_files

    sub = tmp_path / "state"
    sub.mkdir()
    old = sub / ".state.json.tmp.123.456.abcd1234"
    old.write_text("x", encoding="utf-8")
    fresh = sub / ".other.json.tmp.999.111.deadbeef"
    fresh.write_text("y", encoding="utf-8")
    real = sub / "state.json"
    real.write_text("real", encoding="utf-8")

    aged = time.time() - 7200
    os.utime(old, (aged, aged))

    removed = sweep_stale_temp_files(tmp_path, min_age_sec=3600)
    assert removed == 1
    assert not old.exists()      # stale temp reaped
    assert fresh.exists()        # too young — kept (may be an in-flight write)
    assert real.exists()         # not a temp file — untouched

    # missing dir is a no-op, never raises
    assert sweep_stale_temp_files(tmp_path / "does-not-exist") == 0
