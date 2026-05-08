import pathlib
import sys

from ouroboros.extension_companion import (
    CompanionDescriptor,
    CompanionSupervisor,
    init_server_process_pid,
)
from ouroboros.extension_loader import PluginAPIImpl, _PluginAPIConfig
import ouroboros.extension_loader as extension_loader


def test_companion_supervisor_starts_and_stops_process(tmp_path: pathlib.Path) -> None:
    init_server_process_pid()
    supervisor = CompanionSupervisor(tmp_path)
    descriptor = CompanionDescriptor(
        skill_name="demo",
        name="sleepy",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
    )

    assert supervisor.start(descriptor)
    snapshot = supervisor.snapshot()
    assert "demo:sleepy" in snapshot
    pid = int(snapshot["demo:sleepy"]["pid"])
    assert pid > 0

    supervisor.stop("demo", "sleepy", timeout_sec=1)
    assert supervisor.snapshot() == {}


def test_panic_kill_all_clears_runtime_table(tmp_path: pathlib.Path) -> None:
    init_server_process_pid()
    supervisor = CompanionSupervisor(tmp_path)
    descriptor = CompanionDescriptor(
        skill_name="demo",
        name="panic",
        command=[sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
    )

    assert supervisor.start(descriptor)
    assert supervisor.snapshot()
    supervisor.panic_kill_all()
    assert supervisor.snapshot() == {}


def test_plugin_api_companion_registration_uses_reviewed_manifest_descriptor(tmp_path: pathlib.Path) -> None:
    init_server_process_pid(999999)
    state_dir = tmp_path / "state"
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir()
    api = PluginAPIImpl(_PluginAPIConfig(
        skill_name="demo",
        permissions=["companion_process"],
        env_allowlist=[],
        state_dir=state_dir,
        settings_reader=lambda: {},
        companion_processes=[{
            "name": "daemon",
            "command": ["python3", "scripts/daemon.py"],
            "runtime": "python3",
        }],
        skill_dir=skill_dir,
    ))

    api.register_companion_process("daemon")

    api._close_registration()
    init_server_process_pid()


def test_plugin_api_companion_uses_staged_skill_root_as_cwd(tmp_path: pathlib.Path, monkeypatch) -> None:
    init_server_process_pid()
    staged_skill = tmp_path / "import" / "skill"
    (staged_skill / "scripts").mkdir(parents=True)
    (staged_skill / "scripts" / "daemon.py").write_text("print('ok')\n", encoding="utf-8")
    captured = {}

    class FakeSupervisor:
        def start(self, descriptor):
            captured["descriptor"] = descriptor
            return True

    monkeypatch.setattr(extension_loader, "get_global_supervisor", lambda: FakeSupervisor())
    api = PluginAPIImpl(_PluginAPIConfig(
        skill_name="demo",
        permissions=["companion_process"],
        env_allowlist=[],
        state_dir=tmp_path / "state",
        settings_reader=lambda: {},
        companion_processes=[{
            "name": "daemon",
            "command": ["python3", "scripts/daemon.py"],
            "runtime": "python3",
            "env": {"HOST_SERVICE_URL": "https://evil.example", "HOST_SERVICE_TOKEN": "evil"},
        }],
        skill_dir=tmp_path / "mutable",
        runtime_skill_dir=staged_skill,
    ))

    api.register_companion_process("daemon")

    descriptor = captured["descriptor"]
    assert descriptor.cwd == staged_skill
    assert (descriptor.cwd / "scripts" / "daemon.py").is_file()
    assert descriptor.env["HOST_SERVICE_URL"].startswith("http://127.0.0.1:")
    assert descriptor.env["HOST_SERVICE_TOKEN"] != "evil"
