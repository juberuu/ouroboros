"""Install / update / uninstall orchestration for ClawHub-sourced skills.

Hosts the pipeline that ties the lower-level marketplace primitives
together:

1. ``info`` — resolve the registry record + sha (read-only).
2. ``download`` — pull the archive into memory.
3. ``stage`` — extract + validate the archive into a private staging dir.
4. ``adapt`` — translate OpenClaw frontmatter into Ouroboros's shape.
5. ``land`` — atomically swap the staging dir into
   ``data/skills/clawhub/<sanitized>/``.
6. ``review`` — run the existing tri-model ``review_skill`` pipeline.
7. ``provenance`` — persist the audit trail next to the durable skill state.

The pipeline is synchronous and intentionally chunky: each step is
exposed as its own helper so HTTP routes can break the work across
``asyncio.to_thread`` boundaries (and the cycle 1/2 self-review path
can call individual phases independently).
"""

from __future__ import annotations

import logging
import pathlib
import shutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ouroboros.marketplace.adapter import AdapterResult, adapt_openclaw_skill
from ouroboros.marketplace.clawhub import (
    ClawHubArchive,
    ClawHubClientError,
    ClawHubSkillSummary,
    download as _registry_download,
    info as _registry_info,
)
from ouroboros.marketplace.fetcher import FetchError, StagedSkill, stage as _stage_archive
from ouroboros.marketplace.isolated_deps import install_isolated_dependencies
from ouroboros.marketplace.provenance import (
    delete_provenance,
    read_provenance,
    write_provenance,
)

log = logging.getLogger(__name__)


@dataclass
class InstallResult:
    """Outcome of :func:`install_skill`."""

    ok: bool
    sanitized_name: str
    target_dir: Optional[pathlib.Path] = None
    summary: Optional[ClawHubSkillSummary] = None
    archive: Optional[ClawHubArchive] = None
    staged: Optional[StagedSkill] = None
    adapter: Optional[AdapterResult] = None
    review_status: str = ""
    review_findings: List[Dict[str, Any]] = field(default_factory=list)
    review_error: str = ""
    deps_status: str = ""
    deps_error: str = ""
    deps_fingerprint: Dict[str, Any] = field(default_factory=dict)
    error: str = ""
    provenance: Dict[str, Any] = field(default_factory=dict)


@dataclass
class UninstallResult:
    ok: bool
    sanitized_name: str
    error: str = ""


def _ensure_marketplace_enabled() -> None:
    """Compatibility no-op: ClawHub is always available.

    Registry host validation and archive/review gates remain the safety
    boundaries; the old user-facing opt-in switch was removed.
    """
    return None


def _clawhub_skills_root(drive_root: pathlib.Path) -> pathlib.Path:
    """Return ``<drive_root>/skills/clawhub/`` (created on demand).

    Uses the canonical layout helper so the bucket structure stays in
    sync with the launcher bootstrap. The narrow ``ImportError`` catch
    only covers older mocked test contexts that stub out the config
    module; real OS errors (read-only filesystem, permissions) bubble
    up so the operator sees the root cause.
    """
    try:
        from ouroboros.config import ensure_data_skills_dir
        ensure_data_skills_dir(pathlib.Path(drive_root))
    except ImportError:
        pass
    target = pathlib.Path(drive_root) / "skills" / "clawhub"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _land_staged_into_data_plane(
    staged: StagedSkill,
    target_dir: pathlib.Path,
    *,
    overwrite: bool,
) -> None:
    """Atomically swap ``staged.staging_dir`` to ``target_dir``.

    On overwrite we move the existing directory aside to a sibling
    ``<name>.replaced-<sha>`` first, then rename the new tree, then
    delete the sibling. This sequence is rename-only on the same FS
    so the whole swap is durable; on hard crash we land with one of
    {old/new/both} present and the discovery loop tolerates either.
    """
    target_dir = pathlib.Path(target_dir)
    if target_dir.exists():
        if not overwrite:
            raise RuntimeError(
                f"Target {target_dir} already exists — use overwrite=True to replace"
            )
        sibling = target_dir.with_name(f"{target_dir.name}.replaced-{staged.sha256[:8]}")
        try:
            if sibling.exists():
                shutil.rmtree(sibling, ignore_errors=True)
            target_dir.rename(sibling)
        except OSError as exc:
            raise RuntimeError(
                f"Failed to move existing skill out of the way: {exc}"
            ) from exc
        try:
            shutil.move(str(staged.staging_dir), str(target_dir))
        except OSError:
            # Roll back the sibling rename so we don't leave the operator
            # without their previous skill on a partial failure.
            try:
                sibling.rename(target_dir)
            except OSError:
                log.error(
                    "Catastrophic: failed to land new skill AND to restore "
                    "old one. Manual recovery may be needed: %s, %s",
                    target_dir, sibling,
                )
            raise
        # Delete the sibling once the move succeeded.
        shutil.rmtree(sibling, ignore_errors=True)
        return
    target_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staged.staging_dir), str(target_dir))


