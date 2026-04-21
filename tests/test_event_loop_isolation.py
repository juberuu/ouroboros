"""Regression tests for asyncio event loop isolation in conftest.py.

Verifies that the pytest_runtest_call + pytest_runtest_teardown hooks keep
the test suite clean — every test starts with a fresh open loop, tests that
call asyncio.run() do not contaminate subsequent tests, and fixture finalizers
that call asyncio.get_event_loop() after asyncio.run() still work correctly.
"""
import asyncio
import pytest


def test_event_loop_is_available_and_open():
    """asyncio.get_event_loop() must return an open loop inside any test."""
    loop = asyncio.get_event_loop()
    assert loop is not None
    assert not loop.is_closed()


def test_event_loop_run_until_complete_works():
    """asyncio.get_event_loop().run_until_complete() must not raise."""
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0))


def test_asyncio_run_does_not_contaminate_next_test():
    """Test that calls asyncio.run() — the hook must clean up for the NEXT test."""
    asyncio.run(asyncio.sleep(0))


def test_event_loop_is_fresh_after_contamination():
    """Must have a fresh open loop even when the previous test called asyncio.run()."""
    loop = asyncio.get_event_loop()
    assert loop is not None
    assert not loop.is_closed()
    asyncio.get_event_loop().run_until_complete(asyncio.sleep(0))


# ---------------------------------------------------------------------------
# Fixture-finalizer regression test
# ---------------------------------------------------------------------------

@pytest.fixture
def _finalizer_reads_loop(request):
    """Fixture that registers a finalizer which calls asyncio.get_event_loop().

    Without the pytest_runtest_teardown hook, this would raise RuntimeError
    after any test that called asyncio.run(), because the call-phase hook
    sets the loop to None before teardown begins.
    """
    def fin():
        loop = asyncio.get_event_loop()
        assert loop is not None, "No current event loop in fixture finalizer"
        assert not loop.is_closed(), "Event loop is closed in fixture finalizer"

    request.addfinalizer(fin)


def test_fixture_finalizer_sees_valid_loop_after_asyncio_run(_finalizer_reads_loop):
    """Fixture finalizer must see a valid open loop even after asyncio.run().

    The pytest_runtest_teardown hook installs a fresh temporary loop during
    teardown so that _finalizer_reads_loop's finalizer can call
    asyncio.get_event_loop() without RuntimeError.
    """
    asyncio.run(asyncio.sleep(0))
