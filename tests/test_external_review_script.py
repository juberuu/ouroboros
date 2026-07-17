from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_external_review import (
    _REVIEW_SUBSTRATE_PATHS,
    _apply_contributor_landing_obligations,
    _apply_contributor_review_env,
    _assert_contributor_openrouter_config,
    _classify_exit,
    _contributor_snapshot,
    _create_isolated_checkout,
    _openrouter_key_health,
    _openrouter_pool,
    _remove_isolated_checkout,
    _require_contributor_budget,
    _resolved_review_config,
    _review_evidence_and_cost,
    _select_healthy_openrouter_key,
    _settings_defaults_at_ref,
    _write_contributor_packet,
)


def test_contributor_trust_boundary_covers_functional_review_dependencies():
    from ouroboros.tools.scope_review import _CANONICAL_CONTEXT_DOCS

    assert set(_CANONICAL_CONTEXT_DOCS).issubset(_REVIEW_SUBSTRATE_PATHS)
    assert {
        "docs/ARCHITECTURE.md",
        "ouroboros/capability_evidence.py",
        "ouroboros/code_intelligence.py",
        "ouroboros/deadline_utils.py",
        "ouroboros/outcomes.py",
        "ouroboros/platform_layer.py",
        "ouroboros/pricing.py",
        "ouroboros/review_state.py",
        "ouroboros/runtime_mode_policy.py",
        "ouroboros/usage_accounting.py",
        "ouroboros/utils.py",
        "ouroboros/tools/claude_advisory_review.py",
        "ouroboros/tools/registry.py",
        "ouroboros/tools/release_sync.py",
        "ouroboros/tools/review_synthesis.py",
    }.issubset(_REVIEW_SUBSTRATE_PATHS)


def test_external_review_script_delegates_verdict_to_production_gate():
    source = Path("scripts/run_external_review.py").read_text(encoding="utf-8")
    assert "v6.10.0" not in source
    assert "Google Colab" not in source
    assert "_run_non_committing_review_cycle" in source
    assert "adaptive_quorum" not in source
    assert "aggregate_review_verdict" not in source
    # The default operator lane still runs the REAL advisory. Contributor mode
    # explicitly skips it while reusing the production triad+scope cycle.
    assert "operator_binding" not in source
    assert "_handle_advisory_pre_review" in source
    assert "skip_advisory_review=args.contributor" in source
    assert "_CONTRIBUTOR_PROFILE = \"external_pr_readiness\"" in source


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
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    monkeypatch.setenv("OUROBOROS_SCOPE_REVIEW_FLOOR", "blocking_1m")

    config = _resolved_review_config()

    assert config["triad_models"] == [
        "anthropic/claude-opus-4.8",
        "google/gemini-3.5-flash",
        "openai/gpt-5.5",
    ]
    assert config["triad_effort"] == "high"
    assert config["scope_models"] == ["openai/gpt-5.5"]
    assert config["scope_effort"] == "high"
    assert config["review_enforcement"] == "blocking"
    assert config["scope_review_floor"] == "blocking_1m"


def _write_target_config(repo: Path) -> None:
    package = repo / "ouroboros"
    package.mkdir(exist_ok=True)
    (package / "config.py").write_text(
        "SETTINGS_DEFAULTS = {\n"
        "    'OUROBOROS_REVIEW_MODELS': 'anthropic/fable,openai/sol,google/flash',\n"
        "    'OUROBOROS_SCOPE_REVIEW_MODELS': 'anthropic/fable',\n"
        "    'OUROBOROS_EFFORT_REVIEW': 'high',\n"
        "    'OUROBOROS_EFFORT_SCOPE_REVIEW': 'high',\n"
        "    'OUROBOROS_SCOPE_REVIEW_FLOOR': 'blocking_1m',\n"
        "}\n",
        encoding="utf-8",
    )


