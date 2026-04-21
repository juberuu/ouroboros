"""Tests for tool capability SSOT and no-drift invariants.

Verifies:
- tool_capabilities.py is the single source of truth
- tool_policy.py imports from capabilities (no local copy)
- loop_tool_execution.py imports from capabilities (no local copy)
- code_search is classified correctly
- run_shell rejects string cmd
- code_search tool works
"""
import inspect
import os
import pathlib
import re
import tempfile

import pytest

# ---------------------------------------------------------------------------
# SSOT drift tests
# ---------------------------------------------------------------------------


def test_tool_policy_imports_from_capabilities():
    """tool_policy.py must import CORE_TOOL_NAMES from tool_capabilities, not define its own."""
    import ouroboros.tool_policy as tp
    source = inspect.getsource(tp)
    assert "from ouroboros.tool_capabilities import" in source
    # Must NOT define its own frozenset of core tools
    assert "CORE_TOOL_NAMES" not in source.split("from ouroboros.tool_capabilities")[0]


def test_loop_execution_imports_from_capabilities():
    """loop_tool_execution.py must import sets from tool_capabilities."""
    import ouroboros.loop_tool_execution as lte
    source = inspect.getsource(lte)
    assert "from ouroboros.tool_capabilities import" in source
    # Must NOT have local frozenset definitions for these sets
    for name in ("READ_ONLY_PARALLEL_TOOLS", "STATEFUL_BROWSER_TOOLS",
                 "_UNTRUNCATED_TOOL_RESULTS", "_UNTRUNCATED_REPO_READ_PATHS"):
        # Check there's no local `X = frozenset({` pattern
        pattern = rf'^{re.escape(name)}\s*[:=]\s*frozenset'
        assert not re.search(pattern, source, re.MULTILINE), (
            f"{name} is locally defined in loop_tool_execution.py — should import from tool_capabilities"
        )


def test_capabilities_sets_are_frozensets():
    """All exported sets must be frozensets (immutable)."""
    from ouroboros.tool_capabilities import (
        CORE_TOOL_NAMES, META_TOOL_NAMES, READ_ONLY_PARALLEL_TOOLS,
        STATEFUL_BROWSER_TOOLS, UNTRUNCATED_TOOL_RESULTS,
        UNTRUNCATED_REPO_READ_PATHS,
    )
    for name, obj in [
        ("CORE_TOOL_NAMES", CORE_TOOL_NAMES),
        ("META_TOOL_NAMES", META_TOOL_NAMES),
        ("READ_ONLY_PARALLEL_TOOLS", READ_ONLY_PARALLEL_TOOLS),
        ("STATEFUL_BROWSER_TOOLS", STATEFUL_BROWSER_TOOLS),
        ("UNTRUNCATED_TOOL_RESULTS", UNTRUNCATED_TOOL_RESULTS),
        ("UNTRUNCATED_REPO_READ_PATHS", UNTRUNCATED_REPO_READ_PATHS),
    ]:
        assert isinstance(obj, frozenset), f"{name} must be a frozenset"


def test_policy_and_capabilities_core_names_identical():
    """The CORE_TOOL_NAMES used by tool_policy must be the exact same object."""
    from ouroboros.tool_policy import CORE_TOOL_NAMES as policy_names
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES as cap_names
    assert policy_names is cap_names


def test_loop_execution_parallel_tools_from_capabilities():
    """READ_ONLY_PARALLEL_TOOLS in loop_tool_execution is from capabilities."""
    from ouroboros.loop_tool_execution import READ_ONLY_PARALLEL_TOOLS as loop_set
    from ouroboros.tool_capabilities import READ_ONLY_PARALLEL_TOOLS as cap_set
    assert loop_set is cap_set


# ---------------------------------------------------------------------------
# code_search classification tests
# ---------------------------------------------------------------------------


