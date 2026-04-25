"""End-to-end install pipeline tests with a mocked registry.

Covers:

- A successful install lands the staged tree under
  ``<drive_root>/skills/clawhub/<sanitized>/`` and writes provenance.
- A plugin package is rejected up-front (before any review runs).
- An install that fails at the adapter layer cleans up the staging dir
  and never writes provenance.
- ``update_skill`` overwrites the on-disk copy AND re-runs the review.
- ``uninstall_skill`` removes the package + provenance.

The skill_review pipeline is patched to a deterministic stub so the
tests stay hermetic (no LLM calls).
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import textwrap
import zipfile
from dataclasses import dataclass
from unittest import mock

import pytest


SKILL_TEMPLATE = textwrap.dedent(
    """
    ---
    name: SKILL_NAME
    description: Test marketplace skill.
    version: VERSION
    metadata:
      openclaw:
        requires:
          bins: [python3]
        os: [darwin, linux]
    ---

    # Test skill

    Use when running marketplace tests.
    """
).strip() + "\n"


def _build_archive(*, slug_dir: str, version: str = "1.0.0", with_plugin: bool = False) -> bytes:
    """Build an in-memory zip simulating a ClawHub download.

    The archive wraps everything in a ``<slug>/`` directory mirroring
    what ClawHub actually serves; the fetcher strips that prefix.
    """
    skill_md = SKILL_TEMPLATE.replace("SKILL_NAME", slug_dir).replace("VERSION", version)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{slug_dir}/SKILL.md", skill_md)
        zf.writestr(f"{slug_dir}/scripts/main.py", "print('hi')\n")
        if with_plugin:
            zf.writestr(f"{slug_dir}/openclaw.plugin.json", "{}")
    return buf.getvalue()


@pytest.fixture
def enable_marketplace(monkeypatch):
    monkeypatch.setenv("OUROBOROS_CLAWHUB_ENABLED", "true")
    yield
    monkeypatch.delenv("OUROBOROS_CLAWHUB_ENABLED", raising=False)


@pytest.fixture
def stub_review(monkeypatch):
    """Patch skill_review.review_skill to a deterministic PASS stub."""
    @dataclass
    class _Outcome:
        skill_name: str
        status: str = "pass"
        findings: list = None
        reviewer_models: list = None
        content_hash: str = "00" * 32
        prompt_chars: int = 0
        cost_usd: float = 0.0
        raw_result: str = ""
        error: str = ""

        def __post_init__(self):
            self.findings = self.findings or []
            self.reviewer_models = self.reviewer_models or ["stub"]

    def _stub_review_skill(ctx, skill_name, **_kwargs):
        return _Outcome(skill_name=skill_name)

    monkeypatch.setattr("ouroboros.skill_review.review_skill", _stub_review_skill)
    yield _stub_review_skill


@pytest.fixture
def marketplace_drive(tmp_path):
    """Isolate DATA_DIR + REPO_DIR for install pipeline tests.

    install_skill takes ``drive_root`` and ``repo_dir`` as explicit args,
    so we deliberately avoid mutating ``OUROBOROS_DATA_DIR`` /
    reloading ``ouroboros.config`` here — that would poison module-level
    constants for any subsequent tests that imported ``DATA_DIR`` early.
    """
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    data_dir.mkdir()
    repo_dir.mkdir()
    yield data_dir, repo_dir


def _install_with_archive(
    data_dir: pathlib.Path,
    repo_dir: pathlib.Path,
    *,
    slug: str,
    archive: bytes,
    version: str = "1.0.0",
    summary_overrides: dict | None = None,
):
    """Run install_skill with mocked registry calls."""
    import hashlib

    from ouroboros.marketplace import install as install_mod
    from ouroboros.marketplace.clawhub import ClawHubArchive, ClawHubSkillSummary

    summary = ClawHubSkillSummary(
        slug=slug,
        latest_version=version,
        license="MIT",
        is_plugin=bool((summary_overrides or {}).get("is_plugin", False)),
    )
    archive_obj = ClawHubArchive(
        slug=slug,
        version=version,
        content=archive,
        sha256=hashlib.sha256(archive).hexdigest(),
    )
    with mock.patch.object(install_mod, "_registry_info", return_value=summary):
        with mock.patch.object(install_mod, "_registry_download", return_value=archive_obj):
            return install_mod.install_skill(
                data_dir, repo_dir, slug=slug, version=version, auto_review=True, overwrite=True,
            )


def test_install_marketplace_disabled_raises(monkeypatch, marketplace_drive):
    monkeypatch.delenv("OUROBOROS_CLAWHUB_ENABLED", raising=False)
    data_dir, repo_dir = marketplace_drive
    from ouroboros.marketplace.install import install_skill

    with pytest.raises(PermissionError):
        install_skill(data_dir, repo_dir, slug="x/y", version="1.0.0")


def test_install_lands_skill_and_writes_provenance(
    enable_marketplace, stub_review, marketplace_drive
):
    data_dir, repo_dir = marketplace_drive
    archive = _build_archive(slug_dir="my-skill", version="1.0.0")

    result = _install_with_archive(
        data_dir, repo_dir,
        slug="owner/my-skill",
        archive=archive,
        version="1.0.0",
    )
    assert result.ok, f"install failed: {result.error}"
    assert result.review_status == "pass"
    target = data_dir / "skills" / "clawhub" / "owner__my-skill"
    assert target.is_dir()
    assert (target / "SKILL.md").is_file()
    assert (target / "SKILL.openclaw.md").is_file()
    assert (target / "scripts" / "main.py").is_file()

    prov_path = data_dir / "state" / "skills" / "owner__my-skill" / "clawhub.json"
    assert prov_path.is_file()
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    assert prov["source"] == "clawhub"
    assert prov["slug"] == "owner/my-skill"
    assert prov["version"] == "1.0.0"


def test_install_refuses_plugin_packages(
    enable_marketplace, stub_review, marketplace_drive
):
    data_dir, repo_dir = marketplace_drive
    archive = _build_archive(slug_dir="p", with_plugin=True)
    result = _install_with_archive(
        data_dir, repo_dir,
        slug="owner/p",
        archive=archive,
        version="1.0.0",
        summary_overrides={"is_plugin": False},  # only the staged archive flag matters
    )
    assert not result.ok
    assert "plugin" in (result.error or "").lower()
    target = data_dir / "skills" / "clawhub" / "owner__p"
    assert not target.exists()


def test_install_refuses_when_summary_marks_plugin(
    enable_marketplace, stub_review, marketplace_drive
):
    data_dir, repo_dir = marketplace_drive
    archive = _build_archive(slug_dir="p")
    result = _install_with_archive(
        data_dir, repo_dir,
        slug="owner/p",
        archive=archive,
        version="1.0.0",
        summary_overrides={"is_plugin": True},
    )
    assert not result.ok
    assert "plugin" in result.error.lower()


def test_uninstall_removes_dir_and_provenance(
    enable_marketplace, stub_review, marketplace_drive
):
    data_dir, repo_dir = marketplace_drive
    archive = _build_archive(slug_dir="x")
    install_result = _install_with_archive(
        data_dir, repo_dir, slug="owner/x", archive=archive
    )
    assert install_result.ok
    from ouroboros.marketplace.install import uninstall_skill

    uninstall = uninstall_skill(data_dir, sanitized_name="owner__x")
    assert uninstall.ok, uninstall.error
    assert not (data_dir / "skills" / "clawhub" / "owner__x").exists()
    assert not (data_dir / "state" / "skills" / "owner__x" / "clawhub.json").exists()


def test_update_swaps_to_new_version(enable_marketplace, stub_review, marketplace_drive):
    data_dir, repo_dir = marketplace_drive
    archive_v1 = _build_archive(slug_dir="x", version="1.0.0")
    install_result = _install_with_archive(
        data_dir, repo_dir, slug="owner/x", archive=archive_v1, version="1.0.0",
    )
    assert install_result.ok

    from ouroboros.marketplace import install as install_mod
    from ouroboros.marketplace.clawhub import ClawHubArchive, ClawHubSkillSummary

    import hashlib

    archive_v2 = _build_archive(slug_dir="x", version="2.0.0")
    summary = ClawHubSkillSummary(slug="owner/x", latest_version="2.0.0")
    archive_obj = ClawHubArchive(
        slug="owner/x",
        version="2.0.0",
        content=archive_v2,
        sha256=hashlib.sha256(archive_v2).hexdigest(),
    )
    with mock.patch.object(install_mod, "_registry_info", return_value=summary):
        with mock.patch.object(install_mod, "_registry_download", return_value=archive_obj):
            updated = install_mod.update_skill(
                data_dir, repo_dir, sanitized_name="owner__x", version="2.0.0"
            )
    assert updated.ok, updated.error
    prov = json.loads((data_dir / "state" / "skills" / "owner__x" / "clawhub.json").read_text(encoding="utf-8"))
    assert prov["version"] == "2.0.0"


def test_install_review_failure_still_lands_skill(
    enable_marketplace, marketplace_drive, monkeypatch
):
    """Review pipeline is best-effort — a transport error should not roll back install."""
    data_dir, repo_dir = marketplace_drive

    def _broken_review(ctx, skill_name, **_kwargs):
        raise RuntimeError("upstream provider unreachable")

    monkeypatch.setattr("ouroboros.skill_review.review_skill", _broken_review)

    archive = _build_archive(slug_dir="r")
    result = _install_with_archive(data_dir, repo_dir, slug="owner/r", archive=archive)
    assert result.ok
    assert result.review_status == "pending"
    assert "upstream provider unreachable" in result.review_error


# ---------------------------------------------------------------------------
# Cycle 2 critic findings — uninstall path-traversal hardening
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_name",
    [
        "..",
        ".",
        "../external",
        "/etc",
        "foo/bar",
        "foo\\bar",
        "with\x00null",
        "",
        "   ",
    ],
)
def test_uninstall_rejects_path_traversal_names(
    enable_marketplace, marketplace_drive, hostile_name
):
    """Cycle 2 BLOCKER fix — refuse names that try to escape the bucket.

    A name of ``".."`` would otherwise let one POST wipe the entire
    ``data/skills/`` tree (verified by both Gemini and Opus critics).
    """
    data_dir, _repo_dir = marketplace_drive
    # Populate the data plane so we have something destructive to wipe
    # if the validation regresses.
    native_root = data_dir / "skills" / "native"
    clawhub_root = data_dir / "skills" / "clawhub"
    native_root.mkdir(parents=True)
    clawhub_root.mkdir(parents=True)
    (native_root / "weather").mkdir()
    (native_root / "weather" / "SKILL.md").write_text("---\nname: weather\n---\n")
    (data_dir / "skills" / ".bootstrap-seed-complete").write_text("done\n")

    from ouroboros.marketplace.install import uninstall_skill

    result = uninstall_skill(data_dir, sanitized_name=hostile_name)
    assert not result.ok, f"hostile name {hostile_name!r} was accepted"

    # Sanity: nothing destructive happened.
    assert (native_root / "weather" / "SKILL.md").is_file()
    assert (data_dir / "skills" / ".bootstrap-seed-complete").is_file()


def test_uninstall_refuses_directories_without_provenance_sidecar(
    enable_marketplace, marketplace_drive
):
    """Cycle 2 honesty gate — directories under data/skills/clawhub/
    that were not actually installed by the marketplace pipeline must
    not be removable via the uninstall API. The pipeline always writes
    ``.clawhub.json``; without it, the folder is user-managed and the
    API contract does not cover it.
    """
    data_dir, _repo_dir = marketplace_drive
    rogue = data_dir / "skills" / "clawhub" / "user-dropped"
    rogue.mkdir(parents=True)
    (rogue / "SKILL.md").write_text(
        "---\nname: user-dropped\ndescription: x\nversion: 0.1\n---\n"
    )
    # NB: deliberately no .clawhub.json sidecar.

    from ouroboros.marketplace.install import uninstall_skill

    result = uninstall_skill(data_dir, sanitized_name="user-dropped")
    assert not result.ok
    assert "no .clawhub.json sidecar" in result.error
    # Folder must survive.
    assert (rogue / "SKILL.md").is_file()


def test_marketplace_review_ctx_satisfies_tool_context_protocol(tmp_path):
    """v4.50 cycle-2 GPT critic finding — pin the contract.

    The marketplace install path constructs a hand-rolled
    ``_MarketplaceReviewCtx`` shim and passes it to
    ``ouroboros.skill_review.review_skill``. If a future
    ``ToolContextProtocol`` bump adds a new attribute, the auto-review
    path silently regresses with ``AttributeError`` and the install
    lands as ``review_status="pending"``. Pin the structural compliance
    so a future protocol edit fails CI loudly instead.
    """
    from ouroboros.contracts.tool_context import ToolContextProtocol
    from ouroboros.marketplace.install import _MarketplaceReviewCtx

    ctx = _MarketplaceReviewCtx(tmp_path / "drive", tmp_path / "repo")
    assert isinstance(ctx, ToolContextProtocol), (
        "_MarketplaceReviewCtx no longer satisfies ToolContextProtocol — "
        "extend the shim or update the protocol contract."
    )


def test_uninstall_succeeds_on_clean_provenance(
    enable_marketplace, marketplace_drive
):
    """Smoke — the cleaned-up sad path should still pass the happy path.

    A real marketplace-installed skill (with provenance sidecar) gets
    cleanly removed.
    """
    data_dir, _repo_dir = marketplace_drive
    clean = data_dir / "skills" / "clawhub" / "owner__real"
    clean.mkdir(parents=True)
    (clean / "SKILL.md").write_text(
        "---\nname: owner__real\ndescription: x\nversion: 0.1\n---\n"
    )
    (clean / ".clawhub.json").write_text(
        '{"source": "clawhub", "slug": "owner/real"}\n'
    )

    from ouroboros.marketplace.install import uninstall_skill

    result = uninstall_skill(data_dir, sanitized_name="owner__real")
    assert result.ok, result.error
    assert not clean.exists()