def _init_contributor_repo(tmp_path: Path, monkeypatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "core.autocrlf", "false")
    (repo / "scripts").mkdir()
    (repo / "scripts" / "run_external_review.py").write_text("# base script\n", encoding="utf-8")
    _write_target_config(repo)
    (repo / "ouroboros" / "review_substrate.py").write_text(
        "# trusted review substrate\n", encoding="utf-8"
    )
    (repo / "ouroboros" / "utils.py").write_text(
        "# trusted review utilities\n", encoding="utf-8"
    )
    (repo / "ouroboros" / "tools").mkdir()
    (repo / "ouroboros" / "tools" / "registry.py").write_text(
        "# trusted review context\n", encoding="utf-8"
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "test-project"\nversion = "1.2.3"\n',
        encoding="utf-8",
    )
    (repo / "VERSION").write_text("1.2.3\n", encoding="utf-8")
    (repo / "a.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "base")
    _git(repo, "branch", "base")
    (repo / "a.txt").write_text("proposal\n", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-m", "proposal")

    import scripts.run_external_review as module

    monkeypatch.setattr(module, "REPO", repo)
    return repo


def test_target_base_defaults_override_local_review_settings(tmp_path, monkeypatch):
    _init_contributor_repo(tmp_path, monkeypatch)
    defaults = _settings_defaults_at_ref("base")
    monkeypatch.setenv("OUROBOROS_REVIEW_MODELS", "local/override")
    monkeypatch.setenv("OUROBOROS_EFFORT_REVIEW", "low")

    _apply_contributor_review_env(defaults)

    assert defaults["OUROBOROS_REVIEW_MODELS"] == (
        "anthropic/fable,openai/sol,google/flash"
    )
    assert os.environ["OUROBOROS_REVIEW_MODELS"] == defaults[
        "OUROBOROS_REVIEW_MODELS"
    ]
    assert os.environ["OUROBOROS_EFFORT_REVIEW"] == "high"
    assert os.environ["OUROBOROS_REVIEW_ENFORCEMENT"] == "blocking"
    assert os.environ["OUROBOROS_OBSERVABILITY_KEEP_RAW"] == "0"
    assert os.environ["OUROBOROS_PRE_PUSH_TESTS"] == "1"
    assert os.environ["OUROBOROS_PREFLIGHT_DIFF_AWARE"] == "false"


def test_contributor_defaults_reject_explicit_direct_provider_route(monkeypatch):
    defaults = {
        "OUROBOROS_REVIEW_MODELS": "anthropic::claude-fable-5",
        "OUROBOROS_SCOPE_REVIEW_MODELS": "anthropic/claude-fable-5",
        "OUROBOROS_EFFORT_REVIEW": "high",
        "OUROBOROS_EFFORT_SCOPE_REVIEW": "high",
        "OUROBOROS_SCOPE_REVIEW_FLOOR": "blocking_1m",
    }
    with pytest.raises(RuntimeError, match="non-OpenRouter"):
        _apply_contributor_review_env(defaults)


def test_contributor_budget_must_be_explicit_positive_and_finite(monkeypatch):
    monkeypatch.delenv("TOTAL_BUDGET", raising=False)
    with pytest.raises(RuntimeError, match="TOTAL_BUDGET is required"):
        _require_contributor_budget()
    for invalid in ("0", "-1", "inf", "not-a-number"):
        monkeypatch.setenv("TOTAL_BUDGET", invalid)
        with pytest.raises(RuntimeError, match="positive finite"):
            _require_contributor_budget()
    monkeypatch.setenv("TOTAL_BUDGET", "125.50")
    assert _require_contributor_budget() == 125.5


def test_contributor_snapshot_binds_clean_base_head_and_tree(tmp_path, monkeypatch):
    repo = _init_contributor_repo(tmp_path, monkeypatch)

    snapshot = _contributor_snapshot("base", "HEAD")

    assert snapshot["base_sha"] == snapshot["merge_base_sha"]
    assert snapshot["target_version"] == "1.2.3"
    assert snapshot["head_tree_sha"] == _git(repo, "rev-parse", "HEAD^{tree}").strip()
    assert snapshot["changed_paths"] == ["a.txt"]
    assert snapshot["review_substrate_changed"] == []
    assert snapshot["diff_sha256"]

    (repo / "dirty.txt").write_text("not committed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="not clean"):
        _contributor_snapshot("base", "HEAD")


def test_contributor_snapshot_rejects_version_bump(tmp_path, monkeypatch):
    repo = _init_contributor_repo(tmp_path, monkeypatch)
    (repo / "VERSION").write_text("1.2.4\n", encoding="utf-8")
    _git(repo, "add", "VERSION")
    _git(repo, "commit", "-m", "bad contributor bump")

    with pytest.raises(RuntimeError, match="must not bump VERSION"):
        _contributor_snapshot("base", "HEAD")


@pytest.mark.parametrize(
    "relative_path",
    [
        "ouroboros/review_substrate.py",
        "ouroboros/utils.py",
        "ouroboros/tools/registry.py",
    ],
)
def test_contributor_snapshot_flags_transitive_review_substrate_changes(
    tmp_path, monkeypatch, relative_path
):
    repo = _init_contributor_repo(tmp_path, monkeypatch)
    path = repo / relative_path
    path.write_text("# proposal changes trusted review substrate\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", "change review substrate")

    snapshot = _contributor_snapshot("base", "HEAD")

    assert snapshot["review_substrate_changed"] == [relative_path]
    assert snapshot["review_substrate_matches_base"] is False


def test_contributor_snapshot_flags_release_carrier_changes_without_version_file(
    tmp_path, monkeypatch
):
    repo = _init_contributor_repo(tmp_path, monkeypatch)
    path = repo / "pyproject.toml"
    path.write_text(
        '[project]\nname = "test-project"\nversion = "1.2.4"\n',
        encoding="utf-8",
    )
    _git(repo, "add", "pyproject.toml")
    _git(repo, "commit", "-m", "change package carrier only")

    snapshot = _contributor_snapshot("base", "HEAD")

    assert snapshot["release_metadata_or_machinery_changed"] is True
    assert snapshot["release_sensitive_changes"]["carrier_fields"] == [
        "pyproject.project.version"
    ]


def test_contributor_landing_obligations_are_exact_typed_items_only():
    version_only = {
        "status": "blocked",
        "block_reason": "critical_findings",
        "combined_findings": [
            {"item": "version_bump", "severity": "critical"},
            {"item": "changelog_and_badge", "severity": "critical"},
        ],
    }
    deferred = _apply_contributor_landing_obligations(version_only)
    assert deferred["status"] == "passed"
    assert {item["item"] for item in deferred["landing_obligations"]} == {
        "version_bump",
        "changelog_and_badge",
    }

    real_defect = {
        **version_only,
        "combined_findings": [
            *version_only["combined_findings"],
            {"item": "self_consistency", "severity": "critical"},
        ],
    }
    assert _apply_contributor_landing_obligations(real_defect) == real_defect
    scope_failure = {
        **version_only,
        "block_reason": "scope_blocked",
    }
    assert _apply_contributor_landing_obligations(scope_failure) == scope_failure
    assert _apply_contributor_landing_obligations(
        version_only,
        release_sensitive=True,
    ) == version_only


def test_contributor_packet_is_redacted_and_shareable(tmp_path):
    output = tmp_path / "packet"
    output.mkdir()
    local_root = "/Users/example/private/repo"
    packet = _write_contributor_packet(
        output_dir=output,
        snapshot={
            "base_sha": "a" * 40,
            "head_sha": "b" * 40,
            "review_substrate_changed": [],
        },
        resolved_config={"triad_models": ["anthropic/fable"]},
        outcome={"status": "passed", "path": local_root, "api_key": "sk-secret-value"},
        exit_code=0,
        evidence_refs=[],
        cost_report={"reported_actor_cost_usd": 1.0},
        elapsed_sec=1.5,
        triad_raw=[{"authorization": "Bearer secret-token-value", "path": local_root}],
        scope_raw={"status": "responded"},
        degraded_reasons=["reviewer-3=parse_failure (quorum still met)"],
        replacements=[(local_root, "$REPO")],
    )

    evidence_text = (output / "review-evidence.json").read_text(encoding="utf-8")
    full_text = (output / "full-output.txt").read_text(encoding="utf-8")
    assert "sk-secret-value" not in evidence_text
    assert "secret-token-value" not in full_text
    assert local_root not in evidence_text + full_text
    assert "$REPO" in evidence_text + full_text
    assert "production_triad_quorum_plus_authoritative_scope" in evidence_text
    assert "quorum still met" in evidence_text
    with zipfile.ZipFile(packet) as archive:
        assert set(archive.namelist()) == {
            "review-evidence.json",
            "outcome.json",
            "full-output.txt",
        }


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


def test_contributor_openrouter_preflight_fails_closed(monkeypatch):
    import scripts.run_external_review as module

    monkeypatch.setattr(module, "_openrouter_pool", lambda: [])
    with pytest.raises(RuntimeError, match="no OpenRouter key"):
        _select_healthy_openrouter_key(required=True)

    monkeypatch.setattr(module, "_openrouter_pool", lambda: [("key", "secret")])
    monkeypatch.setattr(
        module,
        "_openrouter_key_health",
        lambda _token, **_kwargs: (False, "model_probe_http_403"),
    )
    with pytest.raises(RuntimeError, match="no healthy OpenRouter key"):
        _select_healthy_openrouter_key(required=True)


def test_contributor_resolved_config_rejects_direct_provider_actors():
    defaults = {
        "OUROBOROS_REVIEW_MODELS": "anthropic/claude-fable-5,openai/gpt-5.6-sol",
        "OUROBOROS_SCOPE_REVIEW_MODELS": "anthropic/claude-fable-5",
        "OUROBOROS_EFFORT_REVIEW": "high",
        "OUROBOROS_EFFORT_SCOPE_REVIEW": "high",
        "OUROBOROS_SCOPE_REVIEW_FLOOR": "blocking_1m",
    }
    _assert_contributor_openrouter_config({
        "triad_models": ["anthropic/claude-fable-5", "openai/gpt-5.6-sol"],
        "scope_models": ["anthropic/claude-fable-5"],
        "triad_effort": "high",
        "scope_effort": "high",
        "scope_review_floor": "blocking_1m",
    }, defaults)
    with pytest.raises(RuntimeError, match="exclusively through OpenRouter"):
        _assert_contributor_openrouter_config({
            "triad_models": ["anthropic::claude-fable-5"],
            "scope_models": ["anthropic/claude-fable-5"],
            "triad_effort": "high",
            "scope_effort": "high",
            "scope_review_floor": "blocking_1m",
        }, defaults)
    with pytest.raises(RuntimeError, match="drifted from target-base defaults"):
        _assert_contributor_openrouter_config({
            "triad_models": ["anthropic/claude-fable-5"],
            "scope_models": ["anthropic/claude-fable-5"],
            "triad_effort": "high",
            "scope_effort": "high",
            "scope_review_floor": "blocking_1m",
        }, defaults)


def test_normal_key_probe_stays_single_model_but_contributor_probes_all(monkeypatch):
    import scripts.run_external_review as module

    calls: list[str] = []
    monkeypatch.setattr(module, "_review_probe_models", lambda: ["one", "two", "three"])
    monkeypatch.setattr(
        module,
        "_probe_model_for_key",
        lambda _token, model: (calls.append(model) is None, f"ok:{model}"),
    )
    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"data": {"limit": None}}

    import httpx

    monkeypatch.setattr(httpx, "get", lambda *_args, **_kwargs: Response())

    assert _openrouter_key_health("secret")[0] is True
    assert calls == ["one"]
    calls.clear()
    assert _openrouter_key_health("secret", probe_all_models=True)[0] is True
    assert calls == ["one", "two", "three"]


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
