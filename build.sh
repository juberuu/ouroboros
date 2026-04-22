#!/bin/bash
set -e

SIGN_IDENTITY="Developer ID Application: Ian Mironov (WHY6PAKA5V)"
NOTARYTOOL_PROFILE="ouroboros-notarize"
ENTITLEMENTS="entitlements.plist"
SIGN_MODE="${OUROBOROS_SIGN:-1}"

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

echo ""
echo "=== Done ==="
if [ "$SIGN_MODE" != "0" ]; then
    echo "Signed app: $APP_PATH"
    echo "Signed DMG: $DMG_PATH"
else
    echo "Unsigned app: $APP_PATH"
    echo "Unsigned DMG: $DMG_PATH"
fi
echo "(Not notarized — users need right-click → Open on first launch)"
