import json
import sys

from ouroboros.tools.registry import ToolRegistry


def _force_advanced_runtime(monkeypatch):
    from ouroboros import config as cfg

    cfg.reset_runtime_mode_baseline_for_tests()
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    monkeypatch.delenv(cfg.BOOT_RUNTIME_MODE_ENV_KEY, raising=False)


def test_task_scoped_service_lifecycle(tmp_path, monkeypatch):
    _force_advanced_runtime(monkeypatch)
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir()
    registry = ToolRegistry(repo_dir=repo, drive_root=drive)
    registry._ctx.task_id = "task-1"

    start = registry.execute("start_service", {
        "name": "demo",
        "cmd": [
            sys.executable,
            "-c",
            "import time; print('READY', flush=True); time.sleep(60)",
        ],
        "readiness": {"log_contains": "READY", "timeout_sec": 3},
    })
    start_payload = json.loads(start)
    assert start_payload["state"] == "running"
    assert start_payload["ready"] is True
    assert start_payload["pid"] > 0

    logs = json.loads(registry.execute("service_logs", {"name": "demo", "tail": 200}))
    assert "READY" in logs["tail"]
    assert logs["full_log_ref"]["sha256"]

    stopped = json.loads(registry.execute("stop_service", {"name": "demo"}))
    assert stopped["state"] == "exited"
    assert registry.execute("service_status", {"name": "demo"}).startswith("⚠️ SERVICE_NOT_FOUND")


def test_service_logs_redact_secret_assignments(tmp_path, monkeypatch):
    _force_advanced_runtime(monkeypatch)
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir()
    registry = ToolRegistry(repo_dir=repo, drive_root=drive)
    registry._ctx.task_id = "task-1"

    start = registry.execute("start_service", {
        "name": "secretlog",
        "cmd": [
            sys.executable,
            "-c",
            "print('OPENAI_API_KEY=thisisaverylongsecretvalue123456', flush=True)",
        ],
        "readiness": {"timeout_sec": 1},
    })
    assert json.loads(start)["state"] in {"running", "exited"}
    logs = json.loads(registry.execute("service_logs", {"name": "secretlog", "tail": 500}))
    registry.execute("stop_service", {"name": "secretlog"})

    assert "thisisaverylongsecretvalue" not in logs["tail"]
    assert "***REDACTED***" in logs["tail"]


def test_service_logs_tail_is_capped(tmp_path, monkeypatch):
    _force_advanced_runtime(monkeypatch)
    monkeypatch.setattr("ouroboros.safety.check_safety", lambda *a, **k: (True, ""))
    repo = tmp_path / "repo"
    drive = tmp_path / "data"
    repo.mkdir()
    registry = ToolRegistry(repo_dir=repo, drive_root=drive)
    registry._ctx.task_id = "task-1"

    start = registry.execute("start_service", {
        "name": "bigtail",
        "cmd": [sys.executable, "-c", "print('x' * 120000, flush=True)"],
        "readiness": {"timeout_sec": 1},
    })
    assert json.loads(start)["state"] in {"running", "exited"}
    logs = json.loads(registry.execute("service_logs", {"name": "bigtail", "tail": 1_000_000}))
    registry.execute("stop_service", {"name": "bigtail"})

    assert len(logs["tail"]) <= 80_000
