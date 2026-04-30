from __future__ import annotations

import pathlib

from ouroboros.tool_aliases import adapt_tool_args, canonical_tool_name
from ouroboros.tool_policy import is_initial_task_tool
from ouroboros.tools.registry import ToolRegistry


def _registry(tmp_path: pathlib.Path) -> ToolRegistry:
    repo = tmp_path / "repo"
    data = tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    return ToolRegistry(repo, data)


def test_openclaw_aliases_resolve_to_canonical_tools():
    assert canonical_tool_name("web_fetch") == "browse_page"
    assert canonical_tool_name("exec") == "run_shell"
    assert canonical_tool_name("message") == "send_user_message"
    assert canonical_tool_name("read_file") == "repo_read"
    assert canonical_tool_name("write_file") == "repo_write"
    assert canonical_tool_name("edit") == "str_replace_editor"


def test_openclaw_alias_args_adapt_to_canonical_shapes():
    assert adapt_tool_args("web_fetch", {"url": "https://example.com"})["output"] == "text"
    assert adapt_tool_args("exec", {"command": "python -m pytest"})["cmd"] == "python -m pytest"
    assert adapt_tool_args("message", {"content": "hello"})["text"] == "hello"
    assert adapt_tool_args("read_file", {"file_path": "README.md"})["path"] == "README.md"
    assert adapt_tool_args("edit", {"old_string": "a", "new_string": "b"}) == {
        "old_str": "a",
        "new_str": "b",
    }


def test_alias_schemas_are_exposed_and_lookupable(tmp_path):
    registry = _registry(tmp_path)
    names = {
        schema["function"]["name"]
        for schema in registry.schemas()
    }
    assert "web_fetch" in names
    assert "exec" in names
    assert registry.get_schema_by_name("web_fetch")["function"]["name"] == "web_fetch"
    assert registry.get_schema_by_name("web_fetch")["function"]["parameters"]["required"] == ["url"]


def test_core_aliases_are_initial_task_tools():
    assert is_initial_task_tool("web_fetch")
    assert is_initial_task_tool("exec")
    assert is_initial_task_tool("read_file")
