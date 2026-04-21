"""Tests for ouroboros/tools/release_sync.py (standalone library, no wire-up)."""

import textwrap
from pathlib import Path

import pytest

from ouroboros.tools.release_sync import (
    check_history_limit,
    detect_numeric_claims,
    run_release_preflight,
    sync_release_metadata,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_repo(tmp_path: Path, version: str = "4.99.1") -> Path:
    """Create a minimal fake repo with all four version-carrier files."""
    (tmp_path / "VERSION").write_text(version + "\n", encoding="utf-8")

    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry]\nname = "ouroboros"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )

    badge_line = (
        '[![Version 0.0.0]'
        '(https://img.shields.io/badge/version-0.0.0-green.svg)]'
        '(VERSION)\n'
    )
    readme_content = (
        "# Ouroboros\n\n"
        + badge_line
        + "\n## Version History\n\n"
        "| Version | Date | Description |\n"
        "|---------|------|-------------|\n"
        f"| {version} | 2026-01-01 | New release |\n"
    )
    (tmp_path / "README.md").write_text(readme_content, encoding="utf-8")

    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "ARCHITECTURE.md").write_text(
        "# Ouroboros v0.0.0 — Architecture\n\nContent here.\n",
        encoding="utf-8",
    )

    return tmp_path


# ---------------------------------------------------------------------------
# sync_release_metadata
# ---------------------------------------------------------------------------

