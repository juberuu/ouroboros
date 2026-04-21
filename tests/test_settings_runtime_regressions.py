import asyncio
import inspect
import importlib
import json
import os
import pathlib
import sys
import types

import pytest


def _reload_config(monkeypatch, tmp_path):
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(settings_path))
    import ouroboros.config as config_module

    return importlib.reload(config_module), settings_path


def _reload_server(monkeypatch, tmp_path):
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(tmp_path / "settings.json"))
    monkeypatch.delenv("OUROBOROS_MANAGED_BY_LAUNCHER", raising=False)
    import ouroboros.config as config_module
    import server as server_module

    importlib.reload(config_module)
    return importlib.reload(server_module)


def test_load_settings_uses_env_fallback_for_missing_keys(monkeypatch, tmp_path):
    config_module, settings_path = _reload_config(monkeypatch, tmp_path)
    settings_path.write_text(json.dumps({"TOTAL_BUDGET": 7}), encoding="utf-8")
    file_root = tmp_path / "workspace"
    file_root.mkdir()
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-env")
    monkeypatch.setenv("OUROBOROS_FILE_BROWSER_DEFAULT", str(file_root))

    settings = config_module.load_settings()

    assert settings["TOTAL_BUDGET"] == 7.0
    assert settings["OPENAI_API_KEY"] == "sk-openai-env"
    assert settings["OUROBOROS_FILE_BROWSER_DEFAULT"] == str(file_root)


