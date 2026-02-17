"""Smoke test suite for Ouroboros.

Tests core invariants:
- All modules import cleanly
- Tool registry discovers all 33 tools
- Utility functions work correctly
- Memory operations don't crash
- Context builder produces valid structure
- Bible invariants hold (no hardcoded replies, version sync)

Run: python -m pytest tests/test_smoke.py -v
"""
import ast
import os
import pathlib
import re
import sys
import tempfile

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent

# ── Module imports ───────────────────────────────────────────────

CORE_MODULES = [
    "ouroboros.agent",
    "ouroboros.context",
    "ouroboros.loop",
    "ouroboros.llm",
    "ouroboros.memory",
    "ouroboros.review",
    "ouroboros.utils",
    "ouroboros.consciousness",
]

TOOL_MODULES = [
    "ouroboros.tools.registry",
    "ouroboros.tools.core",
    "ouroboros.tools.git",
    "ouroboros.tools.shell",
    "ouroboros.tools.search",
    "ouroboros.tools.control",
    "ouroboros.tools.browser",
    "ouroboros.tools.review",
]

SUPERVISOR_MODULES = [
    "supervisor.state",
    "supervisor.telegram",
    "supervisor.queue",
    "supervisor.workers",
    "supervisor.git_ops",
    "supervisor.events",
]


@pytest.mark.parametrize("module", CORE_MODULES + TOOL_MODULES + SUPERVISOR_MODULES)
def test_import(module):
    """Every module imports without error."""
    __import__(module)


# ── Tool registry ────────────────────────────────────────────────

@pytest.fixture
def registry():
    from ouroboros.tools.registry import ToolRegistry
    tmp = pathlib.Path(tempfile.mkdtemp())
    return ToolRegistry(repo_dir=tmp, drive_root=tmp)


def test_tool_count(registry):
    """All expected tools are discovered."""
    schemas = registry.schemas()
    assert len(schemas) >= 33, f"Expected ≥33 tools, got {len(schemas)}"


EXPECTED_TOOLS = [
    "repo_read", "repo_write_commit", "repo_list", "repo_commit_push",
    "drive_read", "drive_write", "drive_list",
    "git_status", "git_diff",
    "run_shell", "claude_code_edit",
    "browse_page", "browser_action",
    "web_search",
    "chat_history", "update_scratchpad", "update_identity",
    "request_restart", "promote_to_stable", "request_review",
    "schedule_task", "cancel_task",
    "switch_model", "toggle_evolution", "toggle_consciousness",
    "send_owner_message", "send_photo",
    "codebase_digest", "codebase_health",
    "knowledge_read", "knowledge_write", "knowledge_list",
    "multi_model_review",
]


@pytest.mark.parametrize("tool_name", EXPECTED_TOOLS)
def test_tool_registered(registry, tool_name):
    """Each expected tool is in the registry."""
    available = [t["function"]["name"] for t in registry.schemas()]
    assert tool_name in available, f"{tool_name} not in registry"


def test_unknown_tool_returns_warning(registry):
    """Calling unknown tool returns warning, not exception."""
    result = registry.execute("__nonexistent__", {})
    assert "Unknown tool" in result or "⚠️" in result


def test_tool_schemas_valid(registry):
    """All tool schemas have required OpenAI fields."""
    for schema in registry.schemas():
        assert schema["type"] == "function"
        func = schema["function"]
        assert "name" in func
        assert "description" in func
        assert "parameters" in func
        params = func["parameters"]
        assert params["type"] == "object"
        assert "properties" in params


# ── Utilities ────────────────────────────────────────────────────

def test_safe_relpath_normal():
    from ouroboros.utils import safe_relpath
    result = safe_relpath("foo/bar.py")
    assert result == "foo/bar.py"


def test_safe_relpath_rejects_traversal():
    from ouroboros.utils import safe_relpath
    with pytest.raises(ValueError):
        safe_relpath("../../../etc/passwd")


def test_safe_relpath_rejects_absolute():
    from ouroboros.utils import safe_relpath
    with pytest.raises(ValueError):
        safe_relpath("/etc/passwd")


def test_truncate_exists():
    from ouroboros.utils import truncate
    result = truncate("hello world", 5)
    assert len(result) <= 10  # truncate may add "..."


# ── Memory ───────────────────────────────────────────────────────

def test_memory_scratchpad():
    """Memory reads/writes scratchpad without crash."""
    from ouroboros.memory import Memory
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(drive_root=pathlib.Path(tmp))
        # Write
        mem.update_scratchpad("test content")
        # Read
        content = mem.read_scratchpad()
        assert "test content" in content


def test_memory_identity():
    """Memory reads/writes identity without crash."""
    from ouroboros.memory import Memory
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(drive_root=pathlib.Path(tmp))
        mem.update_identity("I am Ouroboros")
        content = mem.read_identity()
        assert "Ouroboros" in content


