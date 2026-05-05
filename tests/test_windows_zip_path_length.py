from __future__ import annotations

import pathlib
import os

import pytest


pytestmark = pytest.mark.portable_detail


def test_bundled_playwright_headless_shell_paths_stay_short():
    playwright = pytest.importorskip("playwright", reason="Playwright is not installed")
    root = pathlib.Path(playwright.__file__).parent / "driver" / "package" / ".local-browsers"
    if not root.is_dir():
        if os.environ.get("OUROBOROS_EXPECT_HEADLESS_SHELL") == "1":
            pytest.fail("Expected Playwright local browser bundle in this CI lane")
        pytest.skip("Playwright local browser bundle not present")
    shells = sorted(root.glob("chromium_headless_shell-*"))
    if not shells:
        if os.environ.get("OUROBOROS_EXPECT_HEADLESS_SHELL") == "1":
            pytest.fail("Expected Playwright headless-shell bundle in this CI lane")
        pytest.skip("Playwright headless-shell bundle not present")
    too_long = []
    for shell in shells:
        for path in shell.rglob("*"):
            if not path.is_file():
                continue
            rel_len = len(str(path.relative_to(root)))
            if rel_len > 200:
                too_long.append((rel_len, path.relative_to(root).as_posix()))
    assert not too_long, f"Headless-shell paths exceed 200 chars: {too_long[:10]}"