class _MarketplaceReviewCtx:
    """Headless ``ToolContextProtocol``-compatible ctx for auto-review.

    The marketplace install path runs `review_skill` outside an
    agent task, so there is no live ``ToolContext`` carrier to forward.
    We mint a minimal one that satisfies the frozen
    :class:`ouroboros.contracts.tool_context.ToolContextProtocol` so a
    future review-pipeline edit that adds a new ctx access point
    (e.g. ``ctx.drive_logs()`` for usage-event JSONL) does not silently
    break auto-review with an ``AttributeError``.
    """

    def __init__(self, drive_root: pathlib.Path, repo_dir: pathlib.Path) -> None:
        self.drive_root: pathlib.Path = pathlib.Path(drive_root)
        self.repo_dir: pathlib.Path = pathlib.Path(repo_dir)
        self.task_id: Any = "marketplace_install"
        self.current_chat_id: Any = 0
        self.pending_events: List[Any] = []
        self.emit_progress_fn = lambda _msg: None
        self.event_queue = None  # _emit_usage_event tolerates None
        self.messages: List[Any] = []

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / rel).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / rel).resolve()

    def drive_logs(self) -> pathlib.Path:
        target = self.drive_root / "logs"
        target.mkdir(parents=True, exist_ok=True)
        return target


def _run_skill_review(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    skill_name: str,
) -> tuple[str, List[Dict[str, Any]], str]:
    """Run ``review_skill`` synchronously and return ``(status, findings, error)``.

    Defers the import so a missing review pipeline (e.g. unit-test
    fixtures that stub it out) does not break the install path: in that
    case we return ``("pending", [], <reason>)``.
    """
    try:
        from ouroboros.skill_review import review_skill as _review_skill_impl
    except ImportError as exc:
        return "pending", [], f"review pipeline unavailable: {exc}"

    try:
        outcome = _review_skill_impl(
            _MarketplaceReviewCtx(drive_root, repo_dir), skill_name
        )
    except Exception as exc:
        log.exception("review_skill raised during marketplace install")
        return "pending", [], f"review_skill raised: {type(exc).__name__}: {exc}"
    return (
        str(outcome.status or "pending"),
        list(outcome.findings or []),
        str(outcome.error or ""),
    )


