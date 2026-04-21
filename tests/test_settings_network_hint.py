"""Tests for the LAN network status hint in /api/settings (_build_network_meta and _get_lan_ip)."""

import socket
from unittest.mock import MagicMock, patch


# ---- direct unit tests on the pure helper functions ----

class TestGetLanIp:
    """_get_lan_ip() — UDP socket trick."""

    def test_returns_string_on_success(self):
        from server import _get_lan_ip
        result = _get_lan_ip()
        assert isinstance(result, str)

    def test_returns_empty_string_on_socket_error(self):
        from server import _get_lan_ip
        with patch("server.socket") as mock_socket_module:
            mock_socket_module.AF_INET = socket.AF_INET
            mock_socket_module.SOCK_DGRAM = socket.SOCK_DGRAM
            cm = MagicMock()
            cm.__enter__ = MagicMock(return_value=cm)
            cm.__exit__ = MagicMock(return_value=False)
            cm.connect.side_effect = OSError("network unreachable")
            mock_socket_module.socket.return_value = cm
            result = _get_lan_ip()
        assert result == ""

    def test_result_looks_like_ip_or_empty(self):
        from server import _get_lan_ip
        result = _get_lan_ip()
        if result:
            parts = result.split(".")
            assert len(parts) == 4, f"Expected IPv4, got: {result}"


class TestBuildNetworkMeta:
    """_build_network_meta(bind_host, bind_port) — all three branches."""

    def test_loopback_host_returns_loopback_only(self):
        from server import _build_network_meta
        meta = _build_network_meta("127.0.0.1", 8765)
        assert meta["reachability"] == "loopback_only"
        assert meta["recommended_url"] == ""
        assert meta["lan_ip"] == ""
        assert "localhost" in meta["warning"].lower() or "not accessible" in meta["warning"].lower()

    def test_loopback_localhost_string(self):
        from server import _build_network_meta
        meta = _build_network_meta("localhost", 8765)
        assert meta["reachability"] == "loopback_only"

    def test_nonloopback_with_detected_ip(self):
        from server import _build_network_meta
        with patch("server._get_lan_ip", return_value="192.168.1.42"):
            meta = _build_network_meta("0.0.0.0", 8765)
        assert meta["reachability"] == "lan_reachable"
        assert meta["recommended_url"] == "http://192.168.1.42:8765"
        assert meta["lan_ip"] == "192.168.1.42"
        assert meta["warning"] == ""

    def test_nonloopback_without_detected_ip(self):
        from server import _build_network_meta
        with patch("server._get_lan_ip", return_value=""):
            meta = _build_network_meta("0.0.0.0", 9000)
        assert meta["reachability"] == "host_ip_unknown"
        assert "your-host-ip" in meta["recommended_url"]
        assert ":9000" in meta["recommended_url"]
        assert meta["warning"]

    def test_meta_contains_bind_host_and_port(self):
        from server import _build_network_meta
        with patch("server._get_lan_ip", return_value="10.0.0.5"):
            meta = _build_network_meta("0.0.0.0", 9001)
        assert meta["bind_host"] == "0.0.0.0"
        assert meta["bind_port"] == 9001

    def test_loopback_does_not_call_get_lan_ip(self):
        """For loopback hosts, _get_lan_ip must not be called (no unnecessary network socket)."""
        from server import _build_network_meta
        with patch("server._get_lan_ip") as mock_get_ip:
            _build_network_meta("127.0.0.1", 8765)
        mock_get_ip.assert_not_called()

    def test_custom_port_reflected_in_url(self):
        from server import _build_network_meta
        with patch("server._get_lan_ip", return_value="172.16.0.1"):
            meta = _build_network_meta("0.0.0.0", 19999)
        assert "19999" in meta["recommended_url"]