def test_memory_chat_history_empty():
    """Chat history returns empty list when no data."""
    from ouroboros.memory import Memory
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(drive_root=pathlib.Path(tmp))
        history = mem.recent_chat(count=10)
        assert isinstance(history, list)
        assert len(history) == 0


# ── Context builder ─────────────────────────────────────────────

def test_context_static_blocks():
    """Static content blocks can be built."""
    from ouroboros.context import _build_static_content
    content = _build_static_content()
    assert "BIBLE" in content or "Конституция" in content
    assert "SYSTEM" in content or "Уроборос" in content


# ── Bible invariants ─────────────────────────────────────────────

def test_no_hardcoded_replies():
    """Principle 3 (LLM-first): no hardcoded reply strings in code.
    
    Checks that code doesn't contain patterns like:
    - reply = "Fixed string"
    - return "Sorry, I can't..."
    - send_message("hardcoded response")
    """
    # Pattern: assignment to reply/response variable with string literal
    suspicious = re.compile(
        r'(reply|response)\s*=\s*["\'](?!$|{|\s*$)',
        re.IGNORECASE,
    )
    violations = []
    for root, dirs, files in os.walk(REPO / "ouroboros"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = pathlib.Path(root) / f
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if suspicious.search(line):
                    # Allow format strings and empty strings
                    if "{" in line or "f'" in line or 'f"' in line:
                        continue
                    violations.append(f"{path.name}:{i}: {line.strip()}")
    # Allow some known-OK patterns (error messages, tool results)
    assert len(violations) < 5, f"Possible hardcoded replies:\n" + "\n".join(violations)


def test_version_file_exists():
    """VERSION file exists and contains valid semver."""
    version = (REPO / "VERSION").read_text().strip()
    parts = version.split(".")
    assert len(parts) == 3, f"VERSION '{version}' is not semver"
    for p in parts:
        assert p.isdigit(), f"VERSION part '{p}' is not numeric"


def test_version_in_readme():
    """VERSION matches what README claims."""
    version = (REPO / "VERSION").read_text().strip()
    readme = (REPO / "README.md").read_text()
    assert version in readme, f"VERSION {version} not found in README.md"


def test_bible_exists_and_has_principles():
    """BIBLE.md exists and contains all 9 principles (0-8)."""
    bible = (REPO / "BIBLE.md").read_text()
    for i in range(9):
        assert f"Принцип {i}" in bible, f"Principle {i} missing from BIBLE.md"


# ── Code quality invariants ──────────────────────────────────────

def test_no_env_printing():
    """Security: no code prints or logs full environment variables."""
    dangerous = re.compile(r'os\.environ(?!\[|\.get|\.pop)')
    violations = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'tests')]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = pathlib.Path(root) / f
            for i, line in enumerate(path.read_text().splitlines(), 1):
                if line.strip().startswith("#"):
                    continue
                if dangerous.search(line):
                    violations.append(f"{path.name}:{i}: {line.strip()[:80]}")
    assert len(violations) == 0, f"Dangerous env access:\n" + "\n".join(violations)


def test_no_oversized_modules():
    """Principle 5: no module exceeds 1000 lines."""
    max_lines = 1000
    violations = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'tests')]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = pathlib.Path(root) / f
            lines = len(path.read_text().splitlines())
            if lines > max_lines:
                violations.append(f"{path.name}: {lines} lines")
    assert len(violations) == 0, f"Oversized modules (>{max_lines} lines):\n" + "\n".join(violations)


def test_no_silent_exceptions():
    """v4.9.0 guarantee: no bare `except: pass` or `except Exception: pass`."""
    pattern = re.compile(r'except\s+(Exception\s*)?:\s*$')
    violations = []
    for root, dirs, files in os.walk(REPO / "ouroboros"):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = pathlib.Path(root) / f
            lines = path.read_text().splitlines()
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if pattern.match(stripped):
                    # Check next non-empty line is `pass` or `continue`
                    for j in range(i, min(i + 3, len(lines))):
                        next_line = lines[j].strip()
                        if next_line and next_line in ("pass", "continue"):
                            violations.append(f"{path.name}:{i}: {stripped}")
                            break
    assert len(violations) == 0, f"Silent exceptions found:\n" + "\n".join(violations)


# ── AST-based function size check ───────────────────────────────

MAX_FUNCTION_LINES = 200  # Hard limit — anything above is a bug


def _get_function_sizes():
    """Return list of (file, func_name, lines) for all functions."""
    results = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'tests')]
        for f in files:
            if not f.endswith(".py"):
                continue
            path = pathlib.Path(root) / f
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    size = node.end_lineno - node.lineno + 1
                    results.append((f, node.name, size))
    return results


def test_no_extremely_oversized_functions():
    """No function exceeds 200 lines (hard limit)."""
    violations = []
    for fname, func_name, size in _get_function_sizes():
        if size > MAX_FUNCTION_LINES:
            violations.append(f"{fname}:{func_name} = {size} lines")
    assert len(violations) == 0, \
        f"Functions exceeding {MAX_FUNCTION_LINES} lines:\n" + "\n".join(violations)
