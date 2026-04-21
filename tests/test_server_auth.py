"""Tests for the minimal network password gate."""

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import ouroboros.server_auth as server_auth


async def _ok(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True})


def _make_client(monkeypatch) -> TestClient:
    monkeypatch.setenv(server_auth.NETWORK_PASSWORD_KEY, "secret")
    monkeypatch.setattr(server_auth, "load_settings", lambda: {})
    app = server_auth.NetworkAuthGate(Starlette(routes=[
        Route("/", endpoint=_ok),
        Route("/api/health", endpoint=_ok),
        Route("/api/secret", endpoint=_ok),
    ]))
    return TestClient(app)


def test_validate_network_auth_configuration_allows_open_bind_without_password(monkeypatch):
    monkeypatch.delenv(server_auth.NETWORK_PASSWORD_KEY, raising=False)
    monkeypatch.setattr(server_auth, "load_settings", lambda: {})

    assert server_auth.validate_network_auth_configuration("127.0.0.1") is None
    assert server_auth.validate_network_auth_configuration("0.0.0.0") is None


def test_get_network_auth_startup_warning_warns_but_allows_open_bind(monkeypatch):
    monkeypatch.delenv(server_auth.NETWORK_PASSWORD_KEY, raising=False)
    monkeypatch.setattr(server_auth, "load_settings", lambda: {})

    assert server_auth.get_network_auth_startup_warning("127.0.0.1") is None
    warning = server_auth.get_network_auth_startup_warning("0.0.0.0")
    assert warning is not None
    assert "without OUROBOROS_NETWORK_PASSWORD" in warning

    monkeypatch.setenv(server_auth.NETWORK_PASSWORD_KEY, "secret")
    assert server_auth.get_network_auth_startup_warning("0.0.0.0") is None


def test_network_auth_gate_blocks_non_local_requests(monkeypatch):
    with _make_client(monkeypatch) as client:
        html_resp = client.get("/", follow_redirects=False)
        assert html_resp.status_code == 401
        assert "Enter the network password" in html_resp.text

        api_resp = client.get("/api/secret")
        assert api_resp.status_code == 401
        assert api_resp.json()["error"] == "Authentication required."

        health_resp = client.get("/api/health")
        assert health_resp.status_code == 200


def test_network_auth_gate_accepts_header_and_login_cookie(monkeypatch):
    with _make_client(monkeypatch) as client:
        header_resp = client.get("/", headers={"x-ouroboros-password": "secret"})
        assert header_resp.status_code == 200
        assert header_resp.json() == {"ok": True}

    with _make_client(monkeypatch) as client:
        login_resp = client.post(
            "/auth/login",
            json={"password": "secret", "next": "/"},
            follow_redirects=False,
        )
        assert login_resp.status_code == 200

        cookie_resp = client.get("/")
        assert cookie_resp.status_code == 200
        assert cookie_resp.json() == {"ok": True}
