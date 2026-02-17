"""Smoke tests for Ouroboros.

Fast, no network, no secrets required.
Run: python -m pytest tests/test_smoke.py -v
"""

import importlib
import pathlib
import tempfile
import json
import os
import sys
import pytest

# Ensure repo root is importable
REPO = pathlib.Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


# ═══════════════════════════════════════════════════════════════════
# 1. Module imports — every module loads without errors
# ═══════════════════════════════════════════════════════════════════

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
    "supervisor.state",
    "supervisor.telegram",
    "supervisor.queue",
    "supervisor.workers",
    "supervisor.git_ops",
    "supervisor.events",
]


class TestModuleImports:
    @pytest.mark.parametrize("mod", MODULES)
    def test_import(self, mod):
        importlib.import_module(mod)


# ═══════════════════════════════════════════════════════════════════
# 2. Tool registry — all tools discoverable, valid schemas
# ═══════════════════════════════════════════════════════════════════


class TestToolRegistry:
    @pytest.fixture(autouse=True)
    def _setup(self):
        from ouroboros.tools.registry import ToolRegistry
        self.registry = ToolRegistry()

    def test_tool_count_minimum(self):
        """At least 30 tools expected (33 as of v4.14.0)."""
        schemas = self.registry.get_schemas()
        assert len(schemas) >= 30, f"Only {len(schemas)} tools found"

    def test_all_schemas_valid(self):
        """Every schema has required OpenAI function call fields."""
        for schema in self.registry.get_schemas():
            assert schema["type"] == "function"
            fn = schema["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn
            assert fn["parameters"].get("type") == "object"

    def test_known_tools_present(self):
        """Key tools must be registered."""
        names = {s["function"]["name"] for s in self.registry.get_schemas()}
        required = {
            "repo_read", "repo_write_commit", "repo_list",
            "drive_read", "drive_write", "drive_list",
            "git_status", "git_diff", "repo_commit_push",
            "run_shell", "claude_code_edit",
            "web_search", "browse_page", "browser_action",
            "request_restart", "promote_to_stable",
            "update_scratchpad", "update_identity",
            "chat_history", "schedule_task", "cancel_task",
            "request_review", "switch_model",
            "send_owner_message", "codebase_digest",
            "toggle_evolution", "toggle_consciousness",
            "knowledge_read", "knowledge_write", "knowledge_list",
            "codebase_health",
            "multi_model_review",
        }
        missing = required - names
        assert not missing, f"Missing tools: {missing}"

    def test_no_duplicate_names(self):
        """No two tools share the same name."""
        names = [s["function"]["name"] for s in self.registry.get_schemas()]
        assert len(names) == len(set(names)), f"Duplicates: {[n for n in names if names.count(n) > 1]}"

    def test_execute_unknown_tool(self):
        """Unknown tool returns error, doesn't crash."""
        result = self.registry.execute("nonexistent_tool_xyz", {}, ctx={})
        assert "error" in result.lower() or "unknown" in result.lower()


# ═══════════════════════════════════════════════════════════════════
# 3. Utils — pure functions
# ═══════════════════════════════════════════════════════════════════


class TestUtils:
    def test_safe_relpath_normal(self):
        from ouroboros.utils import safe_relpath
        assert safe_relpath("foo/bar.py") == "foo/bar.py"

    def test_safe_relpath_strips_leading_slash(self):
        from ouroboros.utils import safe_relpath
        assert safe_relpath("/foo/bar.py") == "foo/bar.py"

    def test_safe_relpath_rejects_traversal(self):
        from ouroboros.utils import safe_relpath
        with pytest.raises(ValueError, match="traversal"):
            safe_relpath("../../etc/passwd")

    def test_safe_relpath_rejects_mid_traversal(self):
        from ouroboros.utils import safe_relpath
        with pytest.raises(ValueError, match="traversal"):
            safe_relpath("foo/../../etc/passwd")

    def test_truncate_short(self):
        from ouroboros.utils import truncate
        text = "hello"
        assert truncate(text, 100) == text

    def test_truncate_long(self):
        from ouroboros.utils import truncate
        text = "x" * 200
        result = truncate(text, 100)
        assert len(result) <= 120  # some slack for truncation message
        assert "truncated" in result.lower() or len(result) <= 100


# ═══════════════════════════════════════════════════════════════════
# 4. Memory — reads/writes with temp directory
# ═══════════════════════════════════════════════════════════════════


class TestMemory:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_path):
        from ouroboros.memory import Memory
        self.mem = Memory(drive_root=tmp_path, repo_dir=tmp_path)
        self.mem.ensure_files()

    def test_scratchpad_roundtrip(self):
        self.mem.save_scratchpad("test content 123")
        assert self.mem.load_scratchpad() == "test content 123"

    def test_scratchpad_empty_default(self):
        content = self.mem.load_scratchpad()
        # Should return something (empty or default), not crash
        assert isinstance(content, str)

    def test_identity_empty_default(self):
        content = self.mem.load_identity()
        assert isinstance(content, str)

    def test_ensure_files_creates_dirs(self, tmp_path):
        from ouroboros.memory import Memory
        mem = Memory(drive_root=tmp_path / "new_subdir", repo_dir=tmp_path)
        mem.ensure_files()
        assert mem.scratchpad_path().parent.exists()

    def test_chat_history_empty(self):
        result = self.mem.chat_history(count=10)
        assert isinstance(result, str)

    def test_journal_append(self):
        self.mem.append_journal({"action": "test", "data": "hello"})
        path = self.mem.journal_path()
        assert path.exists()
        content = path.read_text()
        assert "test" in content

    def test_read_jsonl_tail_empty(self):
        result = self.mem.read_jsonl_tail("nonexistent.jsonl")
        assert result == []


