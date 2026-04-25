# tests/conftest.py — shared pytest fixtures for the Ouroboros test suite.
#
# Loaded automatically by pytest before any test module runs.
import asyncio
import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):  # noqa: ARG001
    """Install a fresh asyncio event loop for the test *call* phase.

    Problem: asyncio.run() closes the loop it creates, leaving no current
    loop for the next test's asyncio.get_event_loop() call (RuntimeError).

    This hook installs a fresh loop BEFORE the test body and closes it
    AFTER, preventing cross-test contamination.  The loop is set to None
    after the call phase; a companion pytest_runtest_teardown hook
    installs a temporary loop for fixture finalizers.
    """
    test_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(test_loop)
    yield  # test body runs here
    test_loop.close()
    asyncio.set_event_loop(None)


@pytest.fixture(autouse=True)
def _hide_bundled_skills(monkeypatch):
    """Phase 5: skill tests must not see the shipped ``repo/skills/``
    reference skills. Tests build their own fixtures under ``tmp_path``
    and rely on ``discover_skills`` returning exactly those — letting
    the bundled reference skills leak into the view would make every
    test assertion brittle to changes in the shipped reference set.

    v4.50: ALSO neutralise the data-plane skills lookup so a developer
    machine with installed skills under ``~/Ouroboros/data/skills/`` does
    not poison test results. ``discover_skills`` consults
    ``_resolve_data_skills_dir`` for its primary scan; pinning that to
    ``None`` forces tests to either pass an explicit ``drive_root`` (the
    new contract since v4.50 — the helper now honours that argument)
    or stick to ``OUROBOROS_SKILLS_REPO_PATH`` fixtures under tmp_path.

    Production keeps the default behaviour untouched; this fixture only
    neutralises the bundled / data-plane lookups inside the pytest
    process.
    """
    monkeypatch.setattr(
        "ouroboros.skill_loader._bundled_skills_dir",
        lambda: None,
    )
    # Patch the data-plane resolver to None unless the caller supplied
    # an explicit ``drive_root`` (in which case the v4.50 implementation
    # honours that argument and never touches the global). The signature
    # check via ``*args`` keeps the fixture compatible with both the
    # legacy zero-arg call and the new drive_root-aware one.
    real_resolver = None
    try:
        import ouroboros.skill_loader as loader_mod
        real_resolver = loader_mod._resolve_data_skills_dir
    except Exception:
        pass

    def _hermetic_resolver(*args, **kwargs):
        if args and args[0] is not None:
            return real_resolver(*args, **kwargs) if real_resolver else None
        return None

    if real_resolver is not None:
        monkeypatch.setattr(
            "ouroboros.skill_loader._resolve_data_skills_dir",
            _hermetic_resolver,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):  # noqa: ARG001
    """Keep a valid asyncio event loop available during the teardown phase.

    Fixture finalizers run during teardown (LIFO order).  If they call
    asyncio.get_event_loop() after a test that used asyncio.run(), they
    would raise RuntimeError because pytest_runtest_call already cleared
    the loop.  This hook installs a temporary loop for teardown and
    closes it afterwards.
    """
    teardown_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(teardown_loop)
    yield  # fixture finalizers and teardown run here
    teardown_loop.close()
    asyncio.set_event_loop(None)