class TestBuildNetworkMetaNewCases:
    """Additional cases for the improved _build_network_meta."""

    def test_specific_nonloopback_host_used_directly(self):
        """When bind_host is a specific non-loopback address, use it as the LAN IP."""
        from server import _build_network_meta
        meta = _build_network_meta("192.168.1.50", 8765)
        assert meta["reachability"] == "lan_reachable"
        assert meta["lan_ip"] == "192.168.1.50"
        assert meta["recommended_url"] == "http://192.168.1.50:8765"

    def test_specific_nonloopback_does_not_call_get_lan_ip(self):
        """Specific non-wildcard bind must not call _get_lan_ip at all."""
        from server import _build_network_meta
        with patch("server._get_lan_ip") as mock_get_ip:
            _build_network_meta("10.0.0.5", 9000)
        mock_get_ip.assert_not_called()

    def test_container_env_downgrades_to_host_ip_unknown(self):
        """Wildcard bind inside Docker container should be host_ip_unknown."""
        from server import _build_network_meta
        with patch("server.is_container_env", return_value=True):
            meta = _build_network_meta("0.0.0.0", 8765)
        assert meta["reachability"] == "host_ip_unknown"
        assert meta["lan_ip"] == ""

    def test_non_container_wildcard_uses_get_lan_ip(self):
        """Wildcard bind outside container should call _get_lan_ip."""
        from server import _build_network_meta
        with patch("server.is_container_env", return_value=False):
            with patch("server._get_lan_ip", return_value="192.168.0.5"):
                meta = _build_network_meta("0.0.0.0", 8765)
        assert meta["reachability"] == "lan_reachable"
        assert meta["lan_ip"] == "192.168.0.5"

    def test_ipv6_loopback_is_loopback_only(self):
        """IPv6 loopback (::1) is classified as loopback_only."""
        from server import _build_network_meta
        meta = _build_network_meta("::1", 8765)
        assert meta["reachability"] == "loopback_only"

    def test_bracketed_ipv6_loopback_is_loopback_only(self):
        """Bracketed IPv6 loopback ([::1]) is also classified as loopback_only."""
        from server import _build_network_meta
        meta = _build_network_meta("[::1]", 8765)
        assert meta["reachability"] == "loopback_only"

    def test_bracketed_specific_ipv6_no_double_bracket_in_url(self):
        """Bracketed specific IPv6 host must not produce double-bracket URL."""
        from server import _build_network_meta
        meta = _build_network_meta("[2001:db8::5]", 8765)
        # Must not produce [[2001:db8::5]] — brackets added only once
        assert "[[" not in meta.get("recommended_url", ""), (
            f"Double-bracket bug: {meta.get('recommended_url')}"
        )
        if meta["reachability"] == "lan_reachable":
            assert meta["recommended_url"].startswith("http://[2001:db8::5]")

    def test_ipv6_wildcard_degrades_gracefully(self):
        """IPv6 wildcard (::) degrades to host_ip_unknown (AF_INET-only startup limitation)."""
        from server import _build_network_meta
        meta = _build_network_meta("::", 8765)
        # :: is not in _WILDCARD_HOSTS (IPv4-only wildcard set) and is explicitly
        # handled as a special case that degrades to host_ip_unknown because
        # server_entrypoint.py uses AF_INET only for port probing.
        assert meta["reachability"] == "host_ip_unknown"
        assert meta["lan_ip"] == ""
        assert "your-host-ip" in meta["recommended_url"]

    def test_dockerenv_file_detected(self):
        """/.dockerenv file presence triggers container detection on Linux."""
        from ouroboros.platform_layer import is_container_env
        import ouroboros.platform_layer as pl
        # Simulate Linux + /.dockerenv present
        with patch.object(pl, "IS_LINUX", True), \
             patch("ouroboros.platform_layer.pathlib.Path.exists", return_value=True):
            result = is_container_env()
        assert result is True

    def test_container_env_override_via_env_var(self):
        """OUROBOROS_CONTAINER=1 triggers container detection on any platform."""
        from ouroboros.platform_layer import is_container_env
        import os
        with patch.dict(os.environ, {"OUROBOROS_CONTAINER": "1"}, clear=False):
            result = is_container_env()
        assert result is True

    def test_no_container_env_returns_false(self):
        """Non-container environment (no /.dockerenv, no env var) returns False."""
        from ouroboros.platform_layer import is_container_env
        import ouroboros.platform_layer as pl
        import os
        env = {k: v for k, v in os.environ.items() if k != "OUROBOROS_CONTAINER"}
        with patch.dict(os.environ, env, clear=True), \
             patch.object(pl, "IS_LINUX", True), \
             patch("ouroboros.platform_layer.pathlib.Path.exists", return_value=False):
            result = is_container_env()
        assert result is False

    def test_is_loopback_host_handles_bracketed_ipv6(self):
        """is_loopback_host must recognize bracketed IPv6 loopback [::1]."""
        from ouroboros.server_auth import is_loopback_host
        assert is_loopback_host("[::1]") is True, "[::1] should be loopback"
        assert is_loopback_host("[::2]") is False, "[::2] should not be loopback"