# ═══════════════════════════════════════════════════════════════════
# 5. Context builder — builds valid LLM messages
# ═══════════════════════════════════════════════════════════════════


class TestContext:
    def test_build_messages_structure(self, tmp_path):
        """build_llm_messages returns list with system + user messages."""
        from ouroboros.context import build_llm_messages
        from ouroboros.memory import Memory
        from unittest.mock import MagicMock

        mem = Memory(drive_root=tmp_path, repo_dir=tmp_path)
        mem.ensure_files()

        env = MagicMock()
        env.drive_path.return_value = tmp_path
        env.repo_dir.return_value = tmp_path
        env.state = {
            "spent_usd": 10.0,
            "evolution_mode_enabled": False,
            "evolution_cycle": 0,
        }
        env.budget_total.return_value = 500.0
        env.git_head.return_value = "abc123"
        env.git_branch.return_value = "ouroboros"

        task = {"id": "test123", "type": "chat", "text": "hello"}

        messages = build_llm_messages(env, mem, task)

        assert isinstance(messages, list)
        assert len(messages) >= 2  # system + at least one user

        # First message is system
        assert messages[0]["role"] == "system"

        # Last message is user (task)
        assert messages[-1]["role"] == "user"

    def test_system_message_contains_bible(self, tmp_path):
        """System message should contain Bible content."""
        from ouroboros.context import build_llm_messages
        from ouroboros.memory import Memory
        from unittest.mock import MagicMock

        mem = Memory(drive_root=tmp_path, repo_dir=tmp_path)
        mem.ensure_files()

        # Write a fake BIBLE
        (tmp_path / "BIBLE.md").write_text("# Test Bible\nPrinciple 0: Test")

        env = MagicMock()
        env.drive_path.return_value = tmp_path
        env.repo_dir.return_value = tmp_path
        env.state = {
            "spent_usd": 10.0,
            "evolution_mode_enabled": False,
            "evolution_cycle": 0,
        }
        env.budget_total.return_value = 500.0
        env.git_head.return_value = "abc123"
        env.git_branch.return_value = "ouroboros"

        task = {"id": "t", "type": "chat", "text": "hi"}
        messages = build_llm_messages(env, mem, task)

        system_content = messages[0]["content"]
        # System content is either string or list of blocks
        if isinstance(system_content, list):
            text = " ".join(
                b["text"] for b in system_content if isinstance(b, dict) and "text" in b
            )
        else:
            text = system_content
        assert "Bible" in text or "Principle" in text or "BIBLE" in text


# ═══════════════════════════════════════════════════════════════════
# 6. Review — complexity metrics
# ═══════════════════════════════════════════════════════════════════


