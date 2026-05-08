from types import SimpleNamespace


def test_main_normal_exit_does_not_run_emergency_cleanup(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            return None

    monkeypatch.setattr(server, "load_settings", lambda: {"OUROBOROS_SERVER_HOST": "127.0.0.1"})
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "get_network_auth_startup_warning", lambda _host: "")
    monkeypatch.setattr(server, "validate_network_auth_configuration", lambda _host: "")
    monkeypatch.setattr(server, "find_free_port", lambda _host, port: port)
    monkeypatch.setattr(server, "write_port_file", lambda *_a, **_k: None)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_emergency_process_cleanup", lambda: cleanup_calls.append("cleanup"))
    server._restart_requested.clear()

    assert server.main() == 0
    assert cleanup_calls == []


def test_main_graceful_restart_cleanup_avoids_port_sweep(monkeypatch):
    import server

    cleanup_calls = []

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            server._restart_requested.set()
            return None

    class ExitCalled(RuntimeError):
        pass

    monkeypatch.setattr(server, "load_settings", lambda: {"OUROBOROS_SERVER_HOST": "127.0.0.1"})
    monkeypatch.setattr(server, "parse_server_args", lambda *_a, **_k: SimpleNamespace(host="127.0.0.1", port=8765))
    monkeypatch.setattr(server, "get_network_auth_startup_warning", lambda _host: "")
    monkeypatch.setattr(server, "validate_network_auth_configuration", lambda _host: "")
    monkeypatch.setattr(server, "find_free_port", lambda _host, port: port)
    monkeypatch.setattr(server, "write_port_file", lambda *_a, **_k: None)
    monkeypatch.setattr(server.uvicorn, "Config", lambda *a, **k: object())
    monkeypatch.setattr(server.uvicorn, "Server", FakeServer)
    monkeypatch.setattr(server, "_LAUNCHER_MANAGED", True)
    monkeypatch.setattr(server, "_emergency_process_cleanup", lambda **kw: cleanup_calls.append(kw))
    monkeypatch.setattr(server.os, "_exit", lambda code: (_ for _ in ()).throw(ExitCalled(code)))
    server._restart_requested.clear()

    try:
        server.main()
    except ExitCalled:
        pass
    finally:
        server._restart_requested.clear()

    assert cleanup_calls == [{"port_sweep": False}]