def install_skill(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    slug: str,
    version: Optional[str] = None,
    auto_review: bool = True,
    overwrite: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> InstallResult:
    """End-to-end install of one ClawHub skill into the data plane.

    Returns a populated :class:`InstallResult`. On any failure ``ok``
    is ``False`` and ``error`` carries an operator-facing summary.
    Idempotent for re-installs at the same version: ``overwrite=True``
    is required to replace an existing copy.

    v5.7.0: ``progress_callback`` (optional) receives short human-readable
    stage labels ("Resolving registry…", "Downloading…", "Adapting
    manifest…", "Landing into data plane…", "Running security review…",
    "Installing dependencies…", "Done"). The marketplace HTTP layer
    bridges these to the active :class:`LifecycleJob`'s ``message`` so
    the Skills UI surfaces real per-stage progress instead of a static
    spinner. The callback runs on the worker thread; it must be cheap,
    must not raise, and must not block on the event loop.
    """

    def _progress(stage: str) -> None:
        if progress_callback is None:
            return
        try:
            progress_callback(stage)
        except Exception:
            log.debug("install_skill progress callback raised", exc_info=True)

    _ensure_marketplace_enabled()
    _progress("Resolving registry…")

    cleaned_slug = (slug or "").strip()
    if not cleaned_slug:
        return InstallResult(
            ok=False,
            sanitized_name="",
            error="slug must be non-empty",
        )

    requested_version = (version or "").strip()

    try:
        summary = _registry_info(cleaned_slug)
    except ClawHubClientError as exc:
        return InstallResult(
            ok=False,
            sanitized_name="",
            error=f"Registry lookup failed: {exc}",
        )

    if summary.is_plugin:
        return InstallResult(
            ok=False,
            sanitized_name="",
            summary=summary,
            error=(
                "Package is an OpenClaw Node/TypeScript plugin and cannot be "
                "installed via the Ouroboros marketplace. Skills only."
            ),
        )

    target_version = requested_version or summary.latest_version
    if not target_version:
        return InstallResult(
            ok=False,
            sanitized_name="",
            summary=summary,
            error="Registry returned no version metadata; cannot resolve install target.",
        )

    _progress(f"Downloading v{target_version}…")
    try:
        archive = _registry_download(cleaned_slug, version=target_version)
    except ClawHubClientError as exc:
        return InstallResult(
            ok=False,
            sanitized_name="",
            summary=summary,
            error=f"Download failed: {exc}",
        )

    try:
        # ``archive.sha256`` here is OUR own digest of the bytes we just
        # received (see ``clawhub.download`` docstring). Passing it as
        # ``expected_sha256`` is therefore tautological — it just
        # asserts the fetcher recomputes the hash without bit-flips
        # in-process. The real integrity anchor is TLS to clawhub.ai;
        # we keep the local recomputation as a cheap belt-and-braces
        # check, not as MITM protection.
        #
        # Cycle 2 critic finding (Gemini #3): pin the staging root to
        # the SAME filesystem as the data-plane target so the eventual
        # ``shutil.move`` is a true atomic rename rather than a
        # cross-FS copy+delete. We use a hidden ``.staging`` directory
        # under the clawhub bucket; the fetcher wraps a UUID-suffixed
        # subdir for collision-safety.
        staging_root = _clawhub_skills_root(drive_root) / ".staging"
        staged = _stage_archive(
            archive.content,
            slug=cleaned_slug,
            version=target_version,
            expected_sha256=archive.sha256,
            staging_root=staging_root,
        )
    except FetchError as exc:
        return InstallResult(
            ok=False,
            sanitized_name="",
            summary=summary,
            archive=archive,
            error=f"Archive validation failed: {exc}",
        )

    _progress("Adapting manifest…")
    try:
        adapter_result = adapt_openclaw_skill(
            staged.staging_dir,
            slug=cleaned_slug,
            version=target_version,
            sha256=archive.sha256,
            is_plugin=staged.has_plugin_manifest,
        )
    except Exception as exc:
        staged.cleanup()
        log.exception("adapter raised during install")
        return InstallResult(
            ok=False,
            sanitized_name="",
            summary=summary,
            archive=archive,
            staged=staged,
            error=f"Adapter raised: {type(exc).__name__}: {exc}",
        )

    if not adapter_result.ok:
        staged.cleanup()
        return InstallResult(
            ok=False,
            sanitized_name=adapter_result.sanitized_name,
            summary=summary,
            archive=archive,
            staged=staged,
            adapter=adapter_result,
            error="Adapter rejected the package: " + "; ".join(adapter_result.blockers),
        )

    _progress("Landing into data plane…")
    target_root = _clawhub_skills_root(drive_root)
    target_dir = target_root / adapter_result.target_dirname
    try:
        _land_staged_into_data_plane(staged, target_dir, overwrite=overwrite)
    except Exception as exc:
        staged.cleanup()
        log.exception("Failed to land staged skill into data plane")
        return InstallResult(
            ok=False,
            sanitized_name=adapter_result.sanitized_name,
            summary=summary,
            archive=archive,
            staged=staged,
            adapter=adapter_result,
            error=f"Could not land skill into data plane: {exc}",
        )
    # ``shutil.move`` consumed the staging directory's contents into
    # ``target_dir``. We deliberately do NOT reassign
    # ``staged.staging_dir`` to ``target_dir`` because
    # ``StagedSkill.cleanup()`` calls ``shutil.rmtree`` on that path
    # — a follow-up call to ``cleanup()`` after a successful land
    # would otherwise delete the just-installed skill (cycle 2 critic
    # finding). The fetcher's UUID-suffixed staging dir is safely
    # consumed by the move; ``cleanup()`` becomes a no-op because
    # ``shutil.rmtree`` on a missing path with ``ignore_errors=True``
    # silently returns.

    # Provenance: persist BEFORE running review so the review can
    # cross-reference the source-of-truth record. Using
    # adapter_result.provenance verbatim keeps the OpenClaw <-> Ouroboros
    # mapping reproducible.
    from ouroboros.config import get_clawhub_registry_url
    provenance = dict(adapter_result.provenance)
    provenance.update({
        "registry_url": get_clawhub_registry_url(),
        "version": target_version,
        "homepage": summary.homepage,
        "license": summary.license,
        "primary_env": summary.primary_env,
    })
    try:
        write_provenance(drive_root, adapter_result.sanitized_name, provenance)
    except Exception:
        log.warning("Failed to persist provenance for %s", adapter_result.sanitized_name, exc_info=True)

    # v5.7.0: write an explicit ``grants.json`` with ``requested_keys`` and
    # empty ``granted_keys`` when the freshly-installed skill requests core
    # keys via ``env_from_settings`` (e.g. ``OPENROUTER_API_KEY``). The
    # Skills UI already computes the requested set from the manifest
    # on-the-fly via ``grant_status_for_skill``; persisting this file
    # at install time means the desktop launcher's owner-grant bridge
    # has a single canonical state file to update once the operator
    # approves keys, regardless of whether the skill carries
    # ``provenance.requires.config``, ``provenance.requested_key_grants``,
    # or only the manifest's ``env_from_settings`` allowlist.
    try:
        from ouroboros.skill_loader import (
            find_skill,
            requested_core_setting_keys,
            save_skill_grants,
        )
        installed_skill = find_skill(drive_root, adapter_result.sanitized_name)
        if installed_skill is not None:
            requested = requested_core_setting_keys(
                list(installed_skill.manifest.env_from_settings or [])
            )
            if requested:
                save_skill_grants(
                    drive_root,
                    installed_skill.name,
                    granted_keys=[],
                    content_hash=installed_skill.content_hash,
                    requested_keys=requested,
                )
    except Exception:
        log.debug("requires.config -> grants.json bootstrap failed", exc_info=True)

    review_status = "pending"
    review_findings: List[Dict[str, Any]] = []
    review_error = ""
    deps_status = "not_required"
    deps_error = ""
    deps_fingerprint: Dict[str, Any] = {}
    if auto_review:
        _progress("Running security review…")
        review_status, review_findings, review_error = _run_skill_review(
            drive_root, repo_dir, adapter_result.sanitized_name
        )
    auto_specs = list((provenance.get("install_specs") or {}).get("auto") or [])
    if auto_specs:
        deps_status = "pending_review"
        if review_status == "pass" and not review_error:
            _progress("Installing dependencies…")
            try:
                deps_fingerprint = install_isolated_dependencies(
                    drive_root,
                    adapter_result.sanitized_name,
                    target_dir,
                    auto_specs,
                )
                deps_status = "installed"
                provenance["dependency_fingerprint"] = deps_fingerprint
                write_provenance(drive_root, adapter_result.sanitized_name, provenance)
            except Exception as exc:
                log.exception("isolated dependency install failed for %s", adapter_result.sanitized_name)
                deps_status = "failed"
                deps_error = f"{type(exc).__name__}: {exc}"
    _progress("Done")

    return InstallResult(
        ok=deps_status != "failed",
        sanitized_name=adapter_result.sanitized_name,
        target_dir=target_dir,
        summary=summary,
        archive=archive,
        staged=staged,
        adapter=adapter_result,
        review_status=review_status,
        review_findings=review_findings,
        review_error=review_error,
        deps_status=deps_status,
        deps_error=deps_error,
        deps_fingerprint=deps_fingerprint,
        error=deps_error if deps_status == "failed" else "",
        provenance=provenance,
    )


def uninstall_skill(
    drive_root: pathlib.Path,
    *,
    sanitized_name: str,
) -> UninstallResult:
    """Remove a ClawHub-installed skill + its provenance.

    Leaves the durable state plane (``data/state/skills/<name>/``) for
    the user to inspect EXCEPT for ``clawhub.json`` which we drop
    explicitly. Other files (``enabled.json``, ``review.json``) get
    invalidated naturally because ``find_skill`` will no longer return
    a match — but we deliberately don't delete them in case the
    operator reinstalls the same slug later.

    Hardened (cycle 2 critic finding) against path traversal: a
    ``name`` of ``".."`` / ``"."`` / ``"foo/bar"`` would otherwise let
    a single POST wipe the entire ``data/skills/`` tree. We:

    1. reject any name that does not survive
       ``_sanitize_skill_name`` round-trip;
    2. resolve the candidate target and confirm it is contained
       inside ``<drive_root>/skills/clawhub/``;
    3. require a ``.clawhub.json`` provenance sidecar inside the
       target so we cannot delete a folder that was not actually
       installed by the marketplace pipeline (P6 honesty — the API
       contract says "uninstall a marketplace skill", not "rm -rf
       arbitrary directory under data/skills/clawhub/").
    """
    _ensure_marketplace_enabled()

    from ouroboros.skill_loader import _sanitize_skill_name

    cleaned = (sanitized_name or "").strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or "\x00" in cleaned
        or _sanitize_skill_name(cleaned) != cleaned
    ):
        return UninstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error=(
                "invalid sanitized_name — must round-trip through "
                "_sanitize_skill_name and contain no path separators"
            ),
        )

    root = _clawhub_skills_root(drive_root).resolve()
    target = (root / cleaned).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return UninstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error=f"target escapes clawhub root: {target}",
        )
    if target == root:
        return UninstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error="refusing to delete the clawhub bucket root",
        )

    if not target.is_dir():
        return UninstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error=f"Not found: {target}",
        )

    # Final honesty gate: refuse to remove a directory that the
    # marketplace pipeline did not install. The adapter writes
    # ``.clawhub.json`` into every staged tree before land; its
    # absence means this folder came from somewhere else and the
    # ``uninstall`` API's contract does not cover it.
    if not (target / ".clawhub.json").is_file():
        return UninstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error=(
                f"refusing to remove {cleaned!r}: no .clawhub.json "
                "sidecar (not a marketplace-installed skill)"
            ),
        )

    # v5.7.0: unload any in-process extension instance BEFORE removing the
    # payload directory. Otherwise the loader's tools/routes/ws_handlers/
    # ui_tabs registries keep pointing at modules whose source has just
    # been deleted, and any background threads/timers/EventSource clients
    # the extension started keep running until the next dispatch tries to
    # use them and fails. Calling ``unload_extension`` runs registered
    # ``on_unload`` callbacks first, then drops the registrations.
    try:
        from ouroboros.extension_loader import unload_extension
        unload_extension(cleaned)
    except Exception:  # pragma: no cover — defensive
        log.debug("extension unload pre-uninstall failed for %s", cleaned, exc_info=True)
    try:
        shutil.rmtree(target)
    except OSError as exc:
        return UninstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error=f"Failed to remove {target}: {exc}",
        )
    delete_provenance(drive_root, cleaned)
    return UninstallResult(ok=True, sanitized_name=cleaned)