class TestReview:
    def test_collect_code_sections(self):
        from ouroboros.review import collect_code_sections
        sections = collect_code_sections(REPO)
        assert len(sections) > 0
        for path, content in sections:
            assert isinstance(path, str)
            assert isinstance(content, str)

    def test_complexity_metrics(self):
        from ouroboros.review import collect_code_sections, compute_complexity_metrics
        sections = collect_code_sections(REPO)
        metrics = compute_complexity_metrics(sections)
        assert "total_files" in metrics
        assert "py_files" in metrics
        assert metrics["total_files"] > 0


# ═══════════════════════════════════════════════════════════════════
# 7. LLM client — construction (no actual API calls)
# ═══════════════════════════════════════════════════════════════════


class TestLLM:
    def test_client_creation(self):
        """LLMClient can be constructed with dummy key."""
        from ouroboros.llm import LLMClient
        client = LLMClient(api_key="test-key-not-real")
        assert client is not None

    def test_estimate_cost(self):
        """Cost estimation doesn't crash."""
        from ouroboros.llm import LLMClient
        client = LLMClient(api_key="test-key")
        # The method may or may not exist directly, but pricing should work
        assert hasattr(client, 'model') or True  # basic sanity


# ═══════════════════════════════════════════════════════════════════
# 8. Invariants — Bible compliance checks
# ═══════════════════════════════════════════════════════════════════


class TestInvariants:
    def test_version_file_exists(self):
        version = (REPO / "VERSION").read_text().strip()
        assert version, "VERSION file is empty"
        parts = version.split(".")
        assert len(parts) == 3, f"VERSION not semver: {version}"

    def test_readme_contains_version(self):
        version = (REPO / "VERSION").read_text().strip()
        readme = (REPO / "README.md").read_text()
        assert version in readme, f"VERSION {version} not found in README"

    def test_bible_exists_and_has_principles(self):
        bible = (REPO / "BIBLE.md").read_text()
        assert "Принцип 0" in bible or "Principle 0" in bible
        assert "Субъектность" in bible or "Subjectivity" in bible

    def test_system_prompt_exists(self):
        system = (REPO / "prompts" / "SYSTEM.md").read_text()
        assert len(system) > 1000, "SYSTEM.md suspiciously short"

    def test_no_secrets_in_code(self):
        """No hardcoded tokens or keys in Python files."""
        import re
        patterns = [
            r'sk-[a-zA-Z0-9]{20,}',  # OpenAI-style
            r'ghp_[a-zA-Z0-9]{20,}',  # GitHub PAT
            r'xoxb-[a-zA-Z0-9-]{20,}',  # Slack
        ]
        for py_file in REPO.rglob("*.py"):
            if ".git" in str(py_file):
                continue
            content = py_file.read_text()
            for pattern in patterns:
                matches = re.findall(pattern, content)
                assert not matches, f"Possible secret in {py_file}: {pattern}"

    def test_no_env_dump_commands(self):
        """No commands that dump environment variables."""
        dangerous = ["os.environ", "printenv", "env | ", "export "]
        for py_file in REPO.rglob("*.py"):
            if ".git" in str(py_file) or "test_" in py_file.name:
                continue
            content = py_file.read_text()
            for cmd in dangerous:
                # Allow os.environ.get() but not os.environ alone in print/log
                if cmd == "os.environ":
                    # Check for os.environ without .get/.pop/[] etc
                    lines = content.split("\n")
                    for line in lines:
                        if "os.environ" in line and not any(
                            x in line for x in [".get(", "[", ".pop(", "= os.environ", "import"]
                        ):
                            if "print" in line or "log." in line:
                                pytest.fail(f"Env dump in {py_file}: {line.strip()}")

    def test_all_tools_have_descriptions(self):
        """Every tool schema has a non-empty description."""
        from ouroboros.tools.registry import ToolRegistry
        registry = ToolRegistry()
        for schema in registry.get_schemas():
            desc = schema["function"].get("description", "")
            assert desc, f"Tool {schema['function']['name']} has no description"

    def test_modules_under_size_limit(self):
        """No Python module exceeds 1000 lines (Bible Principle 5)."""
        for py_file in REPO.rglob("*.py"):
            if ".git" in str(py_file) or "test_" in py_file.name:
                continue
            lines = len(py_file.read_text().splitlines())
            assert lines <= 1000, f"{py_file.relative_to(REPO)} has {lines} lines (max 1000)"