class TestApiSettingsGetMeta:
    """Server-level test: api_settings_get injects _meta into response."""

    def test_api_settings_get_includes_meta_key(self):
        """_build_network_meta is wired into api_settings_get response."""
        # Test the key injection by checking the helper returns required fields
        from server import _build_network_meta
        with patch("server._get_lan_ip", return_value="192.168.1.1"):
            meta = _build_network_meta("0.0.0.0", 8765)
        # These are the keys api_settings_get injects as safe["_meta"]
        required_keys = {"bind_host", "bind_port", "lan_ip", "reachability", "recommended_url", "warning"}
        assert required_keys.issubset(set(meta.keys()))

    def test_loopback_meta_shape(self):
        from server import _build_network_meta
        meta = _build_network_meta("127.0.0.1", 8765)
        assert meta["reachability"] == "loopback_only"
        assert meta["recommended_url"] == ""
        assert meta["lan_ip"] == ""

    def test_lan_reachable_meta_shape(self):
        from server import _build_network_meta
        with patch("server._get_lan_ip", return_value="10.0.0.2"):
            meta = _build_network_meta("0.0.0.0", 9000)
        assert meta["reachability"] == "lan_reachable"
        assert "10.0.0.2" in meta["recommended_url"]
        assert "9000" in meta["recommended_url"]

    def test_host_ip_unknown_meta_shape(self):
        from server import _build_network_meta
        with patch("server.is_container_env", return_value=True):
            meta = _build_network_meta("0.0.0.0", 8765)
        assert meta["reachability"] == "host_ip_unknown"
        assert "your-host-ip" in meta["recommended_url"]


