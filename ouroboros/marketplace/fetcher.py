"""Staging-area download + verify helpers for the ClawHub marketplace.

Workflow:

1. :func:`stage` is given an :class:`ouroboros.marketplace.clawhub.ClawHubArchive`
   plus a target staging directory. It extracts the zip into a fresh
   temporary subdirectory and verifies size / content-type / file
   policy invariants before returning a :class:`StagedSkill` handle.
2. The caller (typically :mod:`ouroboros.marketplace.install`) then
   passes the handle to :mod:`ouroboros.marketplace.adapter` for
   frontmatter translation, and finally to ``shutil.move`` to land the
   staged tree at ``data/skills/clawhub/<owner>__<slug>/``.

Hard policy guards (every single one fails CLOSED — return value
indicates the reject reason; nothing is silently skipped):

- Total uncompressed size <= 50 MB.
- File count <= 200 (mirrors registry's ~40 review-pack cap with
  generous margin for assets/).
- Per-file extension allowlist: every text-y extension permitted
  by ClawHub plus the loadable-binary denylist from
  :mod:`ouroboros.skill_review`.
- Sensitive filename refuse: ``.env`` / ``credentials.json`` / ``*.pem``
  etc. (defers to :mod:`ouroboros.tools.review_helpers`).
- Path traversal refuse: any zip member with ``..`` segments or an
  absolute path component is treated as malicious and aborts.
- Symlink refuse: zip members that try to inject symlinks are aborted.
"""

from __future__ import annotations

import hashlib
import io
import logging
import pathlib
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)


_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MB total uncompressed
_MAX_FILE_COUNT = 200
_MAX_PER_FILE_BYTES = 8 * 1024 * 1024  # 8 MB per individual file

# Allowed text extensions. Mirrors the ClawHub publish allowlist
# (https://github.com/openclaw/clawhub/blob/main/docs/skill-format.md
# `TEXT_FILE_EXTENSIONS`) plus a few common safe assets like images.
_ALLOWED_EXTENSIONS = frozenset(
    {
        ".md", ".markdown", ".txt", ".rst", ".org",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
        ".py", ".sh", ".bash", ".zsh",
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
        ".html", ".htm", ".css", ".scss", ".sass",
        ".svg", ".csv", ".tsv",
        ".sql", ".graphql", ".gql",
        ".lock", ".license",
        # Inert media assets — opt-in. Adapter still refuses anything
        # that could be loaded as code (.so/.dll/.wasm/etc.) regardless.
        ".png", ".jpg", ".jpeg", ".gif", ".webp",
        # Dotfiles without an extension show up below by basename.
    }
)

_ALLOWED_BARE_BASENAMES = frozenset(
    {
        "LICENSE", "COPYING", "NOTICE", "README", "CHANGELOG",
        "AUTHORS", "CONTRIBUTORS", "AGENTS",
        ".gitignore", ".npmignore", ".editorconfig", ".gitattributes",
        ".eslintrc", ".prettierrc", ".nvmrc",
    }
)


class FetchError(RuntimeError):
    """Raised on any policy violation during stage-time validation."""


@dataclass
class StagedSkill:
    """Outcome of a successful :func:`stage` call.

    ``staging_dir`` is the temporary directory the archive was
    extracted into; the caller is responsible for either moving it to
    the final destination via :mod:`ouroboros.marketplace.install` or
    calling :meth:`cleanup` on failure.
    """

    slug: str
    version: str
    sha256: str
    staging_dir: pathlib.Path
    file_count: int = 0
    total_bytes: int = 0
    file_list: List[str] = field(default_factory=list)
    has_skill_md: bool = False
    has_plugin_manifest: bool = False

    def cleanup(self) -> None:
        """Best-effort removal of the staging tree."""
        try:
            if self.staging_dir.exists():
                shutil.rmtree(self.staging_dir, ignore_errors=True)
        except OSError:
            log.warning(
                "Failed to clean up staging dir %s", self.staging_dir, exc_info=True
            )


def _is_sensitive(path: pathlib.PurePosixPath) -> bool:
    """Defer to :mod:`ouroboros.tools.review_helpers` for the deny check.

    Centralising via that module keeps the marketplace honest with the
    same denylist that the existing skill review pipeline enforces, so
    a sensitive-shape filename cannot enter the data plane via the
    marketplace and dodge later review.
    """
    try:
        from ouroboros.tools.review_helpers import (
            _SENSITIVE_EXTENSIONS,
            _SENSITIVE_NAMES,
        )
    except Exception:  # pragma: no cover — defensive, the module exists
        return False
    name_lower = path.name.lower()
    if name_lower in _SENSITIVE_NAMES:
        return True
    return any(name_lower.endswith(ext) for ext in _SENSITIVE_EXTENSIONS)


