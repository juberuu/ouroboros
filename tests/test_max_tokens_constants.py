"""Regression tests: verify raised max_tokens / max_turns constants."""


def test_review_query_model_max_tokens():
    """review.py _query_model must use ≥65536 max_tokens."""
    import ast
    from pathlib import Path

    src = Path("ouroboros/tools/review.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "max_tokens":
            if isinstance(node.value, ast.Constant) and node.value.value >= 65536:
                return  # found
    raise AssertionError("Expected max_tokens>=65536 in review.py _query_model")


def test_scope_review_max_tokens():
    """scope_review.py _SCOPE_MAX_TOKENS must be ≥100000."""
    from ouroboros.tools.scope_review import _SCOPE_MAX_TOKENS
    assert _SCOPE_MAX_TOKENS >= 100_000


def test_reflection_generate_max_tokens():
    """reflection.py generate_reflection must use ≥4096 max_tokens."""
    src = open("ouroboros/reflection.py", encoding="utf-8").read()
    assert "max_tokens=4096" in src


def test_consciousness_max_tokens():
    """consciousness.py _think must use ≥4096 max_tokens."""
    src = open("ouroboros/consciousness.py", encoding="utf-8").read()
    assert "max_tokens=4096" in src


def test_compaction_max_tokens():
    """context_compaction.py _summarize_round_batch must use ≥16384."""
    src = open("ouroboros/context_compaction.py", encoding="utf-8").read()
    assert "max_tokens=16384" in src


def test_vision_query_default_max_tokens():
    """llm.py vision_query default max_tokens must be ≥4096."""
    src = open("ouroboros/llm.py", encoding="utf-8").read()
    assert "max_tokens: int = 4096" in src


def test_claude_code_edit_sdk_max_turns():
    """Edit and advisory paths must share the same default Claude Code turn budget (50)."""
    import ast
    from pathlib import Path

    # Verify the constant value via AST (works without claude_agent_sdk installed)
    gw_src = Path("ouroboros/gateways/claude_code.py").read_text(encoding="utf-8")
    tree = ast.parse(gw_src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "DEFAULT_CLAUDE_CODE_MAX_TURNS":
                    assert isinstance(node.value, ast.Constant) and node.value.value == 50, (
                        f"DEFAULT_CLAUDE_CODE_MAX_TURNS should be 50, got {getattr(node.value, 'value', '?')}"
                    )
                    found = True
    assert found, "DEFAULT_CLAUDE_CODE_MAX_TURNS not found in claude_code.py"

    # Verify callers reference the shared constant
    shell_src = Path("ouroboros/tools/shell.py").read_text(encoding="utf-8")
    advisory_src = Path("ouroboros/tools/claude_advisory_review.py").read_text(encoding="utf-8")
    assert "DEFAULT_CLAUDE_CODE_MAX_TURNS" in shell_src
    assert "DEFAULT_CLAUDE_CODE_MAX_TURNS" in advisory_src
    assert "max_turns=25" not in shell_src
    assert "max_turns=8" not in advisory_src


def test_claude_code_sdk_only_no_cli_fallback():
    """shell.py must not contain legacy CLI subprocess fallback."""
    src = open("ouroboros/tools/shell.py", encoding="utf-8").read()
    assert "_run_claude_cli" not in src, "CLI fallback function should be gone"
    assert "ensure_claude_cli" not in src, "CLI install function should be gone"


def test_scope_review_budget_limit():
    """scope_review.py budget gate must sit at the unified 850K input-token budget.

    Note: Claude Opus 4.6 has a 1M context window SHARED between input and output.
    estimate_tokens (chars/4) under-counts by ~15%, so at gate=850K actual input
    is ≈1M tokens and output max_tokens draws from the same 1M ceiling. The skip
    path is best-effort — 850K is a conscious trade that lets scope prompts that
    previously skipped at ~778K actually run.
    """
    from ouroboros.tools.scope_review import _SCOPE_BUDGET_TOKEN_LIMIT
    assert _SCOPE_BUDGET_TOKEN_LIMIT == 850_000, (
        f"_SCOPE_BUDGET_TOKEN_LIMIT ({_SCOPE_BUDGET_TOKEN_LIMIT}) must equal 850_000 "
        "to match the unified scope/plan/deep-review input budget."
    )


def test_plan_review_budget_limit():
    """plan_review.py _PLAN_BUDGET_TOKEN_LIMIT must match the unified 850K budget."""
    from ouroboros.tools.plan_review import _PLAN_BUDGET_TOKEN_LIMIT
    assert _PLAN_BUDGET_TOKEN_LIMIT == 850_000, (
        f"_PLAN_BUDGET_TOKEN_LIMIT ({_PLAN_BUDGET_TOKEN_LIMIT}) must equal 850_000 "
        "to match the unified scope/plan/deep-review input budget."
    )


def test_deep_self_review_budget_limit():
    """deep_self_review.py pack-size gate must match the unified 850K budget
    and must gate on the FULL assembled prompt (system + user), not just the pack.

    The deep self-review runner rejects packs whose shared estimate_tokens
    (chars/4) estimate exceeds the gate. This test pins the numeric threshold,
    the use of the shared helper, AND the requirement that the gate reflects
    the full assembled prompt — matching how scope_review and plan_review gate.
    """
    import pathlib
    src = pathlib.Path("ouroboros/deep_self_review.py").read_text(encoding="utf-8")
    assert "estimated_tokens > 850_000" in src, (
        "deep_self_review must gate on estimated_tokens > 850_000"
    )
    assert "Maximum is ~850,000 tokens" in src, (
        "deep_self_review error message must reference 850,000 tokens"
    )
    assert "estimate_tokens(_SYSTEM_PROMPT + pack_text)" in src, (
        "deep_self_review must gate on the FULL assembled prompt "
        "(system + user) using the shared estimate_tokens(chars/4) helper, "
        "matching scope_review and plan_review. Gating on pack_text alone "
        "understates the real request size."
    )
    assert "int(stats[\"total_chars\"] / 3.5)" not in src, (
        "deep_self_review must not use its old chars/3.5 estimator — it diverges "
        "from the scope/plan review chars/4 budget at the same nominal token gate"
    )


def test_tool_timeout_uses_max_of_settings_and_per_tool():
    """_get_tool_timeout must return max(settings, per_tool) not just settings."""
    import importlib
    from unittest.mock import patch
    import ouroboros.loop_tool_execution as mod

    class FakeTools:
        def get_timeout(self, name):
            return 1200  # per-tool declares 1200s

    # settings says 600, per-tool says 1200 → should return 1200
    with patch.object(mod, "load_settings", return_value={"OUROBOROS_TOOL_TIMEOUT_SEC": 600}):
        result = mod._get_tool_timeout(FakeTools(), "claude_code_edit")
    assert result == 1200, f"Expected 1200 (per-tool), got {result}"


def test_tool_timeout_settings_wins_when_higher():
    """_get_tool_timeout: if settings > per_tool, settings wins."""
    from unittest.mock import patch
    import ouroboros.loop_tool_execution as mod

    class FakeTools:
        def get_timeout(self, name):
            return 360  # default per-tool

    with patch.object(mod, "load_settings", return_value={"OUROBOROS_TOOL_TIMEOUT_SEC": 900}):
        result = mod._get_tool_timeout(FakeTools(), "run_shell")
    assert result == 900, f"Expected 900 (settings), got {result}"


def test_review_evidence_no_truncation_by_default():
    """format_review_evidence_for_prompt must NOT truncate by default (max_chars=0)."""
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    big = {"has_evidence": True, "data": "x" * 10000}
    result = format_review_evidence_for_prompt(big)
    assert "truncated" not in result.lower()
    assert len(result) > 10000


def test_review_evidence_bounded_with_omission_note():
    """format_review_evidence_for_prompt truncates with explicit omission note when max_chars>0."""
    from ouroboros.review_evidence import format_review_evidence_for_prompt
    big = {"has_evidence": True, "data": "x" * 10000}
    result = format_review_evidence_for_prompt(big, max_chars=500)
    assert "OMISSION NOTE" in result
    assert "truncated at 500 chars" in result


def test_review_evidence_no_obligation_cap():
    """collect_review_evidence default max_obligations must be None (no cap)."""
    import inspect
    from ouroboros.review_evidence import collect_review_evidence
    sig = inspect.signature(collect_review_evidence)
    default = sig.parameters["max_obligations"].default
    assert default is None, f"Expected None, got {default}"


def test_claude_code_edit_timeout_1200():
    """claude_code_edit ToolEntry must declare timeout_sec=1200."""
    from ouroboros.tools.shell import get_tools
    entries = get_tools()
    cce = [e for e in entries if e.name == "claude_code_edit"]
    assert cce, "claude_code_edit not found in shell.get_tools()"
    assert cce[0].timeout_sec == 1200


def test_advisory_pre_review_timeout_1200():
    """advisory_pre_review ToolEntry must declare timeout_sec=1200."""
    from ouroboros.tools.claude_advisory_review import get_tools
    entries = get_tools()
    apr = [e for e in entries if e.name == "advisory_pre_review"]
    assert apr, "advisory_pre_review not found"
    assert apr[0].timeout_sec == 1200


def test_full_repo_pack_excludes_junk_dirs():
    """build_full_repo_pack must skip jsonschema/, Python.framework/, certifi/, tests/."""
    from ouroboros.tools.review_helpers import _FULL_REPO_SKIP_DIR_PREFIXES
    for prefix in ("jsonschema/", "jsonschema_specifications/", "Python.framework/", "certifi/", "tests/"):
        assert prefix in _FULL_REPO_SKIP_DIR_PREFIXES, f"{prefix} not in skip list"


def test_summary_and_reflection_callers_use_bounded_evidence():
    """Summary and reflection prompt builders must call format_review_evidence_for_prompt with max_chars."""
    import ast
    from pathlib import Path

    for filename in ("ouroboros/agent_task_pipeline.py", "ouroboros/reflection.py"):
        src = Path(filename).read_text(encoding="utf-8")
        assert "format_review_evidence_for_prompt(" in src
        # Must pass max_chars argument (not rely on default 0)
        assert "max_chars=" in src, f"{filename} must call format_review_evidence_for_prompt with max_chars"


def test_obligation_context_shows_all():
    """build_review_context must not slice open_obligations."""
    src = open("ouroboros/agent_task_pipeline.py", encoding="utf-8").read()
    assert "open_obs[:4]" not in src, "open_obs[:4] cap should be removed"
    assert "obligation_ids[:4]" not in src, "obligation_ids[:4] cap should be removed"
