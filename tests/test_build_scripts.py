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

    def test_repo_bundle_generation_before_pyinstaller(self):
        src = _read("build.sh")
        bundle_pos = src.find("scripts/build_repo_bundle.py")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert bundle_pos != -1, "build.sh must generate repo.bundle before packaging"
        assert "--source-branch" in src, "build.sh must pass an explicit source branch for detached-head builds"
        assert pi_pos != -1, "PyInstaller command not found in build.sh"
        assert bundle_pos < pi_pos, "repo bundle generation must happen before PyInstaller in build.sh"

    def test_repo_bundle_requires_real_tag_on_head(self):
        src = _read("build.sh")
        assert 'refs/tags/$RELEASE_TAG' in src
        assert 'git tag --points-at HEAD' in src
        assert 'OUROBOROS_RELEASE_TAG="$RELEASE_TAG"' not in src

    def test_repo_bundle_requires_annotated_tag(self):
        src = _read("build.sh")
        assert 'git cat-file -t "refs/tags/$RELEASE_TAG"' in src
        assert 'requires annotated git tag' in src

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

    def test_repo_bundle_generation_before_pyinstaller(self):
        src = _read("build_linux.sh")
        bundle_pos = src.find("scripts/build_repo_bundle.py")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert bundle_pos != -1
        assert "--source-branch" in src
        assert pi_pos != -1
        assert bundle_pos < pi_pos, "repo bundle generation must happen before PyInstaller in build_linux.sh"

    def test_repo_bundle_requires_real_tag_on_head(self):
        src = _read("build_linux.sh")
        assert 'refs/tags/$RELEASE_TAG' in src
        assert 'git tag --points-at HEAD' in src
        assert 'OUROBOROS_RELEASE_TAG="$RELEASE_TAG"' not in src

    def test_repo_bundle_requires_annotated_tag(self):
        src = _read("build_linux.sh")
        assert 'git cat-file -t "refs/tags/$RELEASE_TAG"' in src
        assert 'requires annotated git tag' in src


# ---------------------------------------------------------------------------
# build_windows.ps1  (Windows / PowerShell)
# ---------------------------------------------------------------------------

class TestBuildWindowsPs1:
    """build_windows.ps1 must install Chromium with PLAYWRIGHT_BROWSERS_PATH=0 before PyInstaller."""

    def test_playwright_install_chromium_present(self):
        src = _read("build_windows.ps1")
        assert "playwright install --only-shell chromium" in src

    def test_playwright_browsers_path_zero_set(self):
        src = _read("build_windows.ps1")
        # PowerShell syntax: $env:PLAYWRIGHT_BROWSERS_PATH = "0"
        assert 'PLAYWRIGHT_BROWSERS_PATH' in src and '"0"' in src, (
            "build_windows.ps1 must set PLAYWRIGHT_BROWSERS_PATH to '0'"
        )

    def test_playwright_install_before_pyinstaller(self):
        src = _read("build_windows.ps1")
        pw_pos = src.find("playwright install --only-shell chromium")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert pw_pos != -1
        assert pi_pos != -1
        assert pw_pos < pi_pos, (
            "playwright install --only-shell chromium must appear BEFORE PyInstaller in build_windows.ps1"
        )

    def test_windows_build_has_path_length_guard(self):
        src = _read("build_windows.ps1")
        assert "Checking Windows archive path lengths" in src
        assert "Length -gt 200" in src
        assert "paths longer than 200 chars" in src

    def test_repo_bundle_generation_before_pyinstaller(self):
        src = _read("build_windows.ps1")
        bundle_pos = src.find("scripts/build_repo_bundle.py")
        pi_pos = _find_pyinstaller_cmd_pos(src)
        assert bundle_pos != -1
        assert "--source-branch" in src
        assert pi_pos != -1
        assert bundle_pos < pi_pos, "repo bundle generation must happen before PyInstaller in build_windows.ps1"

    def test_repo_bundle_requires_real_tag_on_head(self):
        src = _read("build_windows.ps1")
        assert 'refs/tags/$ReleaseTag' in src
        assert 'git tag --points-at HEAD' in src
        assert '$env:OUROBOROS_RELEASE_TAG' not in src

    def test_repo_bundle_requires_annotated_tag(self):
        src = _read("build_windows.ps1")
        assert 'git cat-file -t "refs/tags/$ReleaseTag"' in src
        assert 'annotated git tag' in src


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


