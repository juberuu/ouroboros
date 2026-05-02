"""Regression tests for the doc-only preflight bypass.

The maintainer hit a 6-retry loop on a documentation-only commit (39
rounds, 3 hours) before this check existed. Each retry was running the
full pytest suite against a `.md`-only diff. The fix in
`ouroboros/tools/git.py::_diff_is_doc_only` short-circuits that case.

Defensive: any staged file under ``tests/`` triggers the full preflight,
even if the extension is markdown (test fixtures can be markdown).
"""

from __future__ import annotations

import pytest

from ouroboros.tools.git import _diff_is_doc_only


@pytest.mark.parametrize("paths", [
    ["README.md"],
    ["docs/CHANGELOG.md"],
    ["docs/architecture.md", "README.md"],
    ["data.json"],
    ["notes.txt"],
    ["docs/api.rst"],
])
def test_doc_only_diffs_match(paths):
    assert _diff_is_doc_only(paths) is True


@pytest.mark.parametrize("paths", [
    ["ouroboros/agent.py"],
    ["docs/CHANGELOG.md", "ouroboros/agent.py"],   # mixed → not doc-only
    ["setup.py"],
    ["pyproject.toml"],
])
def test_non_doc_diffs_do_not_match(paths):
    assert _diff_is_doc_only(paths) is False


@pytest.mark.parametrize("paths", [
    ["tests/test_foo.md"],
    ["tests/fixtures/sample.md"],
    ["nested/tests/foo.md"],
    ["ouroboros/tests/foo.md"],
])
def test_paths_under_tests_dir_are_not_doc_only(paths):
    """Defensive: any file under tests/ runs the preflight, even if .md."""
    assert _diff_is_doc_only(paths) is False


def test_empty_path_list_is_not_doc_only():
    assert _diff_is_doc_only([]) is False


def test_blank_strings_are_skipped():
    assert _diff_is_doc_only(["", "  ", "README.md"]) is True
    assert _diff_is_doc_only(["", "  "]) is False
