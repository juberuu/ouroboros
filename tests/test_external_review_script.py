from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from scripts.run_external_review import (
    _classify_exit,
    _create_isolated_checkout,
    _openrouter_pool,
    _remove_isolated_checkout,
    _resolved_review_config,
    _review_evidence_and_cost,
)


def test_external_review_script_delegates_verdict_to_production_gate():
    source = Path("scripts/run_external_review.py").read_text(encoding="utf-8")
    assert "v6.10.0" not in source
    assert "Google Colab" not in source
    assert "_run_non_committing_review_cycle" in source
    assert "adaptive_quorum" not in source
    assert "aggregate_review_verdict" not in source
    # The wrapper stays thin: no operator-side re-binding layer, and the REAL
    # advisory (not a bypass) is the default first stage.
    assert "operator_binding" not in source
    assert "_handle_advisory_pre_review" in source
    assert "skip_advisory_review=True" not in source


def test_external_review_script_defaults_to_pro_mode():
    source = Path("scripts/run_external_review.py").read_text(encoding="utf-8")
    assert 'setdefault("OUROBOROS_RUNTIME_MODE", "pro")' in source


def test_external_review_script_resolves_models_and_efforts(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "CLOUDRU_FOUNDATION_MODELS_API_KEY",
        "GIGACHAT_CREDENTIALS",
        "GIGACHAT_USER",
        "GIGACHAT_PASSWORD",
        "OPENAI_BASE_URL",
        "OPENAI_COMPATIBLE_BASE_URL",
        "OUROBOROS_MODEL",
        "OUROBOROS_MODEL_LIGHT",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OUROBOROS_REVIEW_MODELS", "anthropic/claude-opus-4.8,google/gemini-3.5-flash,openai/gpt-5.5")
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_MODELS", "openai/gpt-5.5")
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "high")
    monkeypatch.setenv("OUROBOROS_EFFORT_SCOPE_REVIEW", "high")

    config = _resolved_review_config()

    assert config["triad_models"] == [
        "anthropic/claude-opus-4.8",
        "google/gemini-3.5-flash",
        "openai/gpt-5.5",
    ]
    assert config["triad_effort"] == "high"
    assert config["scope_models"] == ["openai/gpt-5.5"]
    assert config["scope_effort"] == "high"


def _complete_ctx():
    triad = [
        {
            "slot_id": f"slot_{idx}",
            "model_id": f"reviewer-{idx}",
            "status": "responded",
            "tokens_in": 100,
            "cost_usd": 0.01,
            "prompt_ref": {"manifest_ref": f"prompt-{idx}"},
            "response_ref": {"manifest_ref": f"response-{idx}"},
        }
        for idx in range(1, 4)
    ]
    scope_actor = {
        "slot_id": "scope_slot_1",
        "model_id": "scope-reviewer",
        "status": "responded",
        "tokens_in": 200,
        "cost_usd": 0.0,
        "prompt_ref": {"manifest_ref": "scope-prompt"},
        "response_ref": {"manifest_ref": "scope-response"},
    }
    return SimpleNamespace(
        _last_triad_raw_results=triad,
        _last_scope_raw_result={"raw_results": [scope_actor]},
    )


def test_external_review_cost_report_never_turns_unknown_into_zero():
    evidence, report = _review_evidence_and_cost(_complete_ctx())
    assert len(evidence) == 4
    assert report["reported_actor_cost_usd"] == 0.03
    assert report["unreported_or_unknown_cost_slots"] == ["scope_slot_1"]
    assert "not treated as $0" in report["note"]


def test_exit_classification_separates_infra_from_genuine_blocks():
    assert _classify_exit({"status": "passed"}) == 0
    assert _classify_exit({"status": "blocked", "block_reason": "critical_findings"}) == 1
    # A scope CRITICAL with concrete findings is a genuine reviewer verdict...
    assert _classify_exit({
        "status": "blocked",
        "block_reason": "scope_blocked",
        "combined_findings": [{"severity": "CRITICAL", "text": "real defect"}],
    }) == 1
    # ...while a findings-less scope block is fail-closed infrastructure.
    assert _classify_exit({"status": "blocked", "block_reason": "scope_blocked"}) == 3
    for infra_reason in (
        "tests_preflight_blocked",
        "core_protection_blocked",
        "no_advisory",
        "review_quorum",
        "fingerprint_unavailable",
        "",
    ):
        assert _classify_exit({"status": "blocked", "block_reason": infra_reason}) == 3, infra_reason


def test_openrouter_pool_orders_hope_keys_last(monkeypatch, tmp_path):
    keys = tmp_path / "file1.txt"
    keys.write_text(
        "hope_new_key_openrouter: sk-or-hope-000\n"
        "openrouter_kuznetsov3: sk-or-kuz-111\n"
        "backup_hope_openrouter: sk-or-hope-bak-444\n"
        "openai: sk-oa-222\n"
        "anton_openrouter_main: sk-or-anton-333\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_KEYS_FILE", str(keys))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    pool = _openrouter_pool()

    names = [name for name, _ in pool]
    # Any hope-bucket key sinks to the tail, prefix or not.
    assert names == [
        "openrouter_kuznetsov3",
        "anton_openrouter_main",
        "hope_new_key_openrouter",
        "backup_hope_openrouter",
    ]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True,
    )
    return proc.stdout


def test_isolated_checkout_freezes_the_reviewed_tree(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # Windows runners default to autocrlf=true, which rewrites checked-out
    # files to CRLF and breaks LF patch application in the detached worktree.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "base")
    (repo / "a.txt").write_text("staged change\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    staged_patch = _git(repo, "diff", "--cached", "--binary")

    import scripts.run_external_review as module

    monkeypatch.setattr(module, "REPO", repo)
    checkout_root, checkout = _create_isolated_checkout(staged_patch)
    try:
        # The frozen checkout carries the staged content in both index and tree.
        assert (checkout / "a.txt").read_text(encoding="utf-8") == "staged change\n"
        assert "a.txt" in _git(checkout, "diff", "--cached", "--name-only")
        # A later edit in the primary worktree does not leak into the checkout.
        (repo / "a.txt").write_text("post-review drift\n", encoding="utf-8")
        assert (checkout / "a.txt").read_text(encoding="utf-8") == "staged change\n"
    finally:
        _remove_isolated_checkout(checkout_root, checkout)
    assert not checkout.exists()


def test_reviewed_tree_comparison_is_untracked_safe(tmp_path):
    """A NEW staged file must not read as drift after the cycle's reset HEAD.

    The production cycle ends with ``git reset HEAD`` in the checkout, turning
    newly added files untracked; only a homogeneous re-staged comparison
    (git add -A + git diff --cached) matches the operator's staged patch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    # Windows runners default to autocrlf=true, which rewrites checked-out
    # files to CRLF and breaks LF patch application in the detached worktree.
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")
    (repo / "brand_new.py").write_text("print('new module')\n", encoding="utf-8")
    _git(repo, "add", "brand_new.py")
    staged_patch = _git(repo, "diff", "--cached", "--binary")

    # Simulate the post-cycle state: staged patch applied, then reset HEAD.
    _git(repo, "reset", "HEAD")
    naive = _git(repo, "diff", "HEAD", "--binary")
    assert naive.strip() != staged_patch.strip()  # the trap: untracked lost

    _git(repo, "add", "-A")
    homogeneous = _git(repo, "diff", "--cached", "--binary")
    assert homogeneous.strip() == staged_patch.strip()
