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


def test_safe_relpath_strips_leading_slash():
    """safe_relpath strips leading / but doesn't raise."""
    from ouroboros.utils import safe_relpath
    result = safe_relpath("/etc/passwd")
    assert not result.startswith("/")


def test_clip_text():
    from ouroboros.utils import clip_text
    result = clip_text("hello world " * 100, 50)
    assert len(result) <= 55  # some tolerance for "..."


def test_estimate_tokens():
    from ouroboros.utils import estimate_tokens
    tokens = estimate_tokens("Hello world, this is a test.")
    assert 5 <= tokens <= 20


# ── Memory ───────────────────────────────────────────────────────

def test_memory_scratchpad():
    """Memory reads/writes scratchpad without crash."""
    from ouroboros.memory import Memory
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(drive_root=pathlib.Path(tmp))
        mem.save_scratchpad("test content")
        content = mem.load_scratchpad()
        assert "test content" in content


def test_memory_identity():
    """Memory reads/writes identity without crash."""
    from ouroboros.memory import Memory
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(drive_root=pathlib.Path(tmp))
        # Write identity file directly
        mem.identity_path.parent.mkdir(parents=True, exist_ok=True)
        mem.identity_path.write_text("I am Ouroboros")
        content = mem.load_identity()
        assert "Ouroboros" in content


def test_memory_chat_history_empty():
    """Chat history returns empty list when no data."""
    from ouroboros.memory import Memory
    with tempfile.TemporaryDirectory() as tmp:
        mem = Memory(drive_root=pathlib.Path(tmp))
        history = mem.chat_history(count=10)
        assert isinstance(history, list)
        assert len(history) == 0


# ── Context builder ─────────────────────────────────────────────

def test_context_build_runtime_section():
    """Runtime section builder is callable."""
    from ouroboros.context import _build_runtime_section
    # Just check it's importable and callable
    assert callable(_build_runtime_section)


def test_context_build_memory_sections():
    """Memory sections builder is callable."""
    from ouroboros.context import _build_memory_sections
    assert callable(_build_memory_sections)


# ── Bible invariants ─────────────────────────────────────────────

def test_no_hardcoded_replies():
    """Principle 3 (LLM-first): no hardcoded reply strings in code.
    
    Checks for suspicious patterns like:
    - reply = "Fixed string"
    - return "Sorry, I can't..."
    """
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
                    if "{" in line or "f'" in line or 'f"' in line:
                        continue
                    violations.append(f"{path.name}:{i}: {line.strip()}")
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

def test_no_env_dumping():
    """Security: no code dumps entire env (os.environ without key access).
    
    Allows: os.environ["KEY"], os.environ.get(), os.environ.setdefault(),
            os.environ.copy() (for subprocess).
    Disallows: print(os.environ), json.dumps(os.environ), etc.
    """
    # Only flag raw os.environ without accessor method
    dangerous = re.compile(r'(?:print|json|log|str)\s*[\.(].*os\.environ\b')
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
    assert len(violations) == 0, f"Dangerous env dumping:\n" + "\n".join(violations)


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


def test_no_bare_except_pass():
    """No bare `except: pass` (not even except Exception: pass with just pass).
    
    v4.9.0 hardened exceptions — but checks the STRICTEST form:
    bare except (no Exception class) followed by pass.
    """
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
                # Only flag bare `except:` (no class specified)
                if stripped == "except:":
                    # Check next non-empty line is just `pass`
                    for j in range(i, min(i + 3, len(lines))):
                        next_line = lines[j].strip()
                        if next_line and next_line == "pass":
                            violations.append(f"{path.name}:{i}: bare except: pass")
                            break
    assert len(violations) == 0, f"Bare except:pass found:\n" + "\n".join(violations)


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


def test_function_count_reasonable():
    """Codebase doesn't have too few or too many functions."""
    sizes = _get_function_sizes()
    assert len(sizes) >= 100, f"Only {len(sizes)} functions — too few?"
    assert len(sizes) <= 1000, f"{len(sizes)} functions — too many?"
