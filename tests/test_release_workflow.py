import pathlib


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _workflow() -> str:
    return (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def test_ci_release_preflight_validates_tag_matches_version():
    workflow = _workflow()

    assert "release-preflight:" in workflow
    assert "Validate tag matches VERSION" in workflow
    assert 'expected_tag = f"v{version}"' in workflow
    assert 'tag != expected_tag' in workflow


def test_ci_release_prerelease_flag_uses_preflight_output():
    workflow = _workflow()

    assert "needs.release-preflight.outputs.is_prerelease" in workflow
    assert "prerelease: ${{ needs.release-preflight.outputs.is_prerelease == 'true' }}" in workflow
    assert "re.search(r'(?:rc|alpha|beta|a|b)\\.?\\d+$'" in workflow
    assert "fh.write(f\"is_prerelease={'true' if is_prerelease else 'false'}\\n\")" in workflow


def test_ci_build_job_exports_release_tag_and_fetches_full_history():
    workflow = _workflow()

    assert "OUROBOROS_RELEASE_TAG: ${{ github.ref_name }}" in workflow
    assert "OUROBOROS_MANAGED_SOURCE_BRANCH: ouroboros" in workflow
    assert "fetch-depth: 0" in workflow