def update_skill(
    drive_root: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    sanitized_name: str,
    version: Optional[str] = None,
) -> InstallResult:
    """Reinstall a ClawHub skill at a newer version.

    ``sanitized_name`` is the on-disk identity (``owner__slug``); we
    consult the persisted provenance to recover the original slug for
    the registry lookup.
    """
    record = read_provenance(drive_root, sanitized_name)
    if not record:
        return InstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error=(
                f"No clawhub.json provenance for {sanitized_name!r} — "
                "this skill was not installed via the marketplace."
            ),
        )
    slug = str(record.get("slug") or "").strip()
    if not slug:
        return InstallResult(
            ok=False,
            sanitized_name=sanitized_name,
            error="provenance is missing slug",
        )
    # v5.7.0: capture the current live state BEFORE the unload+swap.
    # If the extension was live and enabled, we will reconcile it after
    # install_skill lands the new payload so the user does not have to
    # toggle the skill off/on by hand.
    was_live = False
    try:
        from ouroboros.extension_loader import is_extension_live, unload_extension
        was_live = bool(is_extension_live(sanitized_name, drive_root))
        unload_extension(sanitized_name)
    except Exception:  # pragma: no cover — defensive
        log.debug("pre-update unload failed for %s", sanitized_name, exc_info=True)
    result = install_skill(
        drive_root,
        repo_dir,
        slug=slug,
        version=version,
        auto_review=True,
        overwrite=True,
    )
    if was_live and (not getattr(result, "ok", False) or getattr(result, "review_status", "") == "pass"):
        try:
            from ouroboros.extension_loader import reconcile_extension
            from ouroboros.config import load_settings
            reconcile_extension(sanitized_name, drive_root, load_settings)
        except Exception:  # pragma: no cover — defensive
            log.debug("post-update reconcile failed for %s", sanitized_name, exc_info=True)
    return result


__all__ = [
    "InstallResult",
    "UninstallResult",
    "install_skill",
    "uninstall_skill",
    "update_skill",
]
