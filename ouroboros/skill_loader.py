"""External skill discovery, state, and review-status tracking (Phase 3).

Reads skills from the local checkout path configured in settings via
``OUROBOROS_SKILLS_REPO_PATH`` (see ``ouroboros.config.get_skills_repo_path``).
A skill is a directory containing either ``SKILL.md`` (with YAML frontmatter)
or ``skill.json``. The manifest schema lives in
``ouroboros.contracts.skill_manifest``; this module is the runtime-side
loader + state tracker on top of that frozen contract.

Per-skill state — the enabled bit, the most recent review verdict, and a
content hash used to invalidate stale reviews — is stored durably in
``~/Ouroboros/data/state/skills/<name>/`` so it survives restarts and lives
on the same plane as other durable state (``state.json``,
``advisory_review.json``). The layout:

- ``enabled.json`` — ``{"enabled": bool, "updated_at": iso_ts}``.
- ``review.json``  — ``{"content_hash": str, "status": "pass"|"fail"|"advisory"|"pending"|"pending_phase4",
  "findings": [...], "reviewer_models": [...], "timestamp": iso_ts,
  "prompt_chars": int, "cost_usd": float, "raw_result": str}``.
  ``pending_phase4`` is reserved for ``type: extension`` skills (execution
  deferred until Phase 4); the loader overlays this status on all
  extension skills regardless of persisted verdict so the Phase 3
  catalogue cannot mislead operators into thinking an extension is
  runnable. ``raw_result`` carries the truncated top-level review
  response for replay/debugging (capped via ``_truncate_raw_result`` in
  ``ouroboros.skill_review`` with an explicit OMISSION NOTE on overflow).

Neither file is required on disk — missing files mean "defaults". The module
treats absent state as: ``enabled=False``, ``review.status="pending"``.

Phase 3 scope: ``type: instruction`` and ``type: script`` are surfaced and
reviewable; ``type: extension`` is parsed but skipped with an explicit
``pending_phase4`` status so the skill shows up in the catalogue without
becoming executable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ouroboros.contracts.skill_manifest import (
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MANIFEST_NAMES = ("SKILL.md", "skill.json")
# Files we actually read as part of a skill's review pack (manifest body +
# executable payload + static assets the payload might depend on). The loader
# also consumes the manifest separately; this list controls content-hashing.
# Directories / files that must NOT contribute to the hash even though
# they live inside the skill checkout. We keep the denylist narrow and
# focused on (a) compiler/package-manager scratch (``__pycache__``,
# ``node_modules``, ``.tox``), (b) editor/VCS metadata (``.git``,
# ``.hg``, ``.idea``, ``.vscode``), (c) OS junk (``.DS_Store``).
# Everything else — including non-metadata dotfiles like ``.env-sample``
# or a hand-rolled ``.hidden_helper.py`` — IS hashed and reviewed,
# because the skill subprocess can ``import``/``source``/``read`` such
# files at runtime. A blanket "skip everything starting with '.'" rule
# would let a hidden helper bypass the review gate.
_SKILL_DIR_CACHE_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".tox",
        ".DS_Store",
    }
)

# Sensitive file shapes we refuse to send to external reviewer models.
# Mirrors the repo-review policy in ``ouroboros.tools.review_helpers``
# (reused verbatim via the import in ``_iter_payload_files`` to keep the
# classifier DRY). These files are ALSO excluded from the content hash:
# if someone drops a ``.env`` into their skill checkout we don't want an
# inadvertent edit to stale-invalidate a reviewed skill, and we
# definitely don't want the reviewer prompt to carry credentials.

_REVIEW_STATUS_PASS = "pass"
_REVIEW_STATUS_FAIL = "fail"
_REVIEW_STATUS_ADVISORY = "advisory"
_REVIEW_STATUS_PENDING = "pending"
_REVIEW_STATUS_DEFERRED_PHASE4 = "pending_phase4"

VALID_REVIEW_STATUSES = frozenset(
    {
        _REVIEW_STATUS_PASS,
        _REVIEW_STATUS_FAIL,
        _REVIEW_STATUS_ADVISORY,
        _REVIEW_STATUS_PENDING,
        _REVIEW_STATUS_DEFERRED_PHASE4,
    }
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class SkillReviewState:
    """Persisted review verdict for one skill.

    ``content_hash`` is the sha256 of the manifest + payload files at the
    time the review was produced. ``is_stale_for(current_hash)`` returns
    True when the user has edited the skill since the last review.
    """

    status: str = _REVIEW_STATUS_PENDING
    content_hash: str = ""
    findings: List[Dict[str, Any]] = field(default_factory=list)
    reviewer_models: List[str] = field(default_factory=list)
    timestamp: str = ""
    prompt_chars: int = 0
    cost_usd: float = 0.0
    raw_result: str = ""

    def is_stale_for(self, current_hash: str) -> bool:
        if not current_hash:
            return True
        if not self.content_hash:
            return True
        return self.content_hash != current_hash

    def to_dict(self) -> Dict[str, Any]:
        status = self.status if self.status in VALID_REVIEW_STATUSES else _REVIEW_STATUS_PENDING
        return {
            "status": status,
            "content_hash": self.content_hash,
            "findings": list(self.findings),
            "reviewer_models": list(self.reviewer_models),
            "timestamp": self.timestamp,
            "prompt_chars": int(self.prompt_chars or 0),
            "cost_usd": float(self.cost_usd or 0.0),
            "raw_result": self.raw_result,
        }


@dataclass
class LoadedSkill:
    """A discovered skill package + its durable state.

    ``available_for_execution`` combines three signals:

    - the skill is enabled by the user;
    - the last review landed with status ``pass``;
    - the review is not stale against the current content hash.

    Phase 3 ``type: extension`` skills are always ``False`` because the
    extension loader does not arrive until Phase 4.
    """

    name: str
    skill_dir: pathlib.Path
    manifest: SkillManifest
    content_hash: str
    enabled: bool = False
    review: SkillReviewState = field(default_factory=SkillReviewState)
    load_error: str = ""

    @property
    def available_for_execution(self) -> bool:
        """True when the skill passes every static availability gate.

        Must agree with ``ouroboros.tools.skill_exec._handle_skill_exec``:
        Phase 3 only executes ``type: script`` skills. ``instruction``
        skills are catalogued + reviewable but have no executable
        payload (their manifest declares no scripts); ``extension``
        skills are deferred to Phase 4. Gating on ``manifest.is_script()``
        here ensures ``summarize_skills`` / ``list_available_for_execution``
        cannot report a false-ready signal for skill types that
        ``skill_exec`` will unconditionally reject.

        This does NOT consult the ambient ``OUROBOROS_RUNTIME_MODE`` —
        that's a runtime decision made by the caller (``skill_exec``
        refuses ``light`` mode; the Skills UI reconciles the flag on
        its own via ``summarize_skills`` / ``/api/state``).
        Use :func:`is_runtime_eligible_for_execution` for the full
        "will this actually run right now" answer.
        """
        if self.load_error:
            return False
        if not self.enabled:
            return False
        if not self.manifest.is_script():
            # Only type: script is executable in Phase 3 (instruction =
            # no payload by design; extension = Phase 4).
            return False
        if self.review.status != _REVIEW_STATUS_PASS:
            return False
        if self.review.is_stale_for(self.content_hash):
            return False
        return True


def is_runtime_eligible_for_execution(skill: "LoadedSkill") -> bool:
    """True when the skill is both statically available AND the current
    ``OUROBOROS_RUNTIME_MODE`` allows skill execution (``advanced``/``pro``).

    Used by ``summarize_skills`` / the Skills UI so the "available"
    count stays consistent with what ``skill_exec`` will actually let
    run right now. ``skill_exec`` re-checks the runtime mode itself —
    this helper just prevents UI drift (e.g. light-mode users seeing
    a "ready to run" badge on a skill that will always refuse).
    """
    if not skill.available_for_execution:
        return False
    from ouroboros.config import get_runtime_mode
    return get_runtime_mode() in {"advanced", "pro"}


# ---------------------------------------------------------------------------
# Disk paths
# ---------------------------------------------------------------------------


def _skills_state_root(drive_root: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(drive_root) / "state" / "skills"


def skill_state_dir(drive_root: pathlib.Path, name: str) -> pathlib.Path:
    """Return ``~/Ouroboros/data/state/skills/<name>/`` (created on demand).

    The name is normalized to its alnum-dashes shape before joining so a
    malicious manifest ``name: ../foo`` cannot escape the state root.
    """
    safe = _sanitize_skill_name(name)
    path = _skills_state_root(drive_root) / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _sanitize_skill_name(name: str) -> str:
    """Clamp a skill name to a safe on-disk identifier.

    Keep alphanumerics, dashes, underscores, and dots; replace everything
    else with ``_``. Empty / pathological inputs become ``"_unnamed"``.
    """
    cleaned = "".join(
        ch if ch.isalnum() or ch in "-_." else "_" for ch in str(name or "").strip()
    )
    cleaned = cleaned.strip("._")
    if not cleaned:
        return "_unnamed"
    return cleaned[:64]  # also bound length to keep state paths sane


# ---------------------------------------------------------------------------
# Manifest discovery
# ---------------------------------------------------------------------------


class _ManifestUnreadable(RuntimeError):
    """A manifest file exists but could not be read (permissions,
    truncation, IO error, etc.). Callers translate this into a
    ``LoadedSkill`` with ``load_error`` set so the broken skill is
    still visible in ``list_skills`` instead of silently disappearing
    from discovery."""

    def __init__(self, path: pathlib.Path, err: BaseException) -> None:
        super().__init__(f"manifest {path}: {type(err).__name__}: {err}")
        self.path = path
        self.err = err


def _manifest_text_for_dir(skill_dir: pathlib.Path) -> Optional[tuple[str, pathlib.Path]]:
    """Return (manifest_text, manifest_path) for a skill dir.

    Returns ``None`` ONLY when the directory has no manifest at all
    (i.e. "this is not a skill dir"). A manifest that exists but can't
    be read raises ``_ManifestUnreadable`` so the caller can surface
    the broken skill with a ``load_error`` instead of pretending the
    dir was not a skill dir in the first place.
    """
    for candidate in _MANIFEST_NAMES:
        mf = skill_dir / candidate
        if mf.is_file():
            try:
                return mf.read_text(encoding="utf-8"), mf
            except (OSError, UnicodeDecodeError) as exc:
                # Catch BOTH IO failures and decode failures: a manifest
                # with invalid UTF-8 would otherwise crash discovery for
                # the whole skills checkout instead of degrading to a
                # single broken-skill entry.
                log.warning("Failed to read skill manifest %s", mf, exc_info=True)
                raise _ManifestUnreadable(mf, exc) from exc
    return None


def _iter_payload_files(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
) -> List[pathlib.Path]:
    """Return the sorted list of files that contribute to the content hash.

    The reviewed/hashed surface MUST equal the runtime surface: the
    subprocess runs with ``cwd=skill_dir`` so any non-hidden file in the
    skill directory can be ``import``/``source``/``read`` by the payload.
    If the hash only covered ``scripts/``/``assets/``, a malicious author
    could stash logic in a top-level ``helper.py`` and it would never
    invalidate the PASS verdict when edited.

    Accordingly this walker hashes **every regular file under
    ``skill_dir``** with just three exclusions:

    - dotfiles and dotted directories INSIDE the skill (``.git``,
      ``.DS_Store``, and the like — the dotfile filter is applied to
      *relative* parts so a skills checkout living in a hidden parent
      directory does not have everything silently skipped);
    - well-known cache directory names (``__pycache__``,
      ``node_modules``);
    - files that resolve outside ``skill_dir`` after ``resolve()``
      (symlink escape guard).

    ``manifest_entry`` and ``manifest_scripts`` are still honoured as an
    explicit safety net: if the manifest declares something outside the
    skill directory (e.g. via a malformed ``entry: ../../boot.py``) we
    refuse to include it; if it declares a confined path we include it
    even if the path happens to be on the dotfile exclusion list, so the
    declared executable surface stays consistent with the reviewed one.
    """
    out: List[pathlib.Path] = []
    resolved_root = skill_dir.resolve()

    def _add(path: pathlib.Path) -> None:
        if path not in out:
            out.append(path)

    def _add_if_confined(relpath: str) -> None:
        rel = str(relpath or "").strip()
        if not rel or rel.startswith("/") or rel.startswith("~"):
            return
        if ".." in pathlib.PurePosixPath(rel).parts:
            return
        resolved = (skill_dir / rel).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError:
            return
        if resolved.is_file():
            _add(resolved)

    # Broad walk first — everything inside skill_dir that the runtime
    # subprocess can reach, minus a narrow denylist of metadata/cache
    # names. Two confinement checks run per candidate:
    #
    # 1. Walk with ``follow_symlinks=False`` equivalent: manually reject
    #    any ``.is_symlink()`` entry whose ``resolve()`` target escapes
    #    ``skill_dir``. A symlink that resolves INSIDE the tree is fine
    #    (dedupe is handled by the ``not in out`` guard), but a symlink
    #    to ``/etc/passwd`` would otherwise leak into the review pack
    #    sent to external reviewer models.
    # 2. Re-verify ``relative_to(resolved_root)`` on the resolved path
    #    so symlinked directories pointing outside skill_dir are also
    #    excluded even if their metadata looks in-tree.
    # Reuse the repo-review sensitive-path classifier so skill review
    # inherits the same "never send .env / .pem / credentials.json to
    # reviewer models" policy that protects the main repo (DRY).
    from ouroboros.tools.review_helpers import (
        _SENSITIVE_EXTENSIONS,
        _SENSITIVE_NAMES,
    )

    def _is_sensitive(path: pathlib.Path) -> bool:
        lowered = path.name.lower()
        if lowered in _SENSITIVE_NAMES:
            return True
        for ext in _SENSITIVE_EXTENSIONS:
            if lowered.endswith(ext):
                return True
        return False

    if resolved_root.is_dir():
        for path in sorted(resolved_root.rglob("*")):
            if not path.is_file():
                continue
            try:
                rel_parts = path.relative_to(resolved_root).parts
            except ValueError:
                continue
            if any(part in _SKILL_DIR_CACHE_NAMES for part in rel_parts):
                continue
            if _is_sensitive(path):
                # Presence of a sensitive-shape file inside a skill's
                # runtime-reachable tree is a hard block. If we silently
                # skipped the file, a reviewed skill could still
                # ``open(".env").read()`` at runtime to exfiltrate
                # credentials even though the file was never part of
                # the review pack. Fail closed — operator must rename
                # the file or move it out of the skill tree.
                raise SkillPayloadUnreadable(
                    str(path.relative_to(resolved_root)),
                    RuntimeError(
                        "sensitive-shape filename present in skill tree "
                        "(e.g. .env / credentials.json / .pem). Rename "
                        "or relocate the file outside the skill checkout."
                    ),
                )
            # Symlink escape guard: reject any entry (or parent) whose
            # resolved path leaves ``skill_dir``. We resolve the final
            # path — Path.resolve() collapses symlinks — and re-check
            # confinement.
            try:
                real = path.resolve()
            except (OSError, RuntimeError):
                log.warning("Could not resolve skill file %s", path, exc_info=True)
                continue
            try:
                real.relative_to(resolved_root)
            except ValueError:
                log.warning(
                    "Skill file %s resolves outside skill_dir (%s) — excluded from review pack.",
                    path, resolved_root,
                )
                continue
            _add(path)

    # Manifest-declared entry + scripts explicitly — catches the edge
    # case where an author declared a path that the broad walk would
    # have skipped (e.g. a bare name that needs the ``scripts/`` prefix
    # expansion applied here rather than in two callers).
    _add_if_confined(manifest_entry)
    for script_entry in manifest_scripts or []:
        if not isinstance(script_entry, dict):
            continue
        declared_name = str(script_entry.get("name") or "").strip()
        if not declared_name:
            continue
        _add_if_confined(declared_name)
        if "/" not in declared_name:
            _add_if_confined(f"scripts/{declared_name}")

    out.sort()
    return out


class SkillPayloadUnreadable(RuntimeError):
    """Raised by ``compute_content_hash`` when a payload file cannot be
    read at hash time. The skill surface must FAIL CLOSED: a silent skip
    (as the old implementation did) would let a ``scripts/main.py`` with
    temporarily-unreadable permissions be excluded from both the review
    pack and the hash. Callers surface this as a ``load_error`` on the
    ``LoadedSkill`` and as ``status='pending'`` on ``review_skill``."""

    def __init__(self, relpath: str, err: BaseException) -> None:
        super().__init__(
            f"Skill payload {relpath!r} unreadable: {type(err).__name__}: {err}"
        )
        self.relpath = relpath
        self.err = err


def compute_content_hash(
    skill_dir: pathlib.Path,
    *,
    manifest_entry: str = "",
    manifest_scripts: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Compute a deterministic sha256 of manifest + payload files.

    Used both as a staleness tag on the stored review verdict and as an
    input to the review prompt so the reviewer can log which snapshot it
    looked at. ``manifest_entry`` and ``manifest_scripts`` ensure that
    every file ``skill_exec`` can actually invoke is part of the hash:
    ``type: extension`` skills whose executable surface is a
    ``plugin.py``-style entry module outside the conventional
    ``scripts/`` directory, and ``type: script`` skills whose manifest
    declares ``scripts[].name`` paths like ``bin/run.sh``.

    Fails CLOSED on unreadable files: an ``OSError`` during
    ``read_bytes`` raises :class:`SkillPayloadUnreadable` so callers
    can surface ``load_error``/``status=pending`` rather than emit a
    deceptive PASS over a partial hash.
    """
    digest = hashlib.sha256()
    skill_dir = skill_dir.resolve()
    for file_path in _iter_payload_files(
        skill_dir,
        manifest_entry=manifest_entry,
        manifest_scripts=manifest_scripts,
    ):
        rel = file_path.relative_to(skill_dir).as_posix()
        # Stream per-file hashing in 64 KiB chunks so a pathological
        # skill with a multi-GB asset cannot force ``list_skills`` /
        # ``skill_exec`` preflight to allocate the whole file into
        # memory.
        file_digest = hashlib.sha256()
        try:
            with file_path.open("rb") as fh:
                while True:
                    chunk = fh.read(64 * 1024)
                    if not chunk:
                        break
                    file_digest.update(chunk)
        except OSError as exc:
            log.warning("Failed to read skill payload file %s", file_path, exc_info=True)
            raise SkillPayloadUnreadable(rel, exc) from exc
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.digest())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def _atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    """Atomically write ``payload`` as JSON to ``path``.

    Uses a unique temp filename (pid + thread id + uuid4 fragment) so
    two concurrent writes to the same durable-state file — whether
    from different threads inside one process or from a reviewer tool
    racing with a ``toggle_skill`` — cannot stomp each other's temp
    files or hit ``FileNotFoundError`` in ``os.replace``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = (
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    )
    tmp = path.with_name(tmp_name)
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, path)
    except Exception:
        # Best-effort cleanup of a stale temp; os.replace failure shouldn't
        # leave dot-turds sitting next to the real file.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _read_json(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.warning("Failed to parse skill state file %s", path, exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def load_enabled(drive_root: pathlib.Path, name: str) -> bool:
    state = _read_json(skill_state_dir(drive_root, name) / "enabled.json")
    return bool(state.get("enabled")) if isinstance(state, dict) else False


def save_enabled(drive_root: pathlib.Path, name: str, enabled: bool) -> None:
    _atomic_write_json(
        skill_state_dir(drive_root, name) / "enabled.json",
        {
            "enabled": bool(enabled),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def load_review_state(drive_root: pathlib.Path, name: str) -> SkillReviewState:
    data = _read_json(skill_state_dir(drive_root, name) / "review.json")
    if not isinstance(data, dict):
        return SkillReviewState()
    raw_status = str(data.get("status") or _REVIEW_STATUS_PENDING).lower()
    # Phase 4 retires the ``pending_phase4`` overlay. Any lingering
    # Phase 3 review.json files still carrying that literal status
    # migrate back to plain ``pending`` on load so the summarizer's
    # buckets stay consistent (``pending_phase4`` is no longer a
    # valid persisted status; an extension's real verdict now
    # surfaces verbatim).
    if raw_status == _REVIEW_STATUS_DEFERRED_PHASE4:
        raw_status = _REVIEW_STATUS_PENDING
    status = raw_status if raw_status in VALID_REVIEW_STATUSES else _REVIEW_STATUS_PENDING
    findings = data.get("findings") if isinstance(data.get("findings"), list) else []
    reviewers = (
        data.get("reviewer_models")
        if isinstance(data.get("reviewer_models"), list)
        else []
    )
    return SkillReviewState(
        status=status,
        content_hash=str(data.get("content_hash") or ""),
        findings=[f for f in findings if isinstance(f, dict)],
        reviewer_models=[str(m) for m in reviewers if m],
        timestamp=str(data.get("timestamp") or ""),
        prompt_chars=int(data.get("prompt_chars") or 0),
        cost_usd=float(data.get("cost_usd") or 0.0),
        raw_result=str(data.get("raw_result") or ""),
    )


def save_review_state(
    drive_root: pathlib.Path,
    name: str,
    review: SkillReviewState,
) -> None:
    _atomic_write_json(
        skill_state_dir(drive_root, name) / "review.json",
        review.to_dict(),
    )


# ---------------------------------------------------------------------------
# Discovery / loading
# ---------------------------------------------------------------------------


def _safe_listdir(root: pathlib.Path) -> List[pathlib.Path]:
    try:
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        log.warning("Failed to list skills repo %s", root, exc_info=True)
        return []


def load_skill(
    skill_dir: pathlib.Path,
    drive_root: pathlib.Path,
) -> Optional[LoadedSkill]:
    """Load one skill package into a ``LoadedSkill`` dataclass.

    Returns ``None`` when the directory has no manifest at all (which is
    the signal to callers that this is not a skill folder). A broken
    manifest is returned as a ``LoadedSkill`` with ``load_error`` populated
    so the catalogue UI can display the failure — the alternative of
    raising would hide the broken skill from the operator.
    """
    skill_dir = skill_dir.resolve()
    try:
        manifest_read = _manifest_text_for_dir(skill_dir)
    except _ManifestUnreadable as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
            name=broken_name,
            skill_dir=skill_dir,
            manifest=SkillManifest(
                name=broken_name,
                description="",
                version="",
                type="instruction",
            ),
            content_hash="",
            load_error=f"manifest unreadable: {exc}",
        )
    if manifest_read is None:
        return None
    manifest_text, manifest_path = manifest_read

    try:
        manifest = parse_skill_manifest_text(manifest_text)
    except SkillManifestError as exc:
        broken_name = _sanitize_skill_name(skill_dir.name)
        return LoadedSkill(
            name=broken_name,
            skill_dir=skill_dir,
            manifest=SkillManifest(
                name=broken_name,
                description="",
                version="",
                type="instruction",
            ),
            content_hash="",
            load_error=f"manifest parse error: {exc}",
        )

    # The runtime / state / tool-surface identity is the DIRECTORY
    # BASENAME, not ``manifest.name``. Reasons:
    #
    # - Tool schemas (``skill_exec`` / ``review_skill`` / ``toggle_skill``)
    #   advertise ``skill`` as "the directory name inside
    #   OUROBOROS_SKILLS_REPO_PATH", which is exactly what an operator
    #   sees when they clone / extract / ``ls`` the skills repo.
    # - ``manifest.name`` is free-form display metadata (``Weather Skill``,
    #   ``Агент Погоды``); sanitising it would produce non-stable keys
    #   that change under renames or localisation tweaks.
    # - Directory-basename keys guarantee uniqueness against the
    #   filesystem, which is what the loader iterates anyway.
    #
    # Manifest-level ``name`` is still carried as the display label, and
    # is backfilled from the directory basename when the manifest omits it.
    if not manifest.name:
        manifest.name = skill_dir.name

    name = _sanitize_skill_name(skill_dir.name)
    load_error = ""
    try:
        content_hash = compute_content_hash(
            skill_dir,
            manifest_entry=manifest.entry,
            manifest_scripts=manifest.scripts,
        )
    except SkillPayloadUnreadable as exc:
        content_hash = ""
        load_error = f"payload unreadable: {exc}"
    enabled = load_enabled(drive_root, name)
    review = load_review_state(drive_root, name)

    # Phase 4 ships the extension loader (``ouroboros.extension_loader``),
    # so ``type: extension`` skills now go through the same review +
    # enable + hash-freshness gate as ``type: script`` skills. The
    # ``pending_phase4`` overlay is retired; extensions land in whatever
    # status review actually persisted (``pending`` pre-review, ``pass``
    # after a clean tri-model verdict, etc.). ``skill_exec`` still
    # refuses them (extensions don't execute through the subprocess
    # substrate — they register through ``PluginAPI``), but the catalogue
    # reflects their true state.

    return LoadedSkill(
        name=name,
        skill_dir=skill_dir,
        manifest=manifest,
        content_hash=content_hash,
        enabled=enabled,
        review=review,
        load_error=load_error,
    )


def discover_skills(
    drive_root: pathlib.Path,
    repo_path: str | None = None,
) -> List[LoadedSkill]:
    """Scan the external skills checkout for skill packages.

    ``repo_path`` defaults to ``ouroboros.config.get_skills_repo_path()``.
    An empty/missing path returns an empty list without raising — the
    "no skills repo configured yet" state is normal.
    """
    if repo_path is None:
        from ouroboros.config import get_skills_repo_path
        repo_path = get_skills_repo_path()
    repo_path = str(repo_path or "").strip()
    if not repo_path:
        return []
    root = pathlib.Path(repo_path).expanduser().resolve()
    if not root.is_dir():
        return []

    skills: List[LoadedSkill] = []
    for entry in _safe_listdir(root):
        loaded = load_skill(entry, drive_root)
        if loaded is not None:
            skills.append(loaded)

    # Detect collisions in the sanitised identity. Two distinct
    # directories ("hello world" and "hello_world") must never share
    # ``enabled.json`` / ``review.json`` — ``load_error`` every member of
    # the collision set so the operator can rename before tools can act
    # on the skill.
    by_name: Dict[str, List[LoadedSkill]] = {}
    for skill in skills:
        by_name.setdefault(skill.name, []).append(skill)
    for name, group in by_name.items():
        if len(group) > 1:
            dirs = ", ".join(str(s.skill_dir) for s in group)
            for skill in group:
                if not skill.load_error:
                    skill.load_error = (
                        f"Skill name collision: multiple checkout directories "
                        f"({dirs}) sanitise to {name!r}. Rename the directories "
                        "so their basenames yield distinct identifiers before "
                        "enabling / reviewing / executing."
                    )

    skills.sort(key=lambda s: s.name)
    return skills


def find_skill(
    drive_root: pathlib.Path,
    name: str,
    *,
    repo_path: str | None = None,
) -> Optional[LoadedSkill]:
    """Return one skill by name, or None. Skills with broken manifests
    are returned with ``load_error`` populated — the caller can then
    decide whether to surface them or ignore them."""
    safe = _sanitize_skill_name(name)
    for skill in discover_skills(drive_root, repo_path=repo_path):
        if skill.name == safe:
            return skill
    return None


def list_available_for_execution(
    drive_root: pathlib.Path,
    *,
    repo_path: str | None = None,
) -> List[LoadedSkill]:
    """Return only skills that are enabled + have a fresh PASS review."""
    return [s for s in discover_skills(drive_root, repo_path=repo_path) if s.available_for_execution]


# ---------------------------------------------------------------------------
# Status helpers consumed by /api/state and future Skills UI
# ---------------------------------------------------------------------------


def summarize_skills(drive_root: pathlib.Path) -> Dict[str, Any]:
    """Return a compact catalogue summary for the Skills UI / /api/state.

    ``available_for_execution`` and the top-level ``available`` count
    reflect the CURRENT ``OUROBOROS_RUNTIME_MODE`` — in light mode a
    reviewed+enabled skill is still counted under ``pending_review``'s
    sibling ``runtime_blocked`` instead of ``available``, so the UI and
    ``/api/state`` can never advertise a skill as runnable when
    ``skill_exec`` would refuse it.

    Does not include raw manifest bodies or review findings — callers
    that need the detail should call ``discover_skills`` directly.
    """
    skills = discover_skills(drive_root)
    from ouroboros.config import get_runtime_mode
    runtime_mode = get_runtime_mode()
    runtime_blocks_execution = runtime_mode not in {"advanced", "pro"}
    return {
        "count": len(skills),
        "runtime_mode": runtime_mode,
        "available": sum(
            1 for s in skills if is_runtime_eligible_for_execution(s)
        ),
        "runtime_blocked": sum(
            1
            for s in skills
            if s.available_for_execution and runtime_blocks_execution
        ),
        "pending_review": sum(
            1
            for s in skills
            if s.review.status in (_REVIEW_STATUS_PENDING, "")
            or (
                s.review.status == _REVIEW_STATUS_PASS
                and s.review.is_stale_for(s.content_hash)
            )
        ),
        "failed_review": sum(
            1 for s in skills if s.review.status == _REVIEW_STATUS_FAIL
        ),
        "advisory_review": sum(
            1 for s in skills if s.review.status == _REVIEW_STATUS_ADVISORY
        ),
        "broken": sum(1 for s in skills if s.load_error),
        "skills": [
            {
                "name": s.name,
                "type": s.manifest.type,
                "version": s.manifest.version,
                "enabled": s.enabled,
                "review_status": s.review.status,
                "review_stale": s.review.is_stale_for(s.content_hash),
                "available_for_execution": is_runtime_eligible_for_execution(s),
                "static_ready": s.available_for_execution,
                "runtime_blocked_by_mode": (
                    s.available_for_execution and runtime_blocks_execution
                ),
                "load_error": s.load_error,
            }
            for s in skills
        ],
    }


__all__ = [
    "LoadedSkill",
    "SkillReviewState",
    "VALID_REVIEW_STATUSES",
    "compute_content_hash",
    "discover_skills",
    "find_skill",
    "is_runtime_eligible_for_execution",
    "list_available_for_execution",
    "load_enabled",
    "load_review_state",
    "load_skill",
    "save_enabled",
    "save_review_state",
    "skill_state_dir",
    "summarize_skills",
]
