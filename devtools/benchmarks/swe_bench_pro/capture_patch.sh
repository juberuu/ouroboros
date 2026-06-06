#!/usr/bin/env bash
# Capture a SWE-bench Pro model_patch from the task repository.
#
# Patch capture determines what the official evaluator sees. A plain
# `git diff BASE` misses new untracked files, while an unfiltered `git add -A`
# captures runtime junk such as Redis dumps, node_modules, and compiled
# binaries. This helper follows the SWE-agent/mini-swe-agent reference shape,
# then removes environment artifacts and binary blobs.
#
# Usage:
#   ./capture_patch.sh <REPO_DIR> <BASE_COMMIT> <OUT.diff>
#
# The agent is expected to have already edited <REPO_DIR>. <BASE_COMMIT> is the
# task base commit from the dataset.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd -P)"
WORK="${1:?usage: capture_patch.sh <REPO_DIR> <BASE_COMMIT> <OUT.diff>}"
BASE="${2:?base_commit is required}"
OUT="${3:?output path is required and must be outside the Ouroboros repo}"
OUT_ABS="$(python3 - "$OUT" <<'PY'
import pathlib
import sys

print(pathlib.Path(sys.argv[1]).expanduser().resolve(strict=False))
PY
)"
case "$OUT_ABS" in
  "$REPO_ROOT"|"$REPO_ROOT"/*)
    echo "output path must be outside the Ouroboros repo: $OUT_ABS" >&2
    exit 2
    ;;
esac
OUT_DIR_ABS="$(dirname "$OUT_ABS")"
mkdir -p "$OUT_DIR_ABS"
STATUS_OUT="${OUT_ABS%.diff}.status.txt"

git -C "$WORK" rev-parse --verify "$BASE^{commit}" >/dev/null
cleanup() {
  git -C "$WORK" reset -q >/dev/null 2>&1 || true
}
trap cleanup EXIT

# (1) Include newly created source files. Several real Pro fixes add files, and
# a clean `git diff BASE` would omit them.
git -C "$WORK" add -A

# Keep a status snapshot for mismatch debugging: M=modified, A=added,
# ??=untracked.
git -C "$WORK" status --porcelain >"$STATUS_OUT"

# (2) Drop environment artifacts. These patterns were chosen to avoid broad
# SWE-agent defaults such as *.cfg/*.toml/setup.py/*.lock, which can remove real
# Pro fixes.
JUNK_RE='appendonlydir|\.rdb$|\.aof$|\.manifest$|\.log$|\.tmp$|\.pid$|\.sock$|(^|/)node_modules/|__pycache__|\.pyc$|\.pyo$|\.pytest_cache|\.ruff_cache|\.mypy_cache|/\.cache/|(^|/)dist/|(^|/)build/|\.DS_Store|(^|/)\.coverage$|coverage\.xml$|/htmlcov/'
while IFS= read -r f; do
  git -C "$WORK" reset -q -- "$f" 2>/dev/null
done < <(git -C "$WORK" diff --cached --name-only "$BASE" | grep -E "$JUNK_RE" || true)

# (3) Drop binary blobs. `git diff --cached --numstat` prints
# "-\t-\t<file>" for binary files. Text source additions remain included.
git -C "$WORK" diff --cached --numstat "$BASE" | awk -F'\t' '$1=="-" && $2=="-" {print $3}' | while IFS= read -r f; do
  [ -n "$f" ] && git -C "$WORK" reset -q -- "$f" 2>/dev/null
done

# (4) Emit final model_patch and restore the index without touching the working
# tree.
git -C "$WORK" diff --cached --binary "$BASE" >"$OUT_ABS"

echo "patch -> $OUT_ABS ($(wc -c <"$OUT_ABS" 2>/dev/null || echo 0)B, files: $(grep -cE '^diff --git' "$OUT_ABS" 2>/dev/null || echo 0))" >&2
