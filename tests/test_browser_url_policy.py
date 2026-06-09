"""Unit tests for the subagent browser URL policy (no Playwright needed).

Verifies the relaxed `_is_subagent_blocked_browser_url`: readonly/acting subagents
may browse external HTTP(S), localhost on non-Ouroboros ports, and file:// under
their workspace, while the Ouroboros control-plane ports, private/link-local IPs,
DNS-rebind, and other schemes stay blocked.
"""
from __future__ import annotations

from types import SimpleNamespace

from ouroboros.tools.browser import _is_subagent_blocked_browser_url


def _ctx(workspace_root: str = ""):
    return SimpleNamespace(workspace_root=workspace_root)


def test_ouroboros_control_ports_blocked_on_loopback():
    for url in (
        "http://127.0.0.1:8765",
        "http://localhost:8765/api/settings",
        "http://127.0.0.1:8766",   # local model
        "http://127.0.0.1:8767",   # host service
        "http://[::1]:8765",
    ):
        assert _is_subagent_blocked_browser_url(url, _ctx()) is True, url


def test_localhost_non_control_ports_allowed():
    for url in (
        "http://localhost:3000/",
        "http://127.0.0.1:5173/app",
        "http://localhost/",
        "http://127.0.0.1:8080",
    ):
        assert _is_subagent_blocked_browser_url(url, _ctx()) is False, url


def test_private_and_linklocal_blocked():
    for url in (
        "http://192.168.1.1",
        "http://10.0.0.1",
        "http://169.254.169.254",
        "http://[::]/",
        "http://172.16.0.1",
    ):
        assert _is_subagent_blocked_browser_url(url, _ctx()) is True, url


def test_non_http_schemes_blocked():
    for url in ("data:text/html,<h1>x</h1>", "about:blank", "ws://localhost:3000"):
        assert _is_subagent_blocked_browser_url(url, _ctx()) is True, url


def test_file_url_blocked_without_workspace():
    assert _is_subagent_blocked_browser_url("file:///etc/passwd", _ctx()) is True
    assert _is_subagent_blocked_browser_url("file:///tmp/app/index.html", _ctx("")) is True


def test_file_url_scoped_to_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "build").mkdir(parents=True)
    app = ws / "build" / "index.html"
    app.write_text("<h1>app</h1>", encoding="utf-8")
    outside = tmp_path / "secret.json"
    outside.write_text("{}", encoding="utf-8")
    ctx = _ctx(str(ws))
    # Path.as_uri() yields a platform-correct file URL (file:///C:/... on Windows).
    assert _is_subagent_blocked_browser_url(app.as_uri(), ctx) is False
    assert _is_subagent_blocked_browser_url(outside.as_uri(), ctx) is True


def test_isolated_server_port_blocked_via_env(monkeypatch):
    # The isolated server sets BOTH control ports to independent free ports; only the
    # ACTUAL configured ports are blocked (no `+1` adjacency guessing).
    monkeypatch.setenv("OUROBOROS_SERVER_PORT", "8900")
    monkeypatch.setenv("OUROBOROS_HOST_SERVICE_PORT", "8902")
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:8900", _ctx()) is True   # agent API
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:8902", _ctx()) is True   # host service
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:8901", _ctx()) is False  # adjacent, NOT a control port
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:3000", _ctx()) is False


def test_loopback_blocklist_includes_local_model_and_state_port(monkeypatch, tmp_path):
    # LOCAL_MODEL_PORT (configured) must be blocked even on a custom port.
    monkeypatch.setenv("LOCAL_MODEL_PORT", "9001")
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:9001", _ctx()) is True
    # The ACTUAL bound server port (find_free_port fallback) recorded in state.
    import ouroboros.config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path)
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "server_port").write_text("9100", encoding="utf-8")
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:9100", _ctx()) is True
    assert _is_subagent_blocked_browser_url("http://127.0.0.1:3000", _ctx()) is False


def test_owner_settings_post_blocked_in_browser():
    """A browser POST /api/settings carrying an owner-only self-modification toggle
    (the click+Save bypass) must be blocked for every browser session."""
    from ouroboros.tools.browser import _is_owner_settings_self_elevation_post

    def req(method, url, body):
        return SimpleNamespace(method=method, url=url, post_data=body)

    base = "http://127.0.0.1:8765/api/settings"
    assert _is_owner_settings_self_elevation_post(req("POST", base, '{"OUROBOROS_POST_TASK_EVOLUTION":"true"}')) is True
    assert _is_owner_settings_self_elevation_post(req("POST", base, '{"OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS":"on"}')) is True
    assert _is_owner_settings_self_elevation_post(req("POST", base, '{"OUROBOROS_EVOLUTION_PERSISTENT_OBJECTIVE":"x"}')) is True
    assert _is_owner_settings_self_elevation_post(req("GET", base, None)) is False
    assert _is_owner_settings_self_elevation_post(req("POST", base, '{"OPENAI_API_KEY":"x"}')) is False
    assert _is_owner_settings_self_elevation_post(
        req("POST", "http://127.0.0.1:8765/api/tasks", '{"OUROBOROS_POST_TASK_EVOLUTION":"true"}')) is False


def test_vlm_and_screenshot_available_to_subagents():
    from ouroboros.tool_capabilities import (
        ACTING_SUBAGENT_TOOL_NAMES,
        LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
    )
    from ouroboros.tools.registry import _WORKSPACE_ALLOWED_TOOLS

    # available_tools()/schemas()/execute() AND-compose the workspace allowlist with the
    # subagent allowlists for a WORKSPACE local-readonly/acting subagent, so a tool must be
    # in ALL THREE to stay discoverable/executable (vlm_query was missing from the
    # workspace allowlist, so workspace subagents could not see/run it despite the relax).
    for name in ("analyze_screenshot", "vlm_query", "browse_page", "browser_action"):
        assert name in LOCAL_READONLY_SUBAGENT_TOOL_NAMES, name
        assert name in ACTING_SUBAGENT_TOOL_NAMES, name
        assert name in _WORKSPACE_ALLOWED_TOOLS, name
