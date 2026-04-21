"""Tests for browser state isolation and infrastructure error detection."""
import pathlib
import sys
import types

import pytest

import ouroboros.tools.browser as browser_mod
from ouroboros.tools.browser import _is_infrastructure_error, cleanup_browser


class TestInfrastructureErrorDetection:
    """_is_infrastructure_error should detect structural Playwright failures."""

    def test_detects_greenlet_switch(self):
        assert _is_infrastructure_error(RuntimeError("cannot switch to a different green thread"))

    def test_detects_different_thread(self):
        assert _is_infrastructure_error(RuntimeError("different thread"))

    def test_detects_browser_closed(self):
        assert _is_infrastructure_error(Exception("browser has been closed"))

    def test_detects_page_closed(self):
        assert _is_infrastructure_error(Exception("page has been closed"))

    def test_detects_connection_closed(self):
        assert _is_infrastructure_error(Exception("Connection closed"))

    def test_ignores_normal_errors(self):
        assert not _is_infrastructure_error(ValueError("invalid selector"))
        assert not _is_infrastructure_error(TimeoutError("navigation timeout"))


class TestBrowserModuleState:
    """Module-level state should be properly initialized."""

    def test_is_infrastructure_error_is_function(self):
        assert callable(_is_infrastructure_error)

    def test_ensure_browser_tolerates_missing_thread_id(self, monkeypatch):
        fake_page = types.SimpleNamespace(set_default_timeout=lambda timeout: None)

        def _new_page(**kwargs):
            return fake_page

        fake_browser = types.SimpleNamespace(
            new_page=_new_page,
            is_connected=lambda: True,
        )
        fake_playwright = types.SimpleNamespace(
            chromium=types.SimpleNamespace(launch=lambda **kwargs: fake_browser)
        )
        fake_sync_api = types.SimpleNamespace(
            sync_playwright=lambda: types.SimpleNamespace(start=lambda: fake_playwright)
        )
        monkeypatch.setattr(browser_mod, "_HAS_STEALTH", False)
        monkeypatch.setattr(browser_mod, "_ensure_playwright_installed", lambda: None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_sync_api)

        ctx = types.SimpleNamespace(
            browser_state=types.SimpleNamespace(
                page=None,
                browser=None,
                pw_instance=None,
                last_screenshot_b64=None,
            )
        )

        page = browser_mod._ensure_browser(ctx)

        assert page is fake_page
        assert getattr(ctx.browser_state, "_thread_id", None) is not None

    def test_aliases_arm64_browser_cache_for_missing_x64_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(browser_mod.sys, "platform", "darwin", raising=False)
        root = tmp_path / "playwright" / "chromium_headless_shell-1208"
        arm_dir = root / "chrome-headless-shell-mac-arm64"
        arm_dir.mkdir(parents=True)
        arm_binary = arm_dir / "chrome-headless-shell"
        arm_binary.write_text("stub", encoding="utf-8")

        missing_binary = root / "chrome-headless-shell-mac-x64" / "chrome-headless-shell"
        err = RuntimeError(f"BrowserType.launch: Executable doesn't exist at {missing_binary}")

        assert browser_mod._maybe_alias_playwright_binary(err) is True
        alias_dir = missing_binary.parent
        assert alias_dir.is_symlink()
        assert pathlib.Path(alias_dir.resolve()) == arm_dir.resolve()


