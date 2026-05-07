# tests/conftest.py — shared pytest fixtures for the Ouroboros test suite.
#
# Loaded automatically by pytest before any test module runs.
import asyncio
import json
import pathlib
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

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
def _reset_runtime_mode_baseline_between_tests():
    """v5.1.2 iter-2 test isolation fix (Gemini finding F2-7):
    ``ouroboros.config._BOOT_RUNTIME_MODE`` is a module-level global
    pinned by ``initialize_runtime_mode_baseline``. Tests that boot a
    Starlette ``TestClient`` trigger ``server.lifespan`` which pins the
    baseline; subsequent tests inherit the pin and may see different
    rank-comparison behaviour depending on test order. Reset to ``None``
    + remove the env var on every test boundary so each test starts
    with the documented "no pin" state. Tests that need a pin call
    ``initialize_runtime_mode_baseline(...)`` explicitly.
    """
    try:
        from ouroboros.config import reset_runtime_mode_baseline_for_tests
        reset_runtime_mode_baseline_for_tests()
    except Exception:
        pass
    yield
    try:
        from ouroboros.config import reset_runtime_mode_baseline_for_tests
        reset_runtime_mode_baseline_for_tests()
    except Exception:
        pass


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


@pytest.fixture
def make_git_repo(tmp_path):
    """Return a helper that creates a minimal committed git repo."""
    def _make(files=None, message="init") -> pathlib.Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        for rel, content in dict(files or {"README.md": "init\n"}).items():
            path = repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=test@ouroboros", "-c", "user.name=TestBot", "commit", "-m", message],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return repo
    return _make


@pytest.fixture
def tool_context(tmp_path):
    repo_dir = tmp_path / "repo"
    drive_root = tmp_path / "data"
    repo_dir.mkdir()
    drive_root.mkdir()
    return SimpleNamespace(
        repo_dir=repo_dir,
        drive_root=drive_root,
        pending_events=[],
        current_chat_id=0,
        task_id="test-task",
        repo_path=lambda rel: (repo_dir / rel).resolve(),
        drive_path=lambda rel: (drive_root / rel).resolve(),
        drive_logs=lambda: (drive_root / "logs").resolve(),
        emit_progress_fn=lambda _msg: None,
    )


@pytest.fixture
def make_chat_mock():
    def _make(content="", usage=None):
        mock = MagicMock()
        mock.chat.return_value = (
            {"content": content, "tool_calls": []},
            usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        return mock
    return _make


@pytest.fixture
def make_extension_skill(tmp_path):
    def _make(root=None, name="demo", *, plugin="", permissions=None, render=None) -> pathlib.Path:
        root_path = pathlib.Path(root or tmp_path)
        skill_dir = root_path / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        perms = permissions or ["tool", "route", "widget"]
        manifest = {
            "name": name,
            "type": "extension",
            "version": "0.1.0",
            "permissions": perms,
            "entry": "plugin.py",
        }
        if render is not None:
            manifest["ui_tab"] = {"tab_id": "main", "title": "Main", "render": render}
        (skill_dir / "skill.json").write_text(json.dumps(manifest), encoding="utf-8")
        (skill_dir / "plugin.py").write_text(plugin or "def register(api):\n    return None\n", encoding="utf-8")
        return skill_dir
    return _make