class TestApiSettingsGetRoute:
    """Starlette TestClient: /api/settings GET actually injects _meta."""

    def test_settings_route_includes_meta_key(self):
        """api_settings_get injects safe['_meta'] — regression guard for the injection line."""
        import json
        import server as srv
        from starlette.testclient import TestClient

        # Patch away the heavy startup side-effects
        with patch.object(srv, "_build_network_meta", return_value={
            "bind_host": "127.0.0.1",
            "bind_port": 8765,
            "lan_ip": "",
            "reachability": "loopback_only",
            "recommended_url": "",
            "warning": "bound to localhost",
        }), patch.object(srv, "load_settings", return_value={}), \
             patch.object(srv, "apply_runtime_provider_defaults", return_value=({}, False, [])), \
             patch("ouroboros.server_auth.get_configured_network_password", return_value=""), \
             patch.object(srv, "PORT_FILE") as mock_port_file:
            mock_port_file.exists.return_value = False
            client = TestClient(srv.app)
            resp = client.get("/api/settings")

        assert resp.status_code == 200
        data = resp.json()
        assert "_meta" in data, "_meta key missing from /api/settings response"
        assert data["_meta"]["reachability"] == "loopback_only"

    def test_settings_route_meta_all_fields_present(self):
        """_meta in /api/settings response contains all six required fields."""
        import server as srv
        from starlette.testclient import TestClient

        expected_meta = {
            "bind_host": "0.0.0.0",
            "bind_port": 9000,
            "lan_ip": "192.168.5.5",
            "reachability": "lan_reachable",
            "recommended_url": "http://192.168.5.5:9000",
            "warning": "",
        }
        with patch.object(srv, "_build_network_meta", return_value=expected_meta), \
             patch.object(srv, "load_settings", return_value={}), \
             patch.object(srv, "apply_runtime_provider_defaults", return_value=({}, False, [])), \
             patch("ouroboros.server_auth.get_configured_network_password", return_value=""), \
             patch.object(srv, "PORT_FILE") as mock_port_file:
            mock_port_file.exists.return_value = False
            client = TestClient(srv.app)
            resp = client.get("/api/settings")

        assert resp.status_code == 200
        meta = resp.json()["_meta"]
        for key in ("bind_host", "bind_port", "lan_ip", "reachability", "recommended_url", "warning"):
            assert key in meta, f"_meta missing key: {key}"

    def test_settings_route_forwards_bind_host(self):
        """api_settings_get passes _BIND_HOST into _build_network_meta."""
        import server as srv
        from starlette.testclient import TestClient

        captured = {}

        def fake_build_meta(bind_host, bind_port):
            captured["bind_host"] = bind_host
            return {
                "bind_host": bind_host,
                "bind_port": bind_port,
                "lan_ip": "10.0.0.1",
                "reachability": "lan_reachable",
                "recommended_url": f"http://10.0.0.1:{bind_port}",
                "warning": "",
            }

        with patch.object(srv, "_build_network_meta", side_effect=fake_build_meta), \
             patch.object(srv, "load_settings", return_value={}), \
             patch.object(srv, "apply_runtime_provider_defaults", return_value=({}, False, [])), \
             patch("ouroboros.server_auth.get_configured_network_password", return_value=""), \
             patch.object(srv, "_BIND_HOST", "0.0.0.0"), \
             patch.object(srv, "PORT_FILE") as mock_port_file:
            mock_port_file.exists.return_value = False
            client = TestClient(srv.app)
            resp = client.get("/api/settings")

        assert resp.status_code == 200
        assert captured.get("bind_host") == "0.0.0.0", (
            f"Expected bind_host='0.0.0.0' from _BIND_HOST, got {captured.get('bind_host')}"
        )
        meta = resp.json()["_meta"]
        assert "bind_host" in meta

    def test_settings_route_uses_port_file_when_present(self):
        """api_settings_get reads live port from PORT_FILE when it exists."""
        import server as srv
        from starlette.testclient import TestClient

        captured = {}

        def fake_build_meta(bind_host, bind_port):
            captured["bind_port"] = bind_port
            return {
                "bind_host": bind_host,
                "bind_port": bind_port,
                "lan_ip": "",
                "reachability": "loopback_only",
                "recommended_url": "",
                "warning": "",
            }

        with patch.object(srv, "_build_network_meta", side_effect=fake_build_meta), \
             patch.object(srv, "load_settings", return_value={}), \
             patch.object(srv, "apply_runtime_provider_defaults", return_value=({}, False, [])), \
             patch("ouroboros.server_auth.get_configured_network_password", return_value=""), \
             patch.object(srv, "PORT_FILE") as mock_port_file:
            mock_port_file.exists.return_value = True
            mock_port_file.read_text.return_value = "9999"
            client = TestClient(srv.app)
            resp = client.get("/api/settings")

        assert resp.status_code == 200
        assert captured.get("bind_port") == 9999, (
            f"Expected bind_port=9999 from PORT_FILE, got {captured.get('bind_port')}"
        )


