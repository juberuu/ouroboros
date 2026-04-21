"""release_sync.py — deterministic release-metadata carrier sync helpers.

Standalone library with NO wire-up into advisory or commit pipelines.
Integration (Commit B) is intentionally deferred.

Scope
-----
This library syncs the VERSION string across its three derived *carrier* files:
``pyproject.toml`` version field, ``README.md`` badge, and
``docs/ARCHITECTURE.md`` header.  It does NOT create or update the
``README.md`` Version History changelog row — that remains a manual authoring
surface because each release entry requires a human-written description.
``check_history_limit`` and ``detect_numeric_claims`` advise on the quality of
whatever row the author wrote, without modifying it.

Public API
----------
sync_release_metadata(repo_dir)  -> list[str]   changed carrier file paths
check_history_limit(readme_text) -> list[str]   advisory P7 limit warnings
detect_numeric_claims(text)      -> list[str]   matched numeric-claim strings
run_release_preflight(repo_dir)  -> (list[str], list[str])  (changed, warnings)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

# ---------------------------------------------------------------------------
# P7 history limits (BIBLE.md)
# ---------------------------------------------------------------------------
_MAX_MAJOR = 2
_MAX_MINOR = 5
_MAX_PATCH = 5

# Numeric-claim pattern: stand-alone integer followed by a release noun.
# Matches "16 tests", "3 fixes", "42 new tests", etc.
_NUMERIC_CLAIM_RE = re.compile(
    r'\b(\d+)\s+(?:new\s+)?(?:\w+\s+)?(?:tests?|fixes?|checks?|functions?|lines?|changes?|regressions?|assertions?)\b',
    re.IGNORECASE,
)

# Version row in README Version History: "| X.Y.Z | date | description |"
_VERSION_ROW_RE = re.compile(r'^\|\s*(\d+)\.(\d+)\.(\d+)\s*\|', re.MULTILINE)

# README badge: [![Version X.Y.Z](...)
_README_BADGE_RE = re.compile(
    r'(\[!\[Version\s+)([\d]+\.[\d]+\.[\d]+)(\]\(https://img\.shields\.io/badge/version-)'
    r'([\d]+\.[\d]+\.[\d]+)'
    r'(-green\.svg\)\])',
    re.IGNORECASE,
)

# ARCHITECTURE.md header: "# Ouroboros vX.Y.Z — ..."
_ARCH_HEADER_RE = re.compile(r'^(#\s+Ouroboros\s+v)([\d]+\.[\d]+\.[\d]+)(\s*)', re.MULTILINE)


def sync_release_metadata(repo_dir: str) -> List[str]:
    """Sync VERSION → pyproject.toml → README badge → ARCHITECTURE.md header.

    Reads the canonical version from the ``VERSION`` file and writes the
    correct value into the other three carriers when they are out of sync.

    Returns
    -------
    list[str]
        Repo-relative paths of files that were actually modified.
    """
    root = Path(repo_dir)
    version_file = root / "VERSION"
    if not version_file.exists():
        return []

    version = version_file.read_text(encoding="utf-8").strip()
    if not re.match(r'^\d+\.\d+\.\d+$', version):
        return []

    changed: List[str] = []

    # --- pyproject.toml ---
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        text = pyproject.read_text(encoding="utf-8")
        new_text = re.sub(
            r'^(version\s*=\s*")[^"]*(")',
            lambda m: f'{m.group(1)}{version}{m.group(2)}',
            text,
            flags=re.MULTILINE,
        )
        if new_text != text:
            pyproject.write_text(new_text, encoding="utf-8")
            changed.append("pyproject.toml")

    # --- README.md badge ---
    readme = root / "README.md"
    if readme.exists():
        text = readme.read_text(encoding="utf-8")
        new_text = _README_BADGE_RE.sub(
            lambda m: (
                m.group(1) + version + m.group(3) + version + m.group(5)
            ),
            text,
        )
        if new_text != text:
            readme.write_text(new_text, encoding="utf-8")
            changed.append("README.md")

    # --- docs/ARCHITECTURE.md header ---
    arch = root / "docs" / "ARCHITECTURE.md"
    if arch.exists():
        text = arch.read_text(encoding="utf-8")
        new_text = _ARCH_HEADER_RE.sub(
            lambda m: m.group(1) + version + m.group(3),
            text,
        )
        if new_text != text:
            arch.write_text(new_text, encoding="utf-8")
            changed.append("docs/ARCHITECTURE.md")

    return changed


def check_history_limit(readme_text: str) -> List[str]:
    """Return advisory warnings when Version History exceeds P7 limits.

    Limits: 2 major, 5 minor, 5 patch rows visible in the history table.
    Never raises — always returns a (possibly empty) list of warning strings.
    """
    warnings: List[str] = []
    major_rows, minor_rows, patch_rows = 0, 0, 0

    for m in _VERSION_ROW_RE.finditer(readme_text):
        _, min_, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if min_ == 0 and patch == 0:
            major_rows += 1
        elif patch == 0:
            minor_rows += 1
        else:
            patch_rows += 1

    if major_rows > _MAX_MAJOR:
        warnings.append(
            f"Version History has {major_rows} major rows (limit {_MAX_MAJOR}): "
            f"trim oldest major entries."
        )
    if minor_rows > _MAX_MINOR:
        warnings.append(
            f"Version History has {minor_rows} minor rows (limit {_MAX_MINOR}): "
            f"trim oldest minor entries."
        )
    if patch_rows > _MAX_PATCH:
        warnings.append(
            f"Version History has {patch_rows} patch rows (limit {_MAX_PATCH}): "
            f"trim oldest patch entries."
        )
    return warnings


def detect_numeric_claims(text: str) -> List[str]:
    """Return matched numeric-claim strings found in *text*.

    Examples: ``"16 tests"``, ``"3 new fixes"``, ``"42 regression tests"``.
    Advisory only — callers decide how to surface these.
    """
    return [m.group(0) for m in _NUMERIC_CLAIM_RE.finditer(text)]


def run_release_preflight(repo_dir: str) -> Tuple[List[str], List[str]]:
    """Run all release-sync checks and return (changed_files, advisory_warnings).

    1. Sync VERSION carriers (pyproject.toml, README badge, ARCHITECTURE header).
    2. Check Version History limits.
    3. Detect numeric claims in the current README changelog row for the new version.

    This function is idempotent: running it twice in a row produces no further
    changes on the second call (assuming no external modifications between calls).

    Returns
    -------
    (changed_files, advisory_warnings)
        *changed_files* — repo-relative paths actually written.
        *advisory_warnings* — non-blocking strings describing policy violations.
    """
    changed = sync_release_metadata(repo_dir)

    warnings: List[str] = []
    readme = Path(repo_dir) / "README.md"
    if readme.exists():
        readme_text = readme.read_text(encoding="utf-8")
        warnings.extend(check_history_limit(readme_text))

        # Find the changelog row for the current VERSION and flag numeric claims.
        version_file = Path(repo_dir) / "VERSION"
        if version_file.exists():
            version = version_file.read_text(encoding="utf-8").strip()
            row_re = re.compile(
                r'^\|\s*' + re.escape(version) + r'\s*\|[^|]*\|([^|]+)\|?\s*$',
                re.MULTILINE,
            )
            m = row_re.search(readme_text)
            if m:
                claims = detect_numeric_claims(m.group(1))
                if claims:
                    warnings.append(
                        f"Changelog row for {version} contains numeric claims that "
                        f"may become stale: {claims!r}. Consider replacing with "
                        f"descriptive language."
                    )

    return changed, warnings