# ---------------------------------------------------------------------------
# .github/workflows/ci.yml + build.sh — macOS code signing & notarization
# ---------------------------------------------------------------------------

class TestMacOSSigning:
    """The CI build job and build.sh together implement optional macOS code
    signing and notarization. Seven contracts are pinned here to prevent
    regression of the GitHub Actions `secrets.*`-in-step-`if:` pitfall, the
    build-script env override / optional-notarytool gate, the keychain
    cleanup guard, and the stapler-failure-as-soft-warning behaviour.

    See docs/DEVELOPMENT.md::"GitHub Actions: secrets in step-level if
    conditions" for the rationale.
    """

    _CI_PATH = ".github/workflows/ci.yml"
    _SIGNING_SECRETS = (
        "BUILD_CERTIFICATE_BASE64",
        "P12_PASSWORD",
        "KEYCHAIN_PASSWORD",
        "APPLE_TEAM_ID",
    )
    _NOTARIZE_SECRETS = (
        "APPLE_ID",
        "APPLE_APP_SPECIFIC_PASSWORD",
    )

    @staticmethod
    def _build_job_header(src: str) -> str:
        """Slice the build job header (everything between `  build:` and the
        first `    steps:` underneath it) so signing-secret env mappings can
        be located without false positives from later step-level env blocks."""
        build_idx = src.find("\n  build:\n")
        assert build_idx != -1, "build job not found in ci.yml"
        steps_idx = src.find("\n    steps:", build_idx)
        assert steps_idx != -1, "build.steps: not found in ci.yml"
        return src[build_idx:steps_idx]

    @staticmethod
    def _iter_step_if_blocks(src: str):
        """Yield every `if:` expression in the workflow as a flat string.

        Catches BOTH step-level and job-level `if:` blocks (the
        `Unrecognized named-value: 'secrets'` rejection applies at every
        level, so checking job-level too is strictly more conservative).

        Heuristic: collect lines starting from `if:` until the next YAML
        key starts (a line whose first non-space char is `-` or whose
        stripped form contains a `:`). Known limitation: a future `if:`
        whose continuation lines legitimately contain `:` (string literals,
        nested expressions) would be split prematurely; the current ci.yml
        has no such case. If that pattern is added, switch to a real YAML
        parser walking each step's `if` field.
        """
        lines = src.splitlines()
        in_if = False
        block: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("if:"):
                if in_if and block:
                    yield " ".join(block)
                in_if = True
                block = [stripped]
                continue
            if in_if:
                # Continuation: indented, not a new YAML key, not a step start.
                if stripped and not stripped.startswith("- ") and ":" not in stripped:
                    block.append(stripped)
                else:
                    yield " ".join(block)
                    in_if = False
                    block = []
        if in_if and block:
            yield " ".join(block)

    def test_ci_signing_secrets_at_job_level(self):
        """All four required signing secrets MUST be mapped at the build job's
        env: block (not at step level), so step-level `if:` conditions can
        read `env.*`. Step-level env blocks are NOT visible to that step's
        own `if:` — only job-level env is.

        Each mapping must ALSO be guarded by `matrix.os == 'macos-latest'`
        so the Apple credentials are scoped to the macOS matrix shard only;
        Linux and Windows sibling shards receive empty strings. This avoids
        exposing the signing material to `build_linux.sh` / `build_windows.ps1`
        subprocesses that have no use for it.
        """
        src = _read(self._CI_PATH)
        header = self._build_job_header(src)
        # Required form (per secret): `<NAME>: ${{ matrix.os == 'macos-latest' && secrets.<NAME> || '' }}`
        for secret in self._SIGNING_SECRETS:
            expected = (
                f"{secret}: ${{{{ matrix.os == 'macos-latest' "
                f"&& secrets.{secret} || '' }}}}"
            )
            assert expected in header, (
                f"build job env: must map {secret} at job level with a "
                f"matrix.os == 'macos-latest' guard so non-macOS shards "
                f"receive empty strings. Expected line: {expected!r}"
            )
        # Optional notarization secrets must also be mapped at job level
        # (so build.sh inherits them as env vars when it runs), with the
        # same matrix-shard guard.
        for secret in self._NOTARIZE_SECRETS:
            expected = (
                f"{secret}: ${{{{ matrix.os == 'macos-latest' "
                f"&& secrets.{secret} || '' }}}}"
            )
            assert expected in header, (
                f"build job env: must also map {secret} (with matrix.os "
                f"guard) so build.sh can run `xcrun notarytool` when it is "
                f"configured. Expected line: {expected!r}"
            )

    def test_release_waits_for_non_provider_smoke_jobs(self):
        src = _read(self._CI_PATH)
        release_idx = src.find("\n  release:\n")
        assert release_idx != -1, "release job not found"
        release_block = src[release_idx:]
        needs_line = next(
            line.strip()
            for line in release_block.splitlines()
            if line.strip().startswith("needs:")
        )
        for job in ("marker-guards", "ui-smoke", "docker-ui-smoke", "docker-portable-test"):
            assert job in needs_line, f"release job must wait for {job}"

    def test_marker_guard_uses_pipefail(self):
        src = _read(self._CI_PATH)
        guard_idx = src.find("Guard non-empty browser marker lanes")
        assert guard_idx != -1, "marker guard step not found"
        guard_block = src[guard_idx:guard_idx + 700]
        assert "set -euo pipefail" in guard_block

    def test_ci_uses_env_context_for_condition(self):
        """No `if:` expression in ci.yml (step-level OR job-level) may
        reference `secrets.*`.

        GitHub Actions rejects `secrets.*` in `if:` with
        `Unrecognized named-value: 'secrets'`. Always use `env.*` instead
        (see the job-level env block test above). The parser used here
        catches both step-level and job-level `if:` blocks deliberately —
        the rejection applies at every level, so a job-level violation
        would also break the workflow.
        """
        src = _read(self._CI_PATH)
        offending = [
            block for block in self._iter_step_if_blocks(src)
            if "secrets." in block
        ]
        assert not offending, (
            "secrets.* must not appear in any step-level if-condition "
            "(promote to job-level env: and reference env.* instead). "
            f"Offenders: {offending}"
        )

    def test_ci_import_gates_on_full_secret_set(self):
        """The Import-Apple-signing-certificate step MUST gate on ALL four
        required signing secrets via env.*, not just the certificate."""
        src = _read(self._CI_PATH)
        import_idx = src.find("Import Apple signing certificate")
        assert import_idx != -1, (
            "Apple signing-certificate Import step not found in ci.yml — "
            "the macOS signing path is missing"
        )
        # Take a generous slice around the Import step's `if:` line.
        region = src[import_idx:import_idx + 800]
        for env_var in self._SIGNING_SECRETS:
            assert f"env.{env_var}" in region, (
                f"Import step if-condition must gate on env.{env_var} to "
                f"prevent partial-secret runs from importing nothing"
            )

    def test_ci_cleanup_keychain_step_present(self):
        """A `Cleanup keychain` step must run with `if: always() &&
        matrix.os == 'macos-latest' && env.BUILD_CERTIFICATE_BASE64 != ''`
        so signing material never persists across runs even when the build
        itself fails, and the bash-only `security` invocation never fires
        on Linux/Windows shards."""
        src = _read(self._CI_PATH)
        # Match the actual STEP definition (`- name: Cleanup keychain`), not
        # any prose mentioning the step elsewhere in the workflow file (e.g.
        # an explanatory comment in the Import step that references the later
        # Cleanup step would match a bare substring search). The `- name:`
        # anchor pins the assertion to the real step header.
        cleanup_anchor = "- name: Cleanup keychain"
        assert cleanup_anchor in src, (
            "ci.yml must include a `- name: Cleanup keychain` step that "
            "deletes the temporary signing keychain after every macOS build"
        )
        cleanup_idx = src.find(cleanup_anchor)
        cleanup_region = src[cleanup_idx:cleanup_idx + 500]
        assert "always()" in cleanup_region, (
            "Cleanup keychain must run with `if: always()` so it fires on "
            "build failures too"
        )
        assert "matrix.os == 'macos-latest'" in cleanup_region, (
            "Cleanup keychain must gate on matrix.os == 'macos-latest' so "
            "the bash-only `security delete-keychain` invocation does not "
            "fire on Linux/Windows shards (where the secret env var would "
            "still be set as job-level env)"
        )
        assert "env.BUILD_CERTIFICATE_BASE64 != ''" in cleanup_region, (
            "Cleanup keychain must gate on env.BUILD_CERTIFICATE_BASE64 so "
            "it does not try to delete a keychain that was never created"
        )

    def test_build_sh_signing_identity_env_override(self):
        """build.sh must allow the signing identity to be overridden via env
        AND auto-detect from the keychain when env is unset/empty.

        The previous hardcoded `Developer ID Application: <Maintainer>
        (<TeamID>)` default broke any fork whose imported cert had a
        different CN (`codesign: no identity found`). The current contract:
        a non-empty `SIGN_IDENTITY` env wins; otherwise auto-detect via
        `security find-identity -v -p codesigning`.
        """
        src = _read("build.sh")
        # The empty-env auto-detect block must check `${SIGN_IDENTITY:-}`
        # explicitly (not `$SIGN_IDENTITY` alone, which would be unbound
        # under `set -u`).
        assert re.search(
            r'\[\s*-z\s*"\$\{SIGN_IDENTITY:-\}"\s*\]',
            src,
        ), (
            "build.sh must guard the auto-detect block with "
            "`[ -z \"${SIGN_IDENTITY:-}\" ]` so the env var wins when set "
            "and auto-detect runs only when env is unset/empty"
        )
        assert "security find-identity" in src and "-p codesigning" in src, (
            "build.sh must call `security find-identity -v -p codesigning` "
            "to auto-detect the signing identity from the keychain when "
            "SIGN_IDENTITY is not set externally"
        )
        # The hardcoded maintainer-specific default must be GONE (it caused
        # `codesign: no identity found` on forks; replaced by auto-detect).
        # We pin a substring that any future re-introduction would trip on.
        assert 'SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application:' not in src, (
            "build.sh must NOT carry a hardcoded maintainer-specific "
            "Developer ID Application default — it breaks forks whose "
            "imported cert has a different CN. Use auto-detect via "
            "`security find-identity` instead."
        )

    def test_build_sh_notarization_optional(self):
        """build.sh must include an optional notarization block guarded on
        APPLE_ID + APPLE_TEAM_ID + APPLE_APP_SPECIFIC_PASSWORD, calling
        `xcrun notarytool submit` followed by `xcrun stapler staple`."""
        src = _read("build.sh")
        assert "xcrun notarytool submit" in src, (
            "build.sh must call `xcrun notarytool submit` to upload the "
            "DMG for Apple notarization when credentials are configured"
        )
        assert "xcrun stapler staple" in src, (
            "build.sh must call `xcrun stapler staple` after a successful "
            "notarytool submission so the ticket is attached to the DMG"
        )
        # The notarization block must be guarded on the three notarytool
        # credential env vars, otherwise builds without an Apple ID hard-fail.
        for var in ("APPLE_ID", "APPLE_TEAM_ID", "APPLE_APP_SPECIFIC_PASSWORD"):
            assert var in src, (
                f"build.sh notarization block must reference {var} so it "
                f"is gated on the full credential set"
            )

    def test_build_sh_stapler_failure_is_soft(self):
        """`xcrun stapler staple` must be wrapped in an `if/then/else`
        (or paired with `||`) so a transient stapler failure becomes a
        warning instead of aborting the build under `set -e`.

        Apple's stapler service can fail intermittently after a successful
        `notarytool submit` (CDN propagation lag, transient 5xx). A
        signed-and-notarized-but-unstapled DMG is still functional —
        Gatekeeper fetches the ticket online on first launch — so a
        stapler hiccup must not delete the macOS artifact from the
        release.
        """
        src = _read("build.sh")
        # Find the stapler invocation and check it is inside an `if` head
        # (i.e. `if xcrun stapler staple ...; then`) OR followed by `||`.
        # The simplest robust check: locate the line, then verify either
        # (a) it begins with `if ` after stripping leading whitespace, or
        # (b) it ends with ` || ...` style continuation.
        # Only inspect actual code lines — strip both whole-line bash comments
        # (`# …`) and inline trailing comments (`code # …`) before testing.
        stapler_lines = []
        for raw in src.splitlines():
            code = raw.split("#", 1)[0]
            if "xcrun stapler staple" in code:
                stapler_lines.append(code)
        assert stapler_lines, (
            "build.sh must call `xcrun stapler staple` (notarization step)"
        )
        for line in stapler_lines:
            stripped = line.strip()
            wrapped_in_if = stripped.startswith("if ") and stripped.endswith("; then")
            soft_or = "||" in stripped
            assert wrapped_in_if or soft_or, (
                "build.sh `xcrun stapler staple` invocation must be guarded "
                "(`if xcrun stapler staple ...; then ... else WARN ... fi` "
                "or `xcrun stapler staple ... || echo WARN`) so a transient "
                "stapler failure does not abort the build under `set -e` and "
                f"silently drop the macOS DMG. Offending line: {stripped!r}"
            )