def test_code_search_in_core_tools():
    """code_search must be in CORE_TOOL_NAMES."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "code_search" in CORE_TOOL_NAMES


def test_code_search_is_parallel_safe():
    """code_search must be in READ_ONLY_PARALLEL_TOOLS."""
    from ouroboros.tool_capabilities import READ_ONLY_PARALLEL_TOOLS
    assert "code_search" in READ_ONLY_PARALLEL_TOOLS


def test_code_search_has_result_limit():
    """code_search must have an explicit result size limit."""
    from ouroboros.tool_capabilities import TOOL_RESULT_LIMITS
    assert "code_search" in TOOL_RESULT_LIMITS


# ---------------------------------------------------------------------------
# code_search tool behavior tests
# ---------------------------------------------------------------------------


def _make_ctx(tmp_path):
    from ouroboros.tools.registry import ToolContext
    from unittest.mock import MagicMock
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = tmp_path
    ctx.repo_path = lambda p: tmp_path / p
    return ctx


def _populate_repo(tmp_path):
    """Create a mini repo structure for search tests."""
    (tmp_path / "foo.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (tmp_path / "bar.py").write_text("import os\ndef hello_bar():\n    pass\n", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "baz.py").write_text("class MyClass:\n    hello = True\n", encoding="utf-8")
    # Binary-like file (should be skipped)
    (tmp_path / "data.png").write_bytes(b'\x89PNG\r\n\x1a\n' + b'\x00' * 100)
    # Cache dir (should be skipped)
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "foo.cpython-310.pyc").write_bytes(b'\x00' * 50)


def test_code_search_literal(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "hello")
    assert "foo.py:1:" in result
    assert "bar.py:2:" in result
    assert "sub/baz.py:2:" in result


def test_code_search_regex(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, r"def \w+\(\)", regex=True)
    assert "foo.py:1:" in result
    assert "bar.py:2:" in result


def test_code_search_scoped_path(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "hello", path="sub")
    assert "sub/baz.py" in result
    assert "foo.py" not in result


def test_code_search_include_filter(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    (tmp_path / "readme.md").write_text("hello from markdown\n", encoding="utf-8")
    result = _code_search(ctx, "hello", include="*.md")
    assert "readme.md" in result
    assert "foo.py" not in result


def test_code_search_no_matches(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "zzz_nonexistent_zzz")
    assert "No matches found" in result


def test_code_search_skips_binaries(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "PNG")
    # .png file should be skipped even though it contains "PNG" bytes
    assert "data.png" not in result


def test_code_search_skips_cache_dirs(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    _populate_repo(tmp_path)
    result = _code_search(ctx, "foo")
    assert "__pycache__" not in result


def test_code_search_max_results(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    # Create many matching lines
    lines = "\n".join(f"match_line_{i}" for i in range(50))
    (tmp_path / "many.py").write_text(lines, encoding="utf-8")
    result = _code_search(ctx, "match_line", max_results=10)
    assert "truncated at 10" in result


def test_code_search_empty_query(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    result = _code_search(ctx, "")
    assert "SEARCH_ERROR" in result


def test_code_search_invalid_regex(tmp_path):
    from ouroboros.tools.core import _code_search
    ctx = _make_ctx(tmp_path)
    result = _code_search(ctx, "[invalid", regex=True)
    assert "SEARCH_ERROR" in result


# ---------------------------------------------------------------------------
# run_shell string contract
# ---------------------------------------------------------------------------


def test_run_shell_string_cmd_is_hard_error(tmp_path):
    """run_shell recovers string cmd via cascade (shlex.split for plain strings)."""
    from ouroboros.tools.shell import _run_shell
    from unittest.mock import MagicMock, patch
    from subprocess import CompletedProcess
    from ouroboros.tools.registry import ToolContext
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = tmp_path
    ctx.drive_logs.return_value = tmp_path
    with patch("ouroboros.tools.shell._tracked_subprocess_run",
               return_value=CompletedProcess(["echo", "hello"], 0, "hello", "")), \
         patch("ouroboros.tools.shell.load_settings", return_value={}):
        result = _run_shell(ctx, "echo hello")
    assert "SHELL_ARG_ERROR" not in result
    assert "exit_code=0" in result


def test_run_shell_list_cmd_works(tmp_path):
    """run_shell with a list cmd should work normally."""
    from ouroboros.tools.shell import _run_shell
    from unittest.mock import MagicMock
    from ouroboros.tools.registry import ToolContext
    ctx = MagicMock(spec=ToolContext)
    ctx.repo_dir = tmp_path
    ctx.drive_logs.return_value = tmp_path
    result = _run_shell(ctx, ["echo", "hello"])
    assert "hello" in result


# ---------------------------------------------------------------------------
# Initial tool visibility
# ---------------------------------------------------------------------------


def test_code_search_in_initial_schemas():
    """code_search must appear in initial tool schemas."""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import initial_tool_schemas
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {s["function"]["name"] for s in initial_tool_schemas(registry)}
    assert "code_search" in names


def test_code_search_registered():
    """code_search must be registered in the tool registry."""
    from ouroboros.tools.registry import ToolRegistry
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    available = {t["function"]["name"] for t in registry.schemas()}
    assert "code_search" in available


# ---------------------------------------------------------------------------
# schedule_task non-core classification tests
# ---------------------------------------------------------------------------


def test_schedule_task_not_in_core():
    """schedule_task must NOT be in CORE_TOOL_NAMES (moved to non-core in v4.27.2)."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "schedule_task" not in CORE_TOOL_NAMES, (
        "schedule_task was moved to non-core to prevent reflexive delegation; "
        "use enable_tools('schedule_task') to activate"
    )


