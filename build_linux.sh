#!/bin/bash
set -e

VERSION=$(tr -d '[:space:]' < VERSION)
ARCHIVE_NAME="Ouroboros-${VERSION}-linux-$(uname -m).tar.gz"
MANAGED_SOURCE_BRANCH="${OUROBOROS_MANAGED_SOURCE_BRANCH:-ouroboros}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-${TMPDIR:-/tmp}/ouroboros-build-pycache}"
mkdir -p "$PYTHONPYCACHEPREFIX"

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

# Bundle the official Node.js runtime so node-runtime skills work in the
# packaged app out of the box.
if [ ! -f "node-standalone/bin/node" ]; then
    echo "--- Downloading bundled Node.js runtime ---"
    bash scripts/download_node_standalone.sh
fi

if [ ! -f "ripgrep-standalone/bin/rg" ]; then
    echo "--- Downloading bundled ripgrep runtime ---"
    bash scripts/download_ripgrep_standalone.sh
fi

echo "--- Installing launcher dependencies ---"
"$PYTHON_CMD" -m pip install -q -r requirements-launcher.txt

echo "--- Installing agent dependencies into python-standalone ---"
python-standalone/bin/pip3 install -q -r requirements.txt

rm -rf build dist

export PYINSTALLER_CONFIG_DIR="$PWD/.pyinstaller-cache"
mkdir -p "$PYINSTALLER_CONFIG_DIR"

echo "--- Installing Chromium/WebKit for browser tools (bundled into python-standalone) ---"
python-standalone/bin/python3 -m playwright install-deps chromium webkit
PLAYWRIGHT_BROWSERS_PATH=0 python-standalone/bin/python3 -m playwright install chromium webkit

echo "--- Building embedded managed repo bundle ---"
"$PYTHON_CMD" scripts/build_repo_bundle.py --source-branch "$MANAGED_SOURCE_BRANCH"

echo "--- Running PyInstaller ---"
"$PYTHON_CMD" -m PyInstaller Ouroboros.spec --clean --noconfirm

echo "--- Installing packaged CLI wrappers ---"
mkdir -p dist/Ouroboros/bin
cp packaging/cli/ouroboros dist/Ouroboros/bin/ouroboros
cp packaging/cli/install-ouroboros-cli dist/Ouroboros/bin/install-ouroboros-cli
chmod +x dist/Ouroboros/bin/ouroboros dist/Ouroboros/bin/install-ouroboros-cli

# WA6 parity: precompile bytecode instead of deleting it. Linux has no codesign
# seal, so this is purely for start-speed + consistency with the macOS build (where
# precompiled+sealed .pyc keep the signature valid). --invalidation-mode
# unchecked-hash means a read-only payload never rewrites the .pyc at import.
echo "--- Precompiling Python bytecode in archive payload (start-speed parity) ---"
APP_EMBEDDED_PY="$(find dist/Ouroboros -type f -path '*/python-standalone/bin/python3' 2>/dev/null | head -1)"
if [ -z "$APP_EMBEDDED_PY" ]; then
    APP_EMBEDDED_PY="$PWD/python-standalone/bin/python3"
fi
echo "Using embedded interpreter for compileall: $APP_EMBEDDED_PY"
COMPILE_TARGETS=()
while IFS= read -r d; do
    [ -n "$d" ] && COMPILE_TARGETS+=("$d")
done < <(find dist/Ouroboros -type d \( -path '*/python-standalone' -o -name ouroboros \) 2>/dev/null)
if [ "${#COMPILE_TARGETS[@]}" -gt 0 ]; then
    # Neutralize the build-time PYTHONDONTWRITEBYTECODE=1 + PYTHONPYCACHEPREFIX for
    # THIS command only, else compileall writes no in-tree .pyc (start-speed parity).
    env -u PYTHONDONTWRITEBYTECODE -u PYTHONPYCACHEPREFIX \
        "$APP_EMBEDDED_PY" -m compileall -q -f --invalidation-mode unchecked-hash "${COMPILE_TARGETS[@]}" || true
else
    echo "WARNING: no compileall targets found in dist/Ouroboros (python-standalone / ouroboros)."
fi

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
echo "To install CLI: ./Ouroboros/bin/install-ouroboros-cli"
