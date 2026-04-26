#!/bin/bash
set -e

VERSION=$(tr -d '[:space:]' < VERSION)
ARCHIVE_NAME="Ouroboros-${VERSION}-linux-$(uname -m).tar.gz"
MANAGED_SOURCE_BRANCH="${OUROBOROS_MANAGED_SOURCE_BRANCH:-ouroboros}"
RELEASE_TAG="v${VERSION}"

PYTHON_CMD="${PYTHON_CMD:-python3}"
if ! command -v "$PYTHON_CMD" >/dev/null 2>&1; then
    PYTHON_CMD=python
fi

echo "=== Building Ouroboros for Linux (v${VERSION}) ==="

if [ ! -f "python-standalone/bin/python3" ]; then
    echo "ERROR: python-standalone/ not found."
    echo "Run first: bash scripts/download_python_standalone.sh"
    exit 1
fi

echo "--- Installing launcher dependencies ---"
"$PYTHON_CMD" -m pip install -q -r requirements-launcher.txt

echo "--- Installing agent dependencies into python-standalone ---"
python-standalone/bin/pip3 install -q -r requirements.txt

rm -rf build dist

export PYINSTALLER_CONFIG_DIR="$PWD/.pyinstaller-cache"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

echo "--- Installing Chromium for browser tools (bundled into python-standalone) ---"
PLAYWRIGHT_BROWSERS_PATH=0 python-standalone/bin/python3 -m playwright install chromium

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
"$PYTHON_CMD" scripts/build_repo_bundle.py --source-branch "$MANAGED_SOURCE_BRANCH"

echo "--- Running PyInstaller ---"
"$PYTHON_CMD" -m PyInstaller Ouroboros.spec --clean --noconfirm

echo ""
echo "=== Creating archive ==="
cd dist
tar -czf "$ARCHIVE_NAME" Ouroboros/
cd ..

echo ""
echo "=== Done ==="
echo "Archive: dist/$ARCHIVE_NAME"
echo ""
echo "To run: extract and execute ./Ouroboros/Ouroboros"
