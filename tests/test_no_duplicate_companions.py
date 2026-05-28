import pathlib
import sys

import pytest

from ouroboros.extension_companion import (
    CompanionDescriptor,
    CompanionSupervisor,
    init_server_process_pid,
)


def test_companions_start_only_in_server_process(tmp_path: pathlib.Path) -> None:
    init_server_process_pid(999999)
    supervisor = CompanionSupervisor(tmp_path)
    descriptor = CompanionDescriptor(
        skill_name="demo",
        name="worker_skip",
        command=[sys.executable, "-c", "raise SystemExit(99)"],
        cwd=tmp_path,
        env={},
    )

    assert supervisor.start(descriptor) is False
    assert supervisor.snapshot() == {}


def test_fresh_worker_import_is_not_server_process(monkeypatch) -> None:
    import importlib
    import ouroboros.extension_companion as companion

    monkeypatch.delenv("OUROBOROS_SERVER_PROCESS_PID", raising=False)
    reloaded = importlib.reload(companion)
    assert reloaded.is_server_process() is False
    reloaded.init_server_process_pid()


@pytest.fixture(autouse=True)
def _restore_server_pid():
    yield
    init_server_process_pid()
