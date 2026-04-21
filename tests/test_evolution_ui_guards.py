"""Regression guards for evolution/consciousness UI wiring."""

import os
import pathlib

REPO = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_evolution_page_supports_refresh_and_runtime_state():
    source = _read("web/modules/evolution.js")

    assert 'id="evo-refresh"' in source
    assert "Runtime Status" in source
    assert "fetch(`/api/evolution-data${suffix}`" in source
    assert "ws.on('open', () => {" in source
    assert "window.addEventListener('ouro:page-shown'" in source
    assert "document.addEventListener('visibilitychange'" in source
    assert "renderRuntimeState(runtime, data.generated_at || '');" in source
    assert "evolution_state" in source
    assert "bg_consciousness_state" in source


def test_server_and_navigation_expose_runtime_refresh_hooks():
    server_source = _read("server.py")
    app_source = _read("web/app.js")
    evo_source = _read("web/modules/evolution.js")
    chat_source = _read("web/modules/chat.js")

    assert "def _describe_bg_consciousness_state(requested_enabled: bool) -> dict:" in server_source
    assert '"evolution_state": evolution_state,' in server_source
    assert '"bg_consciousness_state": bg_state,' in server_source
    assert 'request.query_params.get("force")' in server_source
    assert "window.dispatchEvent(new CustomEvent('ouro:page-shown', { detail: { page: name } }));" in app_source
    assert "evo-runtime-detail" in evo_source
    assert "data?.evolution_state?.detail" in chat_source
    assert "data?.bg_consciousness_state?.detail" in chat_source
