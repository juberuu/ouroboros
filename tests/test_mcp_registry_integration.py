"""ToolRegistry surface tests for MCP-discovered tools.

The registry must expose MCP tools through the same surface used by the
agent loop: ``schemas()``, ``tool_policy.list_non_core_tools()``, ``get_schema_by_name()``,
``get_timeout()``, and ``execute()``. These tests exercise that
contract via the manager's injectable fake transport so no real MCP
server is required.
"""

from __future__ import annotations

import pytest

from ouroboros import mcp_client
from ouroboros.contracts.task_constraint import TaskConstraint
from ouroboros.tool_policy import list_non_core_tools
from ouroboros.tools.registry import ToolRegistry


@pytest.fixture(autouse=True)
def _isolate_manager():
    mcp_client.reset_manager_for_tests()
    yield
    mcp_client.reset_manager_for_tests()


def _settings(*servers, enabled: bool = True, timeout: int = 60) -> dict:
    return {
        "MCP_ENABLED": enabled,
        "MCP_TOOL_TIMEOUT_SEC": timeout,
        "MCP_SERVERS": list(servers),
    }


def _good_server(**overrides) -> dict:
    base = {
        "id": "demo",
        "name": "Demo",
        "enabled": True,
        "transport": "streamable_http",
        "url": "https://e.example/mcp",
        "auth_header": "Authorization",
        "auth_token": "",
        "allowed_tools": [],
    }
    base.update(overrides)
    return base


class _FakeTransport:
    def __init__(self, response):
        self.response = response
        self.list_calls = []
        self.call_calls = []

    async def list_tools(self, cfg, timeout):
        self.list_calls.append((cfg.id, timeout))
        return list(self.response)

    async def call_tool(self, cfg, name, arguments, timeout):
        self.call_calls.append((cfg.id, name, dict(arguments or {}), timeout))
        return f"echo({cfg.id}/{name})"


def _wire_singleton(transport):
    mgr = mcp_client.get_manager()
    mgr._async_list_tools = transport.list_tools
    mgr._async_call_tool = transport.call_tool


@pytest.fixture
def registry(tmp_path):
    return ToolRegistry(repo_dir=tmp_path, drive_root=tmp_path)


def test_schemas_include_mcp_tools(registry):
    fake = _FakeTransport(
        [
            {"name": "ping", "description": "Ping", "input_schema": {"type": "object", "properties": {}}},
            {"name": "echo", "description": "Echo", "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        ]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")

    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert "mcp_svc__ping" in names
    assert "mcp_svc__echo" in names


def test_schemas_cold_worker_loads_settings_and_refreshes_once(registry, monkeypatch):
    fake = _FakeTransport(
        [{"name": "ping", "description": "Ping", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)

    import ouroboros.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "load_settings",
        lambda: _settings(_good_server(id="svc")),
    )

    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert "mcp_svc__ping" in names
    assert len(fake.list_calls) == 1
    registry.schemas()
    assert len(fake.list_calls) == 1


def test_list_non_core_tools_includes_mcp(registry):
    fake = _FakeTransport(
        [{"name": "ping", "description": "Ping", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")

    entries = list_non_core_tools(registry)
    names = [item["name"] for item in entries]
    assert "mcp_svc__ping" in names
    desc = next(item["description"] for item in entries if item["name"] == "mcp_svc__ping")
    assert "untrusted data" in desc


def test_get_schema_by_name_returns_mcp_tool(registry):
    fake = _FakeTransport(
        [
            {"name": "ping", "description": "Ping", "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}}},
        ]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")
    schema = registry.get_schema_by_name("mcp_svc__ping")
    assert schema is not None
    assert schema["function"]["name"] == "mcp_svc__ping"
    assert schema["function"]["parameters"]["properties"].get("q", {}).get("type") == "string"


def test_get_timeout_uses_mcp_tool_timeout(registry):
    fake = _FakeTransport(
        [{"name": "slow", "description": "", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc"), timeout=42))
    mcp_client.get_manager().refresh_server("svc")
    timeout = registry.get_timeout("mcp_svc__slow")
    # ``get_timeout`` adds a small grace on top of the configured value to
    # let the inner ``asyncio.wait_for`` finish before the outer executor
    # cancels it.
    assert 42 < timeout <= 42 + 5


def test_execute_dispatches_mcp_tool(registry, monkeypatch):
    fake = _FakeTransport(
        [{"name": "echo", "description": "Echo back", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")

    # Bypass safety LLM call: the registry path runs check_safety; replace
    # it with a passthrough so we don't try to spin up an actual
    # provider.
    import ouroboros.safety as safety_mod

    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **kw: (True, ""))
    # Also patch the import-cached reference inside the registry module
    # (the registry imports it lazily inside execute(), so the monkeypatch
    # above is sufficient).

    out = registry.execute("mcp_svc__echo", {"hello": "world"})
    assert "echo(svc/echo)" in out
    assert fake.call_calls and fake.call_calls[0][0] == "svc"


def test_execute_blocks_mcp_when_safety_fails(registry, monkeypatch):
    fake = _FakeTransport(
        [{"name": "echo", "description": "Echo back", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")

    import ouroboros.safety as safety_mod

    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **kw: (False, "blocked"))
    out = registry.execute("mcp_svc__echo", {"hello": "world"})
    assert out == "blocked"
    assert fake.call_calls == []


def test_execute_blocks_mcp_in_skill_repair_context(registry, monkeypatch):
    fake = _FakeTransport(
        [{"name": "echo", "description": "Echo back", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")
    registry._ctx.task_constraint = TaskConstraint(
        mode="skill_repair",
        skill_name="demo",
        payload_root="skills/external/demo",
    )

    import ouroboros.safety as safety_mod

    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **kw: (True, ""))
    out = registry.execute("mcp_svc__echo", {"hello": "world"})
    assert "HEAL_MODE_BLOCKED" in out
    assert "MCP tools" in out
    assert fake.call_calls == []


def test_execute_unknown_mcp_returns_not_found(registry, monkeypatch):
    fake = _FakeTransport([])
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")
    import ouroboros.safety as safety_mod
    monkeypatch.setattr(safety_mod, "check_safety", lambda *a, **kw: (True, ""))
    out = registry.execute("mcp_svc__missing", {})
    assert "MCP_TOOL_NOT_FOUND" in out


def test_disabled_manager_hides_tools(registry):
    fake = _FakeTransport(
        [{"name": "ping", "description": "", "input_schema": {"type": "object", "properties": {}}}]
    )
    _wire_singleton(fake)
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc")))
    mcp_client.get_manager().refresh_server("svc")
    # Disable the global flag and reconfigure: schemas should drop the MCP
    # tools immediately.
    mcp_client.reconfigure_from_settings(_settings(_good_server(id="svc"), enabled=False))
    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert "mcp_svc__ping" not in names
