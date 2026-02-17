"""
Smoke tests for Ouroboros.

These tests verify core invariants WITHOUT external dependencies
(no LLM calls, no Telegram, no git push). Safe to run anytime.

Run: python -m pytest tests/ -v
"""

import importlib
import json
import os
import pathlib
import sys
import tempfile

import pytest

# Ensure repo root is on path
REPO_ROOT = pathlib.Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ─── Module imports ────────────────────────────────────────────

class TestModuleImports:
    """Every module must import without side effects."""

    MODULES = [
        "ouroboros.utils",
        "ouroboros.memory",
        "ouroboros.review",
        "ouroboros.context",
        "ouroboros.loop",
        "ouroboros.llm",
        "ouroboros.agent",
        "ouroboros.consciousness",
        "ouroboros.apply_patch",
        "ouroboros.tools.registry",
        "ouroboros.tools.core",
        "ouroboros.tools.git",
        "ouroboros.tools.shell",
        "ouroboros.tools.search",
        "ouroboros.tools.control",
        "ouroboros.tools.browser",
        "ouroboros.tools.review",
    ]

    @pytest.mark.parametrize("module_name", MODULES)
    def test_import(self, module_name):
        """Module imports without error."""
        mod = importlib.import_module(module_name)
        assert mod is not None


# ─── Tool registry ─────────────────────────────────────────────

