from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_public_contributor_flow_targets_working_branch_and_real_review_profile():
    guide = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "against `ouroboros`, not `main` or `ouroboros-stable`" in guide
    assert "--contributor" in guide
    assert "--base-ref upstream/ouroboros" in guide
    assert "--head-ref HEAD" in guide
    assert "does **not** run the Claude advisory pre-review" in guide
    assert "Do not bump the version" in guide
    assert "review-packet.zip" in guide
    assert "evidence, not a promise to merge" in guide


def test_pull_request_template_collects_fast_path_evidence_without_version_bump():
    template = (ROOT / ".github" / "PULL_REQUEST_TEMPLATE.md").read_text(
        encoding="utf-8"
    )

    assert "The PR base branch is `ouroboros`" in template
    assert "I did **not** bump `VERSION`" in template
    assert "Reviewed base SHA" in template
    assert "Triad verdict" in template
    assert "Scope verdict" in template
    assert "If not run, reason" in template
    assert "Agent assistance (optional)" in template


def test_pull_request_ci_is_fork_safe_and_does_not_enable_provider_jobs():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "pull_request:\n    branches: [ouroboros]" in workflow
    assert "\n  pull_request_target:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "github.event_name == 'pull_request' && github.base_ref == 'ouroboros'" in workflow
    assert "release:\n" in workflow
    assert "      contents: write" in workflow


def test_repository_has_explicit_mit_license_holder():
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 Anton Razzhigaev" in license_text
    assert "Andrew Kaznacheev" not in license_text