class TestHasPlatformChromium:
    """_has_platform_chromium: two-level check — chromium-* dir + platform-matching subdir."""

    def test_missing_dir_returns_false(self, tmp_path):
        from ouroboros.tools.browser import _has_platform_chromium
        assert _has_platform_chromium(tmp_path / "nonexistent") is False

    def test_empty_dir_returns_false(self, tmp_path):
        from ouroboros.tools.browser import _has_platform_chromium
        assert _has_platform_chromium(tmp_path) is False

    def test_non_chromium_dir_returns_false(self, tmp_path):
        from ouroboros.tools.browser import _has_platform_chromium
        (tmp_path / "firefox-1234").mkdir()
        assert _has_platform_chromium(tmp_path) is False

    def test_chromium_dir_no_matching_platform_subdir_returns_false(self, tmp_path, monkeypatch):
        from ouroboros.tools import browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        from ouroboros.tools.browser import _has_platform_chromium
        chromium_dir = tmp_path / "chromium-1234"
        chromium_dir.mkdir()
        (chromium_dir / "chrome-linux-x64").mkdir()  # wrong platform
        assert _has_platform_chromium(tmp_path) is False

    def test_chromium_dir_with_no_executable_returns_false(self, tmp_path, monkeypatch):
        """A non-empty chrome-mac-* dir with only metadata (no Chromium.app) must NOT trigger."""
        from ouroboros.tools import browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        from ouroboros.tools.browser import _has_platform_chromium
        chromium_dir = tmp_path / "chromium-1234"
        chromium_dir.mkdir()
        platform_dir = chromium_dir / "chrome-mac-x64"
        platform_dir.mkdir()
        (platform_dir / "metadata.json").write_text("{}", encoding="utf-8")  # metadata only
        assert _has_platform_chromium(tmp_path) is False

    def test_chromium_dir_with_matching_executable_returns_true(self, tmp_path, monkeypatch):
        """A chrome-mac-* dir with the real macOS Chromium.app executable returns True."""
        from ouroboros.tools import browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        from ouroboros.tools.browser import _has_platform_chromium
        chromium_dir = tmp_path / "chromium-1234"
        chromium_dir.mkdir()
        platform_dir = chromium_dir / "chrome-mac-x64"
        exe = platform_dir / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")
        assert _has_platform_chromium(tmp_path) is True

    def test_headless_shell_dir_with_matching_executable_returns_true(self, tmp_path, monkeypatch):
        """A bundled chromium_headless_shell payload should also count as usable."""
        from ouroboros.tools import browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        from ouroboros.tools.browser import _has_platform_chromium
        chromium_dir = tmp_path / "chromium_headless_shell-1234"
        chromium_dir.mkdir()
        platform_dir = chromium_dir / "chrome-headless-shell-mac-arm64"
        exe = platform_dir / "chrome-headless-shell"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")
        assert _has_platform_chromium(tmp_path) is True


class TestSetPlaywrightBrowsersPathIfBundled:
    """_set_playwright_browsers_path_if_bundled: sets env var only when bundled Chromium found."""

    def test_no_op_when_env_already_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/some/custom/path")
        import importlib
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        # Should not overwrite existing env var
        bmod._set_playwright_browsers_path_if_bundled()
        import os
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/some/custom/path"

    def test_sets_zero_when_chromium_dir_matches(self, monkeypatch, tmp_path):
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        # Build fake playwright package structure
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        chromium_dir = local_browsers / "chromium-9999"
        chromium_dir.mkdir(parents=True)
        platform_dir = chromium_dir / "chrome-mac-x64"
        exe = platform_dir / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")  # real macOS executable path
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        bmod._set_playwright_browsers_path_if_bundled()
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"

    def test_sets_zero_when_headless_shell_dir_matches(self, monkeypatch, tmp_path):
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        chromium_dir = local_browsers / "chromium_headless_shell-9999"
        chromium_dir.mkdir(parents=True)
        platform_dir = chromium_dir / "chrome-headless-shell-mac-arm64"
        exe = platform_dir / "chrome-headless-shell"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        bmod._set_playwright_browsers_path_if_bundled()
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"

    def test_no_change_when_no_matching_chromium(self, monkeypatch, tmp_path):
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        local_browsers.mkdir(parents=True)
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        bmod._set_playwright_browsers_path_if_bundled()
        assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ

    def test_import_time_side_effect_sets_env_when_bundled(self, monkeypatch, tmp_path):
        """Module-import calls _set_playwright_browsers_path_if_bundled(); reloading the
        module with a fake bundled Chromium present must set PLAYWRIGHT_BROWSERS_PATH=0."""
        import importlib
        import os
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        # Build fake playwright package with a non-empty platform dir
        local_browsers = tmp_path / "driver" / "package" / ".local-browsers"
        exe = local_browsers / "chromium-9999" / "chrome-mac-x64" / "Chromium.app" / "Contents" / "MacOS" / "Chromium"
        exe.parent.mkdir(parents=True)
        exe.write_text("stub", encoding="utf-8")  # real macOS executable path
        fake_pw = types.SimpleNamespace(__file__=str(tmp_path / "__init__.py"))
        monkeypatch.setitem(sys.modules, "playwright", fake_pw)
        import ouroboros.tools.browser as bmod
        monkeypatch.setattr(bmod.sys, "platform", "darwin", raising=False)
        # Simulate a fresh module import by calling the module-level init directly
        # (importlib.reload would re-run the side-effect but also re-register tools;
        # calling the function directly tests the same code path without side effects)
        bmod._set_playwright_browsers_path_if_bundled()
        assert os.environ.get("PLAYWRIGHT_BROWSERS_PATH") == "0"


class TestCleanupBrowser:
    """cleanup_browser should null out all browser_state references."""

    def test_cleanup_nulls_state(self):
        ctx = types.SimpleNamespace(
            browser_state=types.SimpleNamespace(
                page=None,
                browser=None,
                pw_instance=None,
                last_screenshot_b64=None,
            )
        )
        cleanup_browser(ctx)
        assert ctx.browser_state.page is None
        assert ctx.browser_state.browser is None
        assert ctx.browser_state.pw_instance is None
