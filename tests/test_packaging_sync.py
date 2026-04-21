import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_version_file_and_pyproject_are_synced():
    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    assert f'version = "{version}"' in pyproject


def test_pyproject_includes_provider_svgs():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    assert '"providers/*.svg"' in pyproject


@pytest.mark.skipif(not (REPO / "Dockerfile").exists(), reason="Dockerfile not present in repo (bundle-only)")
def test_dockerfile_sets_default_file_browser_root():
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")

    assert "OUROBOROS_FILE_BROWSER_DEFAULT=${APP_HOME}" in dockerfile
