import pathlib

from ouroboros.tools.query_code import _query_code
from ouroboros.tools.registry import ToolContext, ToolRegistry


def _repo(tmp_path: pathlib.Path) -> pathlib.Path:
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pkg" / "helper.py").write_text(
        "def helper():\n"
        "    return 1\n",
        encoding="utf-8",
    )
    (repo / "pkg" / "main.py").write_text(
        "from .helper import helper\n\n"
        "class Worker:\n"
        "    def run(self):\n"
        "        return helper()\n",
        encoding="utf-8",
    )
    (repo / "web.js").write_text("import {x} from './x.js';\nfunction start(){ return x(); }\n", encoding="utf-8")
    return repo


def test_query_code_symbols_references_callers_and_impact(tmp_path):
    repo = _repo(tmp_path)
    data = tmp_path / "data"
    ctx = ToolContext(repo_dir=repo, drive_root=data)

    symbols = _query_code(ctx, op="symbols", query="Worker")
    assert "pkg/main.py" in symbols
    assert "class Worker" in symbols

    refs = _query_code(ctx, op="references", query="helper", path="pkg/helper.py")
    assert "pkg/main.py" in refs

    callers = _query_code(ctx, op="callers", query="helper", path="pkg/helper.py")
    assert "pkg/main.py" in callers
    assert "helper" in callers
    callees = _query_code(ctx, op="callees", query="run", path="pkg/main.py")
    assert "helper" in callees

    impact = _query_code(ctx, op="impact", query="pkg/helper.py")
    assert "pkg/main.py" in impact


def test_query_code_relevant_and_structural(tmp_path):
    repo = _repo(tmp_path)
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path / "data")

    relevant = _query_code(ctx, op="relevant_files", query="worker run helper")
    assert "pkg/main.py" in relevant

    structural = _query_code(ctx, op="structural", query="FunctionDef", lang="python", path="pkg")
    assert "pkg/helper.py" in structural or "pkg/main.py" in structural

    js_symbols = _query_code(ctx, op="definition", query="start", path="web.js")
    assert "web.js" in js_symbols


def test_query_code_registered_and_policy_wired(tmp_path):
    from ouroboros.safety import POLICY_SKIP, TOOL_POLICY
    from ouroboros.tool_capabilities import (
        ACTING_SUBAGENT_TOOL_NAMES,
        CORE_TOOL_NAMES,
        LOCAL_READONLY_SUBAGENT_TOOL_NAMES,
        READ_ONLY_PARALLEL_TOOLS,
        TOOL_RESULT_LIMITS,
    )

    registry = ToolRegistry(repo_dir=_repo(tmp_path), drive_root=tmp_path / "data")
    names = {schema["function"]["name"] for schema in registry.schemas()}
    assert "query_code" in names
    assert TOOL_POLICY["query_code"] == POLICY_SKIP
    assert "query_code" in CORE_TOOL_NAMES
    assert "query_code" in LOCAL_READONLY_SUBAGENT_TOOL_NAMES
    assert "query_code" in ACTING_SUBAGENT_TOOL_NAMES
    assert "query_code" in READ_ONLY_PARALLEL_TOOLS
    assert TOOL_RESULT_LIMITS["query_code"] == 80_000


def test_query_code_workspace_and_subagent_schema_roots(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tool_capabilities import ACTING_SUBAGENT_MODE, LOCAL_READONLY_SUBAGENT_MODE

    repo = _repo(tmp_path)
    registry = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "data")
    ctx = ToolContext(repo_dir=repo, drive_root=tmp_path / "data", workspace_root=repo, workspace_mode="external")
    registry.set_context(ctx)
    assert "query_code" in {schema["function"]["name"] for schema in registry.schemas()}

    readonly = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path / "data",
        task_constraint=TaskConstraint(mode=LOCAL_READONLY_SUBAGENT_MODE),
    )
    registry.set_context(readonly)
    schema = registry.get_schema_by_name("query_code")["function"]
    assert schema["parameters"]["properties"]["root"]["enum"] == ["active_workspace", "system_repo"]

    acting = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path / "data",
        task_constraint=TaskConstraint(mode=ACTING_SUBAGENT_MODE, surface="self_worktree"),
    )
    registry.set_context(acting)
    schema = registry.get_schema_by_name("query_code")["function"]
    assert schema["parameters"]["properties"]["root"]["enum"] == ["active_workspace"]
    blocked = _query_code(acting, op="symbols", query="Worker", root="system_repo")
    assert "TOOL_ACCESS_BLOCKED" in blocked


def test_query_code_hides_local_readonly_subagent_secret_paths(tmp_path):
    from ouroboros.contracts.task_constraint import TaskConstraint
    from ouroboros.tool_capabilities import LOCAL_READONLY_SUBAGENT_MODE

    repo = _repo(tmp_path)
    (repo / "auth").mkdir()
    (repo / "auth" / "secret.py").write_text("def leak():\n    return 'TOKEN_LEAK'\n", encoding="utf-8")
    ctx = ToolContext(
        repo_dir=repo,
        drive_root=tmp_path / "data",
        task_constraint=TaskConstraint(mode=LOCAL_READONLY_SUBAGENT_MODE),
    )

    result = _query_code(ctx, op="structural", query="leak", path="auth")
    assert "TOKEN_LEAK" not in result
    assert "auth/secret.py" not in result
    cache_files = list((tmp_path / "data" / "state" / "code_intel").glob("*/inventory.json"))
    assert cache_files == []
