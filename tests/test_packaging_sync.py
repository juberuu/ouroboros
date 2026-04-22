import pathlib

import pytest

from ouroboros.tools.release_sync import _normalize_pep440

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_version_file_and_pyproject_are_synced():
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    # ``VERSION`` holds the author-facing spelling (``4.50.0-rc.1`` /
    # ``4.50.0``); ``pyproject.toml`` must carry the PEP 440-canonical
    # form (``4.50.0rc1`` / ``4.50.0``) so pip / build / twine accept
    # the project metadata. For stable versions the two forms are
    # identical; for pre-releases ``_normalize_pep440`` collapses the
    # separators.
    pyproject_version = _normalize_pep440(version)
    assert f'version = "{pyproject_version}"' in pyproject


def test_pyproject_includes_provider_svgs():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    assert '"providers/*.svg"' in pyproject


@pytest.mark.skipif(not (REPO / "Dockerfile").exists(), reason="Dockerfile not present in repo (bundle-only)")
def test_dockerfile_sets_default_file_browser_root():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")

    assert "OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}" in dockerfile
