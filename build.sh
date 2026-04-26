#!/bin/bash
set -e

# Signing identity: env override wins so CI runners (which import a temporary
# Developer ID via `security import` + `security set-key-partition-list`) can
# point at whatever identity matches their imported certificate without
# editing this file. Local dev keeps the historical default identity.
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: Ian Mironov (WHY6PAKA5V)}"
ENTITLEMENTS="entitlements.plist"
SIGN_MODE="${OUROBOROS_SIGN:-1}"
MANAGED_SOURCE_BRANCH="${OUROBOROS_MANAGED_SOURCE_BRANCH:-ouroboros}"
RELEASE_TAG="v$(tr -d '[:space:]' < VERSION)"

APP_PATH="dist/Ouroboros.app"
DMG_NAME="Ouroboros-$(cat VERSION | tr -d '[:space:]').dmg"
DMG_PATH="dist/$DMG_NAME"

echo "=== Building Ouroboros.app ==="

if [ ! -f "python-standalone/bin/python3" ]; then
    echo "ERROR: python-standalone/ not found."
    echo "Run first: bash scripts/download_python_standalone.sh"
    exit 1
fi

echo "--- Installing launcher dependencies ---"
pip install -q -r requirements-launcher.txt

echo "--- Installing agent dependencies into python-standalone ---"
python-standalone/bin/pip3 install -q -r requirements.txt

echo "--- Installing Chromium for browser tools (bundled into python-standalone) ---"
# macOS bundles only the headless shell; the full Chromium app bundle trips
# PyInstaller's nested-bundle codesign path on arm64 runners.
PLAYWRIGHT_BROWSERS_PATH=0 python-standalone/bin/python3 -m playwright install --only-shell chromium

echo "--- Normalizing python-standalone symlinks for PyInstaller ---"
python3 - <<'PY'
import pathlib
import shutil

root = pathlib.Path("python-standalone")
replaced = 0
skipped = 0


def _should_skip_symlink(path: pathlib.Path) -> bool:
    # Keep bundled Playwright browser app/framework trees intact on macOS.
    # Flattening symlinks inside these nested bundles breaks codesign later.
    parts = path.parts
    return (
        ".local-browsers" in parts
        or any(part.endswith(".app") or part.endswith(".framework") for part in parts)
    )

for path in sorted(root.rglob("*")):
    if not path.is_symlink():
        continue
    if _should_skip_symlink(path):
        skipped += 1
        continue
    target = path.resolve()
    path.unlink()
    if target.is_dir():
        shutil.copytree(target, path)
    else:
        shutil.copy2(target, path)
    replaced += 1

print(
    f"Replaced {replaced} symlinks in python-standalone "
    f"(skipped {skipped} inside bundled browser bundles)"
)
PY

echo "--- Building embedded managed repo bundle ---"
if ! git rev-parse -q --verify "refs/tags/$RELEASE_TAG" >/dev/null 2>&1; then
    echo "ERROR: packaging requires git tag $RELEASE_TAG to exist."
    exit 1
fi
TAG_TYPE="$(git cat-file -t "refs/tags/$RELEASE_TAG" 2>/dev/null || true)"
if [ "$TAG_TYPE" != "tag" ]; then
    echo "ERROR: packaging requires annotated git tag $RELEASE_TAG (got '$TAG_TYPE'). Recreate with: git tag -a $RELEASE_TAG -m 'Release $RELEASE_TAG'"
    exit 1
fi
if ! git tag --points-at HEAD | grep -Fx "$RELEASE_TAG" >/dev/null 2>&1; then
    echo "ERROR: packaging requires HEAD to be tagged with $RELEASE_TAG."
    exit 1
fi
python3 scripts/build_repo_bundle.py --source-branch "$MANAGED_SOURCE_BRANCH"

rm -rf build dist

echo "--- Running PyInstaller ---"
python3 -m PyInstaller Ouroboros.spec --clean --noconfirm

if [ "$SIGN_MODE" != "0" ]; then
    echo ""
    echo "=== Signing Ouroboros.app ==="

    echo "--- Finding and signing all Mach-O binaries ---"
    find "$APP_PATH" -type f | while read -r f; do
        if file "$f" | grep -q "Mach-O"; then
            codesign -s "$SIGN_IDENTITY" --timestamp --force --options runtime \
                --entitlements "$ENTITLEMENTS" "$f" 2>&1 || true
        fi
    done
    echo "Signed embedded binaries"

    echo "--- Signing the app bundle ---"
    codesign -s "$SIGN_IDENTITY" --timestamp --force --options runtime \
        --entitlements "$ENTITLEMENTS" "$APP_PATH"

    echo "--- Verifying signature ---"
    codesign -dvv "$APP_PATH"
    codesign --verify --strict "$APP_PATH"
    echo "Signature OK"