class TestToolRegistry:
    """Tool registry loads all tools and produces valid schemas."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        from ouroboros.tools.registry import ToolRegistry
        self.registry = ToolRegistry(
            repo_dir=tmp_path / "repo",
            drive_root=tmp_path / "drive",
        )

    def test_tool_count_minimum(self):
        """At least 30 tools should be registered."""
        tools = self.registry.available_tools()
        assert len(tools) >= 30, f"Only {len(tools)} tools: {tools}"

    def test_all_schemas_valid(self):
        """Every schema must have 'type' and 'function' with 'name'."""
        for schema in self.registry.schemas():
            assert schema["type"] == "function"
            fn = schema["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn
            # Parameters must be a valid JSON Schema object
            params = fn["parameters"]
            assert params.get("type") == "object"

    def test_known_tools_present(self):
        """Critical tools must be registered."""
        tools = set(self.registry.available_tools())
        critical = {
            "repo_read", "repo_write_commit", "repo_list",
            "drive_read", "drive_write", "drive_list",
            "run_shell", "claude_code_edit",
            "git_status", "git_diff", "repo_commit_push",
            "web_search", "browse_page", "browser_action",
            "request_restart", "promote_to_stable",
            "update_scratchpad", "update_identity",
            "chat_history", "schedule_task",
            "switch_model", "send_owner_message",
            "codebase_digest", "codebase_health",
            "multi_model_review",
            "toggle_evolution", "toggle_consciousness",
            "knowledge_read", "knowledge_write", "knowledge_list",
        }
        missing = critical - tools
        assert not missing, f"Missing critical tools: {missing}"

    def test_no_duplicate_names(self):
        """Tool names must be unique."""
        names = [s["function"]["name"] for s in self.registry.schemas()]
        assert len(names) == len(set(names)), f"Duplicates: {[n for n in names if names.count(n) > 1]}"

    def test_execute_unknown_tool(self):
        """Unknown tool returns warning, not crash."""
        result = self.registry.execute("nonexistent_tool_xyz", {})
        assert "Unknown tool" in result


# ─── Utils ──────────────────────────────────────────────────────

class TestUtils:
    """Core utility functions."""

    def test_safe_relpath_prevents_traversal(self):
        from ouroboros.utils import safe_relpath
        assert ".." not in safe_relpath("../../etc/passwd")
        assert safe_relpath("normal/path.txt") == "normal/path.txt"

    def test_utc_now_iso_format(self):
        from ouroboros.utils import utc_now_iso
        ts = utc_now_iso()
        assert "T" in ts
        assert "+" in ts or "Z" in ts

    def test_clip_text(self):
        from ouroboros.utils import clip_text
        short = "hello"
        assert clip_text(short, 100) == short
        long_text = "a" * 1000
        clipped = clip_text(long_text, 100)
        assert len(clipped) <= 120  # some margin for truncation marker

    def test_estimate_tokens(self):
        from ouroboros.utils import estimate_tokens
        t = estimate_tokens("hello world")
        assert isinstance(t, int)
        assert t > 0


# ─── Memory ─────────────────────────────────────────────────────

class TestMemory:
    """Memory module (scratchpad, identity, chat history)."""

    @pytest.fixture
    def memory(self, tmp_path):
        from ouroboros.memory import Memory
        return Memory(drive_root=tmp_path)

    def test_scratchpad_roundtrip(self, memory):
        memory.save_scratchpad("Test content 123")
        content = memory.load_scratchpad()
        assert "Test content 123" in content

    def test_identity_roundtrip(self, memory):
        memory.save_identity("I am Ouroboros")
        content = memory.load_identity()
        assert "I am Ouroboros" in content

    def test_chat_history_add_and_load(self, memory):
        memory.append_chat("user", "Hello")
        memory.append_chat("assistant", "Hi there")
        history = memory.load_chat_history(count=10)
        assert len(history) >= 2


# ─── Context builder ────────────────────────────────────────────

class TestContextBuilder:
    """Context assembly produces valid LLM messages."""

    def test_build_user_content_text(self):
        from ouroboros.context import _build_user_content
        task = {"text": "Hello world"}
        content = _build_user_content(task)
        assert content == "Hello world"

    def test_build_user_content_with_image(self):
        from ouroboros.context import _build_user_content
        task = {
            "text": "Describe this",
            "image_base64": "abc123",
            "image_mime": "image/png",
        }
        content = _build_user_content(task)
        assert isinstance(content, list)
        assert any(c.get("type") == "image_url" for c in content)


# ─── Review / complexity metrics ────────────────────────────────

class TestReview:
    """Code review and complexity metrics."""

    def test_collect_code_sections(self):
        from ouroboros.review import collect_code_sections
        sections = collect_code_sections(REPO_ROOT)
        assert len(sections) > 0
        # Each section is (path, content)
        for path, content in sections:
            assert isinstance(path, str)
            assert isinstance(content, str)

    def test_compute_complexity_metrics(self):
        from ouroboros.review import collect_code_sections, compute_complexity_metrics
        sections = collect_code_sections(REPO_ROOT)
        metrics = compute_complexity_metrics(sections)
        assert "total_files" in metrics
        assert metrics["total_files"] > 0


# ─── Version invariant ──────────────────────────────────────────

class TestVersionInvariant:
    """VERSION file, README, and git tag must be in sync."""

    def test_version_file_exists(self):
        version_path = REPO_ROOT / "VERSION"
        assert version_path.exists(), "VERSION file missing"
        version = version_path.read_text().strip()
        assert version, "VERSION file is empty"
        # Must be semver-like
        parts = version.split(".")
        assert len(parts) == 3, f"Not semver: {version}"
        assert all(p.isdigit() for p in parts), f"Not numeric: {version}"

    def test_version_in_readme(self):
        version = (REPO_ROOT / "VERSION").read_text().strip()
        readme = (REPO_ROOT / "README.md").read_text()
        assert version in readme, f"VERSION {version} not found in README.md"


# ─── BIBLE invariant ────────────────────────────────────────────

class TestBibleInvariant:
    """BIBLE.md must exist and contain core principles."""

    def test_bible_exists(self):
        assert (REPO_ROOT / "BIBLE.md").exists()

    def test_bible_has_all_principles(self):
        bible = (REPO_ROOT / "BIBLE.md").read_text()
        for i in range(9):
            assert f"Принцип {i}" in bible, f"Missing Принцип {i}"

    def test_bible_has_limitations(self):
        bible = (REPO_ROOT / "BIBLE.md").read_text()
        assert "Ограничения" in bible


# ─── Structural invariants ──────────────────────────────────────

class TestStructuralInvariants:
    """Key files and directories must exist."""

    REQUIRED_FILES = [
        "BIBLE.md",
        "VERSION",
        "README.md",
        "requirements.txt",
        "colab_launcher.py",
        "prompts/SYSTEM.md",
        "ouroboros/__init__.py",
        "ouroboros/agent.py",
        "ouroboros/loop.py",
        "ouroboros/context.py",
        "ouroboros/llm.py",
        "ouroboros/memory.py",
        "ouroboros/tools/__init__.py",
        "ouroboros/tools/registry.py",
        "supervisor/__init__.py",
    ]

    @pytest.mark.parametrize("path", REQUIRED_FILES)
    def test_file_exists(self, path):
        assert (REPO_ROOT / path).exists(), f"Missing: {path}"

    def test_no_oversized_modules(self):
        """No Python file should exceed 1000 lines (Bible Principle 5)."""
        for py_file in REPO_ROOT.rglob("*.py"):
            if ".git" in str(py_file) or "__pycache__" in str(py_file):
                continue
            lines = len(py_file.read_text().splitlines())
            assert lines <= 1000, f"{py_file.relative_to(REPO_ROOT)}: {lines} lines > 1000 limit"