def _is_loadable_binary(path: pathlib.PurePosixPath) -> bool:
    try:
        from ouroboros.skill_review import _LOADABLE_BINARY_EXTENSIONS
    except Exception:  # pragma: no cover
        return False
    name_lower = path.name.lower()
    return any(name_lower.endswith(ext) for ext in _LOADABLE_BINARY_EXTENSIONS)


def _validate_member_path(name: str) -> pathlib.PurePosixPath:
    """Normalise + reject path-traversal / absolute zip members.

    The zip spec allows arbitrary names; a malicious archive can use
    ``../etc/passwd`` or ``/etc/passwd`` to land files outside the
    target directory. We reject any such member up-front.
    """
    cleaned = name.replace("\\", "/").lstrip("/")
    if not cleaned:
        raise FetchError(f"Archive member has empty path: {name!r}")
    posix = pathlib.PurePosixPath(cleaned)
    if posix.is_absolute() or posix.anchor:
        raise FetchError(f"Archive member uses absolute path: {name!r}")
    parts = posix.parts
    if any(part == ".." for part in parts):
        raise FetchError(f"Archive member uses '..' traversal: {name!r}")
    if any(part.startswith("/") for part in parts):
        raise FetchError(f"Archive member has malformed segment: {name!r}")
    return posix


def _classify_member(member: zipfile.ZipInfo) -> str:
    """Return ``"file"`` / ``"dir"`` / ``"symlink"`` / ``"other"``.

    Symlinks (``S_IFLNK`` in the high bits of ``external_attr``) are
    rejected outright — a symlinked zip member that resolves outside
    the staging dir would let a malicious archive plant arbitrary
    paths. Directories are recorded but never extracted as data; their
    sub-files create the directory structure on demand.
    """
    is_dir = member.is_dir() or member.filename.endswith("/")
    if is_dir:
        return "dir"
    # zipfile uses the high 16 bits of external_attr to store unix mode
    mode = (member.external_attr >> 16) & 0xFFFF
    if mode and (mode & 0xF000) == 0xA000:  # S_IFLNK
        return "symlink"
    return "file"


def _extension_allowed(path: pathlib.PurePosixPath) -> bool:
    name_lower = path.name.lower()
    if path.name in _ALLOWED_BARE_BASENAMES or name_lower in {
        bn.lower() for bn in _ALLOWED_BARE_BASENAMES
    }:
        return True
    suffix = path.suffix.lower()
    return suffix in _ALLOWED_EXTENSIONS