class TestRenderNetworkHintJSContract:
    """JS contract tests for _renderNetworkHint in web/modules/settings.js.

    This repo has no jsdom/Node test harness (see test_chat_js_contracts.py
    for the established pattern). Tests parse the actual function body to
    verify branch-specific assignments — stronger than whole-file grep.
    """

    SETTINGS_JS = __import__("pathlib").Path(__file__).parent.parent / "web" / "modules" / "settings.js"

    def _src(self):
        return self.SETTINGS_JS.read_text(encoding="utf-8")

    def _fn_body(self) -> str:
        """Extract the body of _renderNetworkHint from settings.js."""
        import re
        src = self._src()
        # Match from function declaration to its closing brace (same indent as open)
        m = re.search(
            r"function _renderNetworkHint\(meta\)\s*\{(.+?)\n    \}",
            src, re.DOTALL
        )
        if not m:
            raise AssertionError("Could not find _renderNetworkHint function body in settings.js")
        return m.group(1)

    def _branch_body(self, reachability: str) -> str:
        """Extract the if/else-if block that handles a specific reachability value."""
        fn = self._fn_body()
        # Find the conditional that checks for the reachability string
        idx = fn.find(f"'{reachability}'")
        if idx == -1:
            raise AssertionError(f"Reachability '{reachability}' not found in _renderNetworkHint body")
        # Take the next 600 chars as the block body (generous window for the branch)
        return fn[idx: idx + 600]

    def test_render_function_exists(self):
        assert "_renderNetworkHint" in self._src()

    def test_function_body_extractable(self):
        """Verify the regex can extract the function body reliably."""
        body = self._fn_body()
        assert "hint" in body
        assert "meta" in body

    def test_loopback_branch_sets_info_tone(self):
        """loopback_only branch must assign dataset.tone = 'info'."""
        block = self._branch_body("loopback_only")
        assert "dataset.tone" in block, "loopback_only branch must set hint.dataset.tone"
        assert "'info'" in block, "loopback_only branch must set tone to 'info'"

    def test_loopback_branch_shows_hint(self):
        """loopback_only branch must set hint.hidden = false."""
        block = self._branch_body("loopback_only")
        assert "hidden = false" in block or "hidden=false" in block, (
            "loopback_only branch must show the hint (hint.hidden = false)"
        )

    def test_lan_reachable_branch_sets_ok_tone(self):
        """lan_reachable branch must assign dataset.tone = 'ok'."""
        block = self._branch_body("lan_reachable")
        assert "dataset.tone" in block, "lan_reachable branch must set hint.dataset.tone"
        assert "'ok'" in block, "lan_reachable branch must set tone to 'ok'"

    def test_lan_reachable_branch_shows_clickable_url(self):
        """lan_reachable branch must include the recommended_url in rendered HTML."""
        block = self._branch_body("lan_reachable")
        assert "recommended_url" in block, (
            "lan_reachable branch must reference meta.recommended_url in rendered HTML"
        )
        assert "hidden = false" in block or "hidden=false" in block

    def test_host_ip_unknown_branch_sets_warn_tone(self):
        """host_ip_unknown branch must assign dataset.tone = 'warn'."""
        block = self._branch_body("host_ip_unknown")
        assert "dataset.tone" in block, "host_ip_unknown branch must set hint.dataset.tone"
        assert "'warn'" in block, "host_ip_unknown branch must set tone to 'warn'"

    def test_fallback_branch_hides_hint(self):
        """The else/fallback branch must set hint.hidden = true."""
        fn = self._fn_body()
        assert "hidden = true" in fn or "hidden=true" in fn, (
            "_renderNetworkHint fallback must hide the hint (hint.hidden = true)"
        )

    def test_no_inline_style_display_in_function(self):
        """_renderNetworkHint must not use style.display (use hidden attribute per design system)."""
        fn = self._fn_body()
        assert "style.display" not in fn, (
            "_renderNetworkHint must use hint.hidden, not style.display"
        )
