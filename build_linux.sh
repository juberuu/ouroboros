#!/bin/bash
set -e

VERSION=$(tr -d '[:space:]' < VERSION)
ARCHIVE_NAME="Ouroboros-${VERSION}-linux-$(uname -m).tar.gz"

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