def stage(
    archive_bytes: bytes,
    *,
    slug: str,
    version: str = "",
    expected_sha256: Optional[str] = None,
    staging_root: Optional[pathlib.Path] = None,
) -> StagedSkill:
    """Validate + extract an archive into a private staging directory.

    Returns a :class:`StagedSkill` on success. Raises :class:`FetchError`
    with a descriptive message on any policy violation; in that case
    the staging tree (if it was partially created) is cleaned up before
    the exception propagates.
    """
    if not isinstance(archive_bytes, (bytes, bytearray)):
        raise FetchError(f"archive_bytes must be bytes, got {type(archive_bytes).__name__}")
    if not archive_bytes:
        raise FetchError("archive_bytes is empty")
    if len(archive_bytes) > _MAX_TOTAL_BYTES:
        raise FetchError(
            f"Archive size {len(archive_bytes)} bytes exceeds {_MAX_TOTAL_BYTES} cap"
        )

    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    if expected_sha256 and expected_sha256.strip() and expected_sha256 != actual_sha:
        raise FetchError(
            f"Archive sha256 mismatch: expected {expected_sha256}, got {actual_sha}"
        )

    if staging_root is None:
        # Use ``tempfile.mkdtemp`` per call rather than a shared
        # ``ouroboros_marketplace_staging`` directory so a local
        # attacker cannot pre-create the staging root with malicious
        # permissions or as a symlink. ``mkdtemp`` returns a freshly
        # created mode-0700 directory owned by the calling user.
        staging_dir = pathlib.Path(
            tempfile.mkdtemp(
                prefix=f"ouroboros_marketplace_{slug.replace('/', '__')}_"
            )
        )
    else:
        try:
            staging_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FetchError(f"Cannot create staging root {staging_root}: {exc}") from exc
        staging_dir = staging_root / (
            f"{slug.replace('/', '__')}-{actual_sha[:12]}-{uuid.uuid4().hex[:8]}"
        )
        staging_dir.mkdir(parents=True, exist_ok=False)

    file_list: List[str] = []
    total_bytes = 0
    file_count = 0
    has_skill_md = False
    has_plugin_manifest = False

    try:
        try:
            with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as zf:
                members = zf.infolist()
                if not members:
                    raise FetchError("Archive contains no entries")
                if len(members) > _MAX_FILE_COUNT * 2:  # allow some directory entries
                    raise FetchError(
                        f"Archive has {len(members)} entries (cap is "
                        f"{_MAX_FILE_COUNT * 2} including directories)"
                    )
                # Many ClawHub archives wrap content in a top-level
                # ``<slug>/`` directory. We strip that prefix so the
                # staging dir mirrors the on-disk skill layout (SKILL.md
                # at the root, scripts/ as a child, etc.). Detect it
                # by checking whether every non-empty member shares a
                # common first path segment.
                stripped_prefix = _common_top_prefix(members)
                for member in members:
                    classification = _classify_member(member)
                    if classification == "dir":
                        continue
                    if classification == "symlink":
                        raise FetchError(
                            f"Archive member {member.filename!r} is a symlink (rejected)"
                        )
                    rel_path = _validate_member_path(member.filename)
                    if stripped_prefix:
                        parts = rel_path.parts
                        if parts and parts[0] == stripped_prefix:
                            rel_path = pathlib.PurePosixPath(*parts[1:])
                            if not rel_path.parts:
                                continue
                    if _is_sensitive(rel_path):
                        raise FetchError(
                            f"Archive contains sensitive-shape filename {rel_path}"
                        )
                    if _is_loadable_binary(rel_path):
                        raise FetchError(
                            f"Archive contains loadable-binary file {rel_path} "
                            "(.so/.dll/.wasm/.pyc/.exe etc. are not permitted)"
                        )
                    if not _extension_allowed(rel_path):
                        raise FetchError(
                            f"Archive contains disallowed extension: {rel_path}"
                        )
                    if member.file_size > _MAX_PER_FILE_BYTES:
                        raise FetchError(
                            f"Archive member {rel_path} is "
                            f"{member.file_size} bytes (cap {_MAX_PER_FILE_BYTES})"
                        )
                    file_count += 1
                    if file_count > _MAX_FILE_COUNT:
                        raise FetchError(
                            f"Archive exceeds file count cap {_MAX_FILE_COUNT}"
                        )
                    target_path = staging_dir / pathlib.Path(*rel_path.parts)
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member, "r") as src:
                        # v4.50 cycle-2 GPT-critic finding: zip-bomb
                        # defence — ``member.file_size`` is publisher-
                        # controlled metadata in the central
                        # directory; a forged CD with `file_size=100`
                        # can still decompress to 50 GB. Bound the
                        # read buffer so the peak working set stays
                        # under the per-file cap regardless of
                        # forged metadata. We read cap+1 bytes; if
                        # we got back the cap+1 we know the actual
                        # decompressed size exceeds the limit.
                        data = src.read(_MAX_PER_FILE_BYTES + 1)
                    if len(data) > _MAX_PER_FILE_BYTES:
                        raise FetchError(
                            f"Archive member {rel_path} actual size "
                            f"> cap {_MAX_PER_FILE_BYTES} (forged file_size header?)"
                        )
                    if len(data) != member.file_size:
                        # Compressed archives may legitimately differ
                        # from declared file_size; recheck the limit
                        # on the actual bytes too. Above check already
                        # bounded by the read cap.
                        if len(data) > _MAX_PER_FILE_BYTES:
                            raise FetchError(
                                f"Archive member {rel_path} actual size "
                                f"{len(data)} > cap {_MAX_PER_FILE_BYTES}"
                            )
                    total_bytes += len(data)
                    if total_bytes > _MAX_TOTAL_BYTES:
                        raise FetchError(
                            f"Archive uncompressed size exceeds {_MAX_TOTAL_BYTES} bytes"
                        )
                    target_path.write_bytes(data)
                    file_list.append(rel_path.as_posix())
                    if rel_path.name in ("SKILL.md", "skill.json"):
                        has_skill_md = True
                    if rel_path.name == "openclaw.plugin.json":
                        has_plugin_manifest = True
        except zipfile.BadZipFile as exc:
            raise FetchError(f"Archive is not a valid zip: {exc}") from exc

        if not has_skill_md:
            raise FetchError(
                "Archive does not contain SKILL.md / skill.json — not a recognisable skill package"
            )

        return StagedSkill(
            slug=slug,
            version=(version or "").strip(),
            sha256=actual_sha,
            staging_dir=staging_dir,
            file_count=file_count,
            total_bytes=total_bytes,
            file_list=sorted(file_list),
            has_skill_md=has_skill_md,
            has_plugin_manifest=has_plugin_manifest,
        )
    except Exception:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise


def _common_top_prefix(members: List[zipfile.ZipInfo]) -> str:
    """Return a single common top-level directory name, or '' if there is none."""
    prefixes: set[str] = set()
    for member in members:
        cleaned = member.filename.replace("\\", "/").lstrip("/")
        if not cleaned:
            continue
        head, _, _ = cleaned.partition("/")
        if not head or head == cleaned:
            return ""  # at least one entry is a top-level file -> no wrapper dir
        prefixes.add(head)
        if len(prefixes) > 1:
            return ""
    if len(prefixes) == 1:
        return next(iter(prefixes))
    return ""


__all__ = [
    "FetchError",
    "StagedSkill",
    "stage",
]