else
    echo ""
    echo "=== Skipping signing (OUROBOROS_SIGN=0) ==="
fi

echo ""
echo "=== Creating DMG ==="
hdiutil create -volname Ouroboros -srcfolder "$APP_PATH" -ov -format UDZO "$DMG_PATH"

if [ "$SIGN_MODE" != "0" ]; then
    codesign -s "$SIGN_IDENTITY" --timestamp "$DMG_PATH"
fi

# Optional notarization: only fires when codesign already ran AND the three
# notarytool credentials are present in env. This way unsigned builds skip
# the whole notarization path, and signed-but-unconfigured builds (no Apple
# ID configured for notarization) still ship cleanly — they just need
# right-click → Open on first launch on receiver machines.
#
# A single enum tracks the outcome so the final summary cascade can
# distinguish all four cases without contradiction:
#   * success         — notarytool submit AND stapler staple both succeeded
#   * staple_failed   — notarytool submit OK; stapler failed (Gatekeeper
#                       fetches the ticket online; DMG is genuinely notarized)
#   * submit_failed   — notarytool submit failed (DMG is signed only)
#   * unconfigured    — Apple credentials not set (signed-only OR unsigned)
NOTARIZE_OUTCOME="unconfigured"
if [ "$SIGN_MODE" != "0" ] \
        && [ -n "${APPLE_ID:-}" ] \
        && [ -n "${APPLE_TEAM_ID:-}" ] \
        && [ -n "${APPLE_APP_SPECIFIC_PASSWORD:-}" ]; then
    echo ""
    echo "=== Notarizing DMG (Apple ID: $APPLE_ID) ==="
    # `--wait` blocks until Apple finishes the notarization scan so the
    # subsequent `xcrun stapler staple` always operates on a finalized ticket.
    # A submit failure is treated as a soft warning (DMG is signed; release
    # ships with a clear log line) rather than a hard build abort, so an
    # Apple-side outage / wrong-credential typo never silently drops the
    # macOS artifact from the GitHub Release.
    if xcrun notarytool submit "$DMG_PATH" \
            --apple-id "$APPLE_ID" \
            --team-id "$APPLE_TEAM_ID" \
            --password "$APPLE_APP_SPECIFIC_PASSWORD" \
            --wait; then
        echo "--- Stapling notarization ticket ---"
        # Stapler hits Apple's CDN separately and can fail transiently after
        # a successful notarytool submission. Treat that as a soft warning
        # too: the DMG is still signed + notarized, Gatekeeper will fetch
        # the ticket online on first launch (slower but functional). Without
        # this guard `set -e` would abort the script.
        if xcrun stapler staple "$DMG_PATH"; then
            NOTARIZE_OUTCOME="success"
        else
            NOTARIZE_OUTCOME="staple_failed"
            echo "WARNING: stapler staple failed — DMG is notarized but ticket not embedded; receivers may briefly need right-click → Open until Apple's ticket propagates."
        fi
    else
        NOTARIZE_OUTCOME="submit_failed"
        echo "WARNING: notarytool submit failed — DMG is signed but not notarized; verify APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD are correct or check the notarytool log above."
    fi
fi

echo ""
echo "=== Done ==="
if [ "$SIGN_MODE" != "0" ]; then
    echo "Signed app: $APP_PATH"
    echo "Signed DMG: $DMG_PATH"
else
    echo "Unsigned app: $APP_PATH"
    echo "Unsigned DMG: $DMG_PATH"
fi
case "$NOTARIZE_OUTCOME" in
    success)
        echo "(Notarized + stapled — no right-click → Open required on first launch)"
        ;;
    staple_failed)
        echo "(Notarized but ticket not stapled — Gatekeeper will fetch the ticket online; receivers need internet on first launch)"
        ;;
    submit_failed)
        echo "(Signed but notarytool submit failed — DMG was not accepted by Apple; check the WARNING above for details)"
        ;;
    unconfigured)
        if [ "$SIGN_MODE" != "0" ]; then
            echo "(Signed but not notarized — set APPLE_ID / APPLE_TEAM_ID / APPLE_APP_SPECIFIC_PASSWORD to enable notarization)"
        else
            echo "(Not notarized — users need right-click → Open on first launch)"
        fi
        ;;
    *)
        # Defensive default: a future enum value added to NOTARIZE_OUTCOME
        # without a matching arm would otherwise silently print no summary
        # line. Surface it loudly so the bug is easy to find.
        echo "(Unknown notarization outcome: '$NOTARIZE_OUTCOME' — please report; likely a missing case arm in build.sh)"
        ;;
esac