def test_wait_for_task_not_in_core():
    """wait_for_task must NOT be in CORE_TOOL_NAMES."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "wait_for_task" not in CORE_TOOL_NAMES


def test_get_task_result_not_in_core():
    """get_task_result must NOT be in CORE_TOOL_NAMES."""
    from ouroboros.tool_capabilities import CORE_TOOL_NAMES
    assert "get_task_result" not in CORE_TOOL_NAMES


def test_schedule_task_available_in_registry():
    """schedule_task must still be registered (available via enable_tools)."""
    from ouroboros.tools.registry import ToolRegistry
    import pathlib, tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    all_names = {t["function"]["name"] for t in registry.schemas()}
    assert "schedule_task" in all_names, (
        "schedule_task must be discoverable via list_available_tools / enable_tools"
    )


def test_schedule_task_not_in_initial_schemas():
    """schedule_task must NOT appear in initial tool schemas (non-core)."""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import initial_tool_schemas
    import pathlib, tempfile
    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    names = {s["function"]["name"] for s in initial_tool_schemas(registry)}
    assert "schedule_task" not in names, (
        "schedule_task should not be loaded by default; activate with enable_tools"
    )


# ---------------------------------------------------------------------------
# Discovery path drift test
# ---------------------------------------------------------------------------


def test_discovery_uses_ssot_not_registry_core_names():
    """tool_discovery.py must use SSOT (via tool_policy), not registry.CORE_TOOL_NAMES."""
    import ouroboros.tools.tool_discovery as td
    source = inspect.getsource(td)
    # Must import from tool_policy (SSOT-aware)
    assert "tool_policy" in source, (
        "tool_discovery.py must import from tool_policy for SSOT-aware non-core listing"
    )
    # Must NOT call _registry.list_non_core_tools() — that uses the registry's own set
    assert "_registry.list_non_core_tools()" not in source, (
        "tool_discovery.py must not call _registry.list_non_core_tools() — "
        "that uses registry.py's local CORE_TOOL_NAMES, not the SSOT"
    )


def test_discovery_path_consistent_with_policy():
    """list_available_tools must return the same non-core set as tool_policy.list_non_core_tools."""
    from ouroboros.tools.registry import ToolRegistry
    from ouroboros.tool_policy import list_non_core_tools as policy_non_core
    import ouroboros.tools.tool_discovery as td

    tmp = pathlib.Path(tempfile.mkdtemp())
    registry = ToolRegistry(repo_dir=tmp, drive_root=tmp)
    td.set_registry(registry)

    # Get what tool_policy says (SSOT)
    policy_names = {t["name"] for t in policy_non_core(registry)}
    # Remove meta-tools (discovery excludes them from its listing)
    policy_names -= {"list_available_tools", "enable_tools"}

    # Get what discovery tool shows
    from ouroboros.tools.registry import ToolContext
    ctx = ToolContext(repo_dir=tmp, drive_root=tmp)
    output = td._list_available_tools(ctx)

    if not policy_names:
        assert "All tools are already" in output
    else:
        for name in policy_names:
            assert name in output, (
                f"tool_policy says '{name}' is non-core but discovery doesn't show it"
            )
