"""Regression tests for platform build scripts.

These tests ensure each build script contains the critical Playwright Chromium
install step with the correct env-var flag and that the install step appears
BEFORE the actual PyInstaller command-line invocation — so Chromium is always
bundled inside the ``python-standalone`` data tree before packaging.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).parent.parent


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _find_pyinstaller_cmd_pos(src: str) -> int:
    """Return the character position of the first line that actually *runs*
    PyInstaller (i.e. contains 'PyInstaller' outside a comment/echo line).

    Build scripts have comment lines and echo lines mentioning 'PyInstaller'
    before the actual invocation; we need the real command line.
    """
    for match in re.finditer(r"PyInstaller", src):
        # Find the start of the line containing this match.
        line_start = src.rfind("\n", 0, match.start()) + 1
        line = src[line_start: src.find("\n", match.start())]
        stripped = line.strip()
        # Skip comment lines (bash: '#', PowerShell: '#') and echo/Write-Host.
        if stripped.startswith("#") or stripped.lower().startswith("echo") or stripped.lower().startswith("write-host"):
            continue
        return match.start()
    return -1


# ---------------------------------------------------------------------------
# build.sh  (macOS)
# ---------------------------------------------------------------------------

class TestBuildSh:
    """build.sh must install the Chromium headless shell before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build.sh")
        assert "playwright install --only-shell chromium" in src, (
            "build.sh must call 'playwright install --only-shell chromium' on macOS"
        )

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build.sh")
        assert "PLAYWRIGHT_BROWSERS_PATH=0" in src, (
            "build.sh must set PLAYWRIGHT_BROWSERS_PATH=0 for the playwright install step"
        )

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build.sh")
        pw_pos = src.find("playwright install --only-shell chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1, "playwright install --only-shell chromium not found in build.sh"
        assert pi_pos != -1, "PyInstaller command not found in build.sh"
        assert pw_pos < pi_pos, (
            "playwright install --only-shell chromium must appear BEFORE PyInstaller in build.sh "
            f"(found at char {pw_pos}, PyInstaller cmd at {pi_pos})"
        )

    def test_symlink_normalizer_skips_playwright_browser_bundles(self):
        src = _read("build.sh")
        assert "_should_skip_symlink" in src, (
            "build.sh should centralize the macOS symlink-skip guard for bundled "
            "browser bundles"
        )
        assert ".local-browsers" in src, (
            "build.sh must skip symlink normalization inside Playwright's bundled "
            "browser tree on macOS"
        )
        assert ".app" in src and ".framework" in src, (
            "build.sh must preserve nested macOS app/framework bundles during "
            "symlink normalization"
        )


# ---------------------------------------------------------------------------
# build_linux.sh  (Linux)
# ---------------------------------------------------------------------------

class TestBuildLinuxSh:
    """build_linux.sh must install Chromium with PLAYWRIGHT_BROWSERS_PATH=0 before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build_linux.sh")
        assert "playwright install chromium" in src

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build_linux.sh")
        assert "PLAYWRIGHT_BROWSERS_PATH=0" in src

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build_linux.sh")
        pw_pos = src.find("playwright install chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1
        assert pi_pos != -1
        assert pw_pos < pi_pos, (
            "playwright install chromium must appear BEFORE PyInstaller in build_linux.sh"
        )


# ---------------------------------------------------------------------------
# build_windows.ps1  (Windows / PowerShell)
# ---------------------------------------------------------------------------

class TestBuildWindowsPs1:
    """build_windows.ps1 must install Chromium with PLAYWRIGHT_BROWSERS_PATH=0 before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build_windows.ps1")
        assert "playwright install chromium" in src

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build_windows.ps1")
        # PowerShell syntax: $env:PLAYWRIGHT_BROWSERS_PATH = "0"
        assert 'PLAYWRIGHT_BROWSERS_PATH' in src and '"0"' in src, (
            "build_windows.ps1 must set PLAYWRIGHT_BROWSERS_PATH to '0'"
        )

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build_windows.ps1")
        pw_pos = src.find("playwright install chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1
        assert pi_pos != -1
        assert pw_pos < pi_pos, (
            "playwright install chromium must appear BEFORE PyInstaller in build_windows.ps1"
        )


# ---------------------------------------------------------------------------
# Dockerfile  (Docker / web runtime)
# ---------------------------------------------------------------------------

class TestDockerfile:
    """Dockerfile must install Playwright Chromium binary so browser tools work
    out of the box in the container without additional setup."""

    def test_playwright_install_chromium_present(self):
        src = _read("Dockerfile")
        assert "playwright install chromium" in src, (
            "Dockerfile must call 'playwright install chromium' to bundle the browser"
        )

    def test_playwright_browsers_path_zero_set(self):
        src = _read("Dockerfile")
        assert "PLAYWRIGHT_BROWSERS_PATH=0" in src, (
            "Dockerfile must set PLAYWRIGHT_BROWSERS_PATH=0 so Chromium installs "
            "inside the pip package tree (not into a user cache that won't survive "
            "image layer boundaries)"
        )

    def test_playwright_install_deps_present(self):
        """Dockerfile must use 'playwright install-deps chromium' (the authoritative
        Playwright dependency resolver) rather than a hand-curated apt library list.
        This ensures all runtime native libs required by Chromium are present."""
        src = _read("Dockerfile")
        assert "playwright install-deps chromium" in src, (
            "Dockerfile must call 'playwright install-deps chromium' to install all "
            "native system libraries required by Chromium via Playwright's authoritative "
            "dependency resolver"
        )

    def test_install_deps_before_install_chromium(self):
        """Native system dependencies must be installed BEFORE the Chromium binary
        is downloaded, so the binary can find its runtime libraries on first launch."""
        src = _read("Dockerfile")
        deps_pos = src.find("playwright install-deps chromium")
        binary_pos = src.find("playwright install chromium")
        # binary_pos must not match the install-deps line itself
        # find the standalone 'playwright install chromium' (not install-deps)
        import re as _re
        binary_match = _re.search(r"(?<!install-deps )playwright install chromium", src)
        assert deps_pos != -1, "playwright install-deps chromium not found in Dockerfile"
        assert binary_match is not None, "standalone playwright install chromium not found in Dockerfile"
        assert deps_pos < binary_match.start(), (
            "playwright install-deps must appear BEFORE playwright install chromium in Dockerfile"
        )

    def test_pip_install_before_playwright_install_deps(self):
        """pip install must appear BEFORE playwright install-deps chromium — the
        playwright Python package must be importable when install-deps runs."""
        src = _read("Dockerfile")
        pip_pos = src.find("pip install")
        deps_pos = src.find("playwright install-deps chromium")
        assert pip_pos != -1, "pip install step not found in Dockerfile"
        assert deps_pos != -1, "playwright install-deps chromium not found in Dockerfile"
        assert pip_pos < deps_pos, (
            "pip install must appear BEFORE playwright install-deps chromium in Dockerfile "
            f"(pip at char {pip_pos}, install-deps at {deps_pos})"
        )

    def test_pip_install_before_all_playwright_invocations(self):
        """pip install must appear BEFORE every ``python3 -m playwright ...`` invocation
        in the Dockerfile — both ``install-deps`` and ``install chromium``.
        If *any* playwright invocation precedes pip install, ModuleNotFoundError occurs."""
        src = _read("Dockerfile")
        pip_pos = src.find("pip install")
        assert pip_pos != -1, "pip install step not found in Dockerfile"

        import re as _re
        playwright_invocations = [
            m.start() for m in _re.finditer(r"python3 -m playwright", src)
        ]
        assert playwright_invocations, "No 'python3 -m playwright' invocations found in Dockerfile"

        earliest_playwright = min(playwright_invocations)
        assert pip_pos < earliest_playwright, (
            "pip install must appear BEFORE the earliest 'python3 -m playwright' invocation "
            f"in the Dockerfile (pip at char {pip_pos}, earliest playwright at {earliest_playwright}). "
            f"Found {len(playwright_invocations)} playwright invocation(s) at positions: "
            f"{playwright_invocations}"
        )