class TestSyncReleaseMetadata:
    def test_syncs_pyproject_toml(self, tmp_path):
        repo = _make_repo(tmp_path, "1.2.3")
        changed = sync_release_metadata(str(repo))
        assert "pyproject.toml" in changed
        text = (repo / "pyproject.toml").read_text()
        assert 'version = "1.2.3"' in text

    def test_syncs_readme_badge(self, tmp_path):
        repo = _make_repo(tmp_path, "1.2.3")
        changed = sync_release_metadata(str(repo))
        assert "README.md" in changed
        text = (repo / "README.md").read_text()
        assert "Version 1.2.3" in text
        assert "version-1.2.3-green" in text

    def test_syncs_architecture_header(self, tmp_path):
        repo = _make_repo(tmp_path, "1.2.3")
        changed = sync_release_metadata(str(repo))
        assert "docs/ARCHITECTURE.md" in changed
        text = (repo / "docs" / "ARCHITECTURE.md").read_text()
        assert "v1.2.3" in text

    def test_idempotent_second_call_returns_no_changes(self, tmp_path):
        repo = _make_repo(tmp_path, "1.2.3")
        sync_release_metadata(str(repo))  # first call mutates
        changed2 = sync_release_metadata(str(repo))  # second call: already in sync
        assert changed2 == []

    def test_no_changes_when_already_in_sync(self, tmp_path):
        repo = _make_repo(tmp_path, "0.0.0")  # _make_repo already uses 0.0.0 in carriers
        # Overwrite VERSION to match the pre-set values
        (repo / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        changed = sync_release_metadata(str(repo))
        assert changed == []

    def test_returns_empty_when_version_file_missing(self, tmp_path):
        changed = sync_release_metadata(str(tmp_path))
        assert changed == []

    def test_returns_empty_for_invalid_version_string(self, tmp_path):
        (tmp_path / "VERSION").write_text("not-a-version\n", encoding="utf-8")
        changed = sync_release_metadata(str(tmp_path))
        assert changed == []

    def test_missing_pyproject_skipped_gracefully(self, tmp_path):
        repo = _make_repo(tmp_path, "2.0.0")
        (repo / "pyproject.toml").unlink()
        changed = sync_release_metadata(str(repo))
        assert "pyproject.toml" not in changed
        # README and ARCHITECTURE still synced
        assert "README.md" in changed

    def test_missing_architecture_skipped_gracefully(self, tmp_path):
        repo = _make_repo(tmp_path, "2.0.0")
        (repo / "docs" / "ARCHITECTURE.md").unlink()
        changed = sync_release_metadata(str(repo))
        assert "docs/ARCHITECTURE.md" not in changed
        assert "pyproject.toml" in changed


# ---------------------------------------------------------------------------
# check_history_limit
# ---------------------------------------------------------------------------

def _build_readme_history(*versions: str) -> str:
    rows = "\n".join(f"| {v} | 2026-01-01 | desc |" for v in versions)
    return f"## Version History\n\n| Version | Date | Description |\n|---|---|---|\n{rows}\n"


class TestCheckHistoryLimit:
    def test_no_warnings_within_limits(self):
        readme = _build_readme_history(
            "5.0.0", "4.0.0",           # 2 major
            "4.5.0", "4.4.0", "4.3.0", "4.2.0", "4.1.0",  # 5 minor
            "4.5.5", "4.5.4", "4.5.3", "4.5.2", "4.5.1",  # 5 patch
        )
        warnings = check_history_limit(readme)
        assert warnings == []

    def test_too_many_major_rows(self):
        readme = _build_readme_history("5.0.0", "4.0.0", "3.0.0")  # 3 major
        warnings = check_history_limit(readme)
        assert any("major" in w for w in warnings)

    def test_too_many_minor_rows(self):
        readme = _build_readme_history(
            "4.6.0", "4.5.0", "4.4.0", "4.3.0", "4.2.0", "4.1.0"  # 6 minor
        )
        warnings = check_history_limit(readme)
        assert any("minor" in w for w in warnings)

    def test_too_many_patch_rows(self):
        readme = _build_readme_history(
            "4.5.6", "4.5.5", "4.5.4", "4.5.3", "4.5.2", "4.5.1"  # 6 patch
        )
        warnings = check_history_limit(readme)
        assert any("patch" in w for w in warnings)

    def test_multiple_violations_reported_separately(self):
        # 3 major + 6 minor → two separate warnings
        readme = _build_readme_history(
            "5.0.0", "4.0.0", "3.0.0",
            "4.6.0", "4.5.0", "4.4.0", "4.3.0", "4.2.0", "4.1.0",
        )
        warnings = check_history_limit(readme)
        assert len(warnings) >= 2

    def test_empty_readme_returns_no_warnings(self):
        assert check_history_limit("") == []

    def test_exact_limit_not_a_violation(self):
        readme = _build_readme_history(
            "4.0.0", "3.0.0",                          # exactly 2 major
            "4.5.0", "4.4.0", "4.3.0", "4.2.0", "4.1.0",  # exactly 5 minor
            "4.5.5", "4.5.4", "4.5.3", "4.5.2", "4.5.1",  # exactly 5 patch
        )
        warnings = check_history_limit(readme)
        assert warnings == []


# ---------------------------------------------------------------------------
# detect_numeric_claims
# ---------------------------------------------------------------------------

class TestDetectNumericClaims:
    def test_detects_N_tests(self):
        claims = detect_numeric_claims("Added 16 tests for the new module.")
        assert any("16" in c for c in claims)

    def test_detects_N_fixes(self):
        claims = detect_numeric_claims("Contains 3 fixes for edge cases.")
        assert any("3" in c for c in claims)

    def test_detects_N_new_tests(self):
        claims = detect_numeric_claims("Includes 42 new regression tests.")
        assert any("42" in c for c in claims)

    def test_no_false_positive_on_plain_numbers(self):
        claims = detect_numeric_claims("Version 4.36.3 released in 2026.")
        assert claims == []

    def test_no_false_positive_on_non_claim_nouns(self):
        claims = detect_numeric_claims("The 5 providers are all supported.")
        assert claims == []

    def test_returns_all_matches_in_text(self):
        text = "Added 5 tests and fixed 2 regressions plus 10 new assertions."
        claims = detect_numeric_claims(text)
        assert len(claims) == 3

    def test_empty_string_returns_empty(self):
        assert detect_numeric_claims("") == []

    def test_case_insensitive(self):
        claims = detect_numeric_claims("Ships 7 TESTS for reliability.")
        assert any("7" in c for c in claims)


# ---------------------------------------------------------------------------
# run_release_preflight (orchestrator)
# ---------------------------------------------------------------------------

class TestRunReleasePreflight:
    def test_returns_changed_and_warnings_tuple(self, tmp_path):
        repo = _make_repo(tmp_path, "4.99.1")
        changed, warnings = run_release_preflight(str(repo))
        assert isinstance(changed, list)
        assert isinstance(warnings, list)

    def test_syncs_carriers_and_returns_paths(self, tmp_path):
        repo = _make_repo(tmp_path, "4.99.1")
        changed, _ = run_release_preflight(str(repo))
        assert len(changed) >= 1  # at least one carrier was out of sync

    def test_second_call_idempotent_no_changes(self, tmp_path):
        repo = _make_repo(tmp_path, "4.99.1")
        run_release_preflight(str(repo))
        changed2, _ = run_release_preflight(str(repo))
        assert changed2 == []

    def test_warns_on_history_limit_breach(self, tmp_path):
        repo = _make_repo(tmp_path, "4.99.1")
        # Inject too many patch rows into README
        readme = repo / "README.md"
        extra_rows = "\n".join(
            f"| 4.99.{i} | 2026-01-01 | desc |" for i in range(10)
        )
        text = readme.read_text()
        readme.write_text(text + "\n" + extra_rows + "\n", encoding="utf-8")
        _, warnings = run_release_preflight(str(repo))
        assert any("patch" in w for w in warnings)

    def test_warns_on_numeric_claims_in_changelog_row(self, tmp_path):
        repo = _make_repo(tmp_path, "4.99.1")
        readme = repo / "README.md"
        text = readme.read_text()
        # Replace the changelog row description with a numeric claim
        text = text.replace(
            "| 4.99.1 | 2026-01-01 | New release |",
            "| 4.99.1 | 2026-01-01 | Ships 12 new tests for reliability. |",
        )
        readme.write_text(text, encoding="utf-8")
        _, warnings = run_release_preflight(str(repo))
        assert any("numeric claims" in w for w in warnings)

    def test_no_warnings_on_clean_repo(self, tmp_path):
        repo = _make_repo(tmp_path, "0.0.0")
        (repo / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        _, warnings = run_release_preflight(str(repo))
        assert warnings == []

    def test_handles_missing_readme_gracefully(self, tmp_path):
        repo = _make_repo(tmp_path, "4.99.1")
        (repo / "README.md").unlink()
        changed, warnings = run_release_preflight(str(repo))
        # Should not raise; README-dependent output will be empty
        assert isinstance(changed, list)
        assert isinstance(warnings, list)
