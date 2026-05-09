import types

import launcher


def test_host_service_cleanup_uses_configured_port(monkeypatch):
    killed: list[int] = []

    monkeypatch.setenv("OUROBOROS_HOST_SERVICE_PORT", "9876")
    monkeypatch.setattr(launcher, "_kill_stale_on_port", lambda port: killed.append(port))

    launcher._kill_stale_runtime_ports(8765)

    assert killed == [8765, 9876]


def test_agent_lifecycle_preflight_cleans_host_service_port(monkeypatch):
    killed: list[int] = []

    class FakeProcess:
        returncode = 0

        def wait(self):
            launcher._shutdown_event.set()

    launcher._shutdown_event.clear()
    monkeypatch.setattr(launcher, "_host_service_port", lambda: 9876)
    monkeypatch.setattr(launcher, "_kill_stale_on_port", lambda port: killed.append(port))
    monkeypatch.setattr(launcher, "start_agent", lambda port: FakeProcess())
    monkeypatch.setattr(launcher, "_poll_port_file", lambda timeout=30: 8765)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda port, timeout=30.0: True)
    monkeypatch.setattr(launcher, "_agent_job", None)
    monkeypatch.setattr(launcher, "log", types.SimpleNamespace(info=lambda *args, **kwargs: None))

    try:
        launcher.agent_lifecycle_loop(port=8765)
    finally:
        launcher._shutdown_event.clear()

    assert killed[:2] == [8765, 9876]