def test_load_settings_prefers_explicit_file_values_over_env(monkeypatch, tmp_path):
    config_module, settings_path = _reload_config(monkeypatch, tmp_path)
    file_root = tmp_path / "file-root"
    file_root.mkdir()
    settings_path.write_text(
        json.dumps(
            {
                "OPENAI_API_KEY": "sk-openai-file",
                "OUROBOROS_FILE_BROWSER_DEFAULT": str(file_root),
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-env")
    monkeypatch.setenv("OUROBOROS_FILE_BROWSER_DEFAULT", str(tmp_path / "env-root"))

    settings = config_module.load_settings()

    assert settings["OPENAI_API_KEY"] == "sk-openai-file"
    assert settings["OUROBOROS_FILE_BROWSER_DEFAULT"] == str(file_root)


def test_merge_settings_payload_preserves_masked_secrets(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)

    merged = server_module._merge_settings_payload(
        {
            "OPENAI_API_KEY": "sk-openai-real-secret",
            "OUROBOROS_MODEL": "openai::gpt-4.1",
        },
        {
            "OPENAI_API_KEY": "sk-opena...",
            "OUROBOROS_MODEL": "openai::gpt-5",
        },
    )

    assert merged["OPENAI_API_KEY"] == "sk-openai-real-secret"
    assert merged["OUROBOROS_MODEL"] == "openai::gpt-5"


def test_merge_settings_payload_allows_explicit_secret_clear(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)

    merged = server_module._merge_settings_payload(
        {"OPENAI_API_KEY": "sk-openai-real-secret"},
        {"OPENAI_API_KEY": ""},
    )

    assert merged["OPENAI_API_KEY"] == ""


def test_restart_current_process_falls_back_to_spawn_on_exec_failure(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)
    called = {}
    spawned = {}
    import ouroboros.server_control as server_control_module

    def _fake_execvpe(executable, argv, env):
        called["executable"] = executable
        called["argv"] = argv
        called["env"] = env
        raise RuntimeError("stop")

    def _fake_popen(argv, env=None, cwd=None):
        spawned["argv"] = argv
        spawned["env"] = env
        spawned["cwd"] = cwd
        return object()

    monkeypatch.setattr(server_control_module.os, "execvpe", _fake_execvpe)
    monkeypatch.setattr(server_control_module.subprocess, "Popen", _fake_popen)

    server_module._restart_current_process("127.0.0.1", 9032)

    assert called["executable"] == sys.executable
    assert called["argv"][0] == sys.executable
    assert called["env"]["OUROBOROS_SERVER_HOST"] == "127.0.0.1"
    assert called["env"]["OUROBOROS_SERVER_PORT"] == "9032"
    assert "OUROBOROS_MANAGED_BY_LAUNCHER" not in called["env"]
    assert spawned["argv"] == called["argv"]
    assert spawned["env"]["OUROBOROS_SERVER_PORT"] == "9032"
    assert spawned["cwd"] == str(server_module.REPO_DIR)


def test_api_settings_post_rejects_local_only_unrouted_runtime(monkeypatch, tmp_path):
    for key in (
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_COMPATIBLE_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    server_module = _reload_server(monkeypatch, tmp_path)

    class _Request:
        async def json(self):
            return {
                "LOCAL_MODEL_SOURCE": "Qwen/Qwen2.5-7B-Instruct-GGUF",
                "LOCAL_MODEL_FILENAME": "qwen2.5-7b-instruct-q3_k_m.gguf",
                "USE_LOCAL_MAIN": False,
                "USE_LOCAL_CODE": False,
                "USE_LOCAL_LIGHT": False,
                "USE_LOCAL_FALLBACK": False,
            }

    response = asyncio.run(server_module.api_settings_post(_Request()))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 400
    assert payload["error"] == "Local-only setups must route at least one model to the local runtime."


def test_api_command_uses_local_enqueue_semantics(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)
    captured = {}
    import supervisor.message_bus as message_bus

    class _Bridge:
        def ui_send(self, text, **kwargs):
            captured["text"] = text
            captured["kwargs"] = kwargs

    class _Request:
        async def json(self):
            return {"cmd": "status"}

    monkeypatch.setattr(message_bus, "get_bridge", lambda: _Bridge())

    response = asyncio.run(server_module.api_command(_Request()))
    payload = json.loads(response.body.decode("utf-8"))

    assert response.status_code == 200
    assert payload == {"status": "ok"}
    assert captured == {"text": "status", "kwargs": {"broadcast": False}}


@pytest.mark.skipif(
    not (pathlib.Path(__file__).resolve().parents[1] / "launcher.py").exists(),
    reason="launcher.py not present in repo (bundle-only)",
)
def test_launcher_marks_server_as_managed():
    launcher_source = (pathlib.Path(__file__).resolve().parents[1] / "launcher.py").read_text(encoding="utf-8")

    assert 'env["OUROBOROS_MANAGED_BY_LAUNCHER"] = "1"' in launcher_source


def test_local_dev_bootstrap_skips_safe_restart(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)
    calls = []

    fake_git_ops = types.SimpleNamespace(
        init=lambda **kwargs: calls.append(("init", kwargs)),
        ensure_repo_present=lambda: calls.append("ensure_repo_present"),
        safe_restart=lambda **kwargs: calls.append(("safe_restart", kwargs)) or (True, "unexpected"),
        sync_runtime_dependencies=lambda reason: calls.append(("deps", reason)) or (True, "requirements"),
        import_test=lambda: calls.append("import_test") or {"ok": True, "returncode": 0},
    )

    monkeypatch.setattr(server_module, "_LAUNCHER_MANAGED", False)
    monkeypatch.setattr(
        server_module,
        "setup_remote_if_configured",
        lambda settings, log: calls.append(("setup_remote_if_configured", dict(settings))),
    )

    ok, msg = server_module._bootstrap_supervisor_repo({"TOTAL_BUDGET": 1}, git_ops_module=fake_git_ops)

    assert ok
    assert msg == "OK: local-dev bootstrap"
    assert ("deps", "bootstrap_local_dev") in calls
    assert "import_test" in calls
    assert not any(isinstance(call, tuple) and call[0] == "safe_restart" for call in calls)


def test_launcher_bootstrap_uses_safe_restart(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)
    calls = []

    fake_git_ops = types.SimpleNamespace(
        init=lambda **kwargs: calls.append(("init", kwargs)),
        ensure_repo_present=lambda: calls.append("ensure_repo_present"),
        safe_restart=lambda **kwargs: calls.append(("safe_restart", kwargs)) or (True, "OK: ouroboros"),
        sync_runtime_dependencies=lambda reason: (_ for _ in ()).throw(AssertionError(reason)),
        import_test=lambda: (_ for _ in ()).throw(AssertionError("import_test should not run")),
    )

    monkeypatch.setattr(server_module, "_LAUNCHER_MANAGED", True)
    monkeypatch.setattr(
        server_module,
        "setup_remote_if_configured",
        lambda settings, log: calls.append(("setup_remote_if_configured", dict(settings))),
    )

    ok, msg = server_module._bootstrap_supervisor_repo({"TOTAL_BUDGET": 1}, git_ops_module=fake_git_ops)

    assert ok
    assert msg == "OK: ouroboros"
    assert any(
        isinstance(call, tuple)
        and call[0] == "safe_restart"
        and call[1]["reason"] == "bootstrap"
        and call[1]["unsynced_policy"] == "rescue_and_reset"
        for call in calls
    )


def test_run_supervisor_keeps_safe_restart_in_event_context(monkeypatch, tmp_path):
    server_module = _reload_server(monkeypatch, tmp_path)
    source = inspect.getsource(server_module._run_supervisor)

    assert "from supervisor.git_ops import safe_restart" in source
    assert "safe_restart=safe_restart" in source


def test_set_tool_timeout_persists_and_applies_immediately(monkeypatch, tmp_path):
    config_module, settings_path = _reload_config(monkeypatch, tmp_path)
    import ouroboros.tools.control as control_module

    control_module = importlib.reload(control_module)
    result = control_module._set_tool_timeout(object(), 777)

    assert "777s" in result
    saved = json.loads(settings_path.read_text(encoding="utf-8"))
    assert saved["OUROBOROS_TOOL_TIMEOUT_SEC"] == 777
    assert os.environ["OUROBOROS_TOOL_TIMEOUT_SEC"] == "777"
    assert config_module.load_settings()["OUROBOROS_TOOL_TIMEOUT_SEC"] == 777


def test_get_tool_timeout_prefers_settings_file_over_stale_env(monkeypatch, tmp_path):
    _config_module, settings_path = _reload_config(monkeypatch, tmp_path)
    settings_path.write_text(json.dumps({"OUROBOROS_TOOL_TIMEOUT_SEC": 888}), encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_TOOL_TIMEOUT_SEC", "120")

    import ouroboros.loop_tool_execution as loop_tool_execution_module

    loop_tool_execution_module = importlib.reload(loop_tool_execution_module)

    class _Tools:
        def get_timeout(self, name):
            return 360

    assert loop_tool_execution_module._get_tool_timeout(_Tools(), "run_shell") == 888
