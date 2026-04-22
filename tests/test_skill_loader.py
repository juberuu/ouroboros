"""Phase 3 regression tests for ``ouroboros.skill_loader``.

Covers discovery, content-hashing, enabled-state persistence, and review
state round-trip. No network, no real review calls — these tests stay
hermetic against ``tmp_path``.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from ouroboros.skill_loader import (
    LoadedSkill,
    SkillReviewState,
    VALID_REVIEW_STATUSES,
    compute_content_hash,
    discover_skills,
    find_skill,
    list_available_for_execution,
    load_enabled,
    load_review_state,
    load_skill,
    save_enabled,
    save_review_state,
    skill_state_dir,
    summarize_skills,
)


def _write_skill(
    repo_root: pathlib.Path,
    name: str,
    *,
    manifest: str,
    scripts: dict[str, str] | None = None,
    manifest_name: str = "SKILL.md",
) -> pathlib.Path:
    skill_dir = repo_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / manifest_name).write_text(manifest, encoding="utf-8")
    if scripts:
        (skill_dir / "scripts").mkdir(exist_ok=True)
        for filename, body in scripts.items():
            (skill_dir / "scripts" / filename).write_text(body, encoding="utf-8")
    return skill_dir


def _valid_script_manifest(name: str = "weather") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        "description: Check the weather.\n"
        "version: 0.1.0\n"
        "type: script\n"
        "runtime: python3\n"
        "timeout_sec: 30\n"
        "permissions: [net]\n"
        "scripts:\n"
        "  - name: fetch.py\n"
        "    description: Fetch current weather.\n"
        "---\n"
        "# Weather skill\n\nCall fetch.py with a city.\n"
    )


# ---------------------------------------------------------------------------
# Discovery + loading
# ---------------------------------------------------------------------------


def test_discover_skills_returns_empty_when_unconfigured(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    assert discover_skills(drive_root, repo_path="") == []
    # A missing path is also silently tolerated — same "no skills" signal.
    assert discover_skills(drive_root, repo_path=str(tmp_path / "does-not-exist")) == []


def test_load_skill_parses_manifest_and_computes_hash(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(
        repo_root,
        "weather",
        manifest=_valid_script_manifest(),
        scripts={"fetch.py": "print('hi')\n"},
    )
    loaded = load_skill(repo_root / "weather", drive_root)
    assert isinstance(loaded, LoadedSkill)
    assert loaded.name == "weather"
    assert loaded.manifest.type == "script"
    assert loaded.manifest.runtime == "python3"
    assert loaded.content_hash  # non-empty
    assert loaded.enabled is False  # default
    assert loaded.review.status == "pending"
    assert loaded.available_for_execution is False


def test_load_skill_returns_none_for_non_skill_dir(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    other = tmp_path / "random"
    other.mkdir()
    (other / "README.txt").write_text("hi", encoding="utf-8")
    assert load_skill(other, drive_root) is None


def test_load_skill_surfaces_broken_manifest(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(
        repo_root,
        "broken",
        manifest='{"name": ',  # truncated JSON
        manifest_name="skill.json",
    )
    loaded = load_skill(repo_root / "broken", drive_root)
    assert loaded is not None
    assert loaded.load_error
    assert loaded.available_for_execution is False


def test_load_skill_surfaces_unreadable_manifest(tmp_path):
    """Phase 3 round 16 regression: an existing-but-unreadable manifest
    must surface as ``load_error`` instead of silently looking like
    "not a skill dir at all"."""
    import os, platform, stat
    if platform.system() == "Windows":
        pytest.skip("chmod-based permission test not portable to Windows")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "unread",
        manifest=_valid_script_manifest("unread"),
    )
    manifest_path = skill_dir / "SKILL.md"
    original_mode = manifest_path.stat().st_mode
    os.chmod(manifest_path, 0o000)
    try:
        loaded = load_skill(skill_dir, drive_root)
    finally:
        os.chmod(manifest_path, original_mode)
    # Root users can read anything regardless of perms — skip the
    # assertion in that case (rare, but CI runners vary).
    if os.geteuid() == 0:  # pragma: no cover — only hit in root CI
        pytest.skip("root user bypasses 0o000 chmod, cannot trigger OSError")
    assert loaded is not None, "Unreadable manifest must still appear in discovery."
    assert loaded.load_error, "load_error should be populated for unreadable manifests."
    assert "unreadable" in loaded.load_error.lower()


def test_discover_skills_picks_up_multiple(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(repo_root, "alpha", manifest=_valid_script_manifest("alpha"))
    _write_skill(repo_root, "beta", manifest=_valid_script_manifest("beta"))
    skills = discover_skills(drive_root, repo_path=str(repo_root))
    names = {s.name for s in skills}
    assert names == {"alpha", "beta"}


def test_find_skill_returns_match_and_missing(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(repo_root, "alpha", manifest=_valid_script_manifest("alpha"))
    os.environ["OUROBOROS_SKILLS_REPO_PATH"] = str(repo_root)
    try:
        assert find_skill(drive_root, "alpha") is not None
        assert find_skill(drive_root, "does-not-exist") is None
    finally:
        os.environ.pop("OUROBOROS_SKILLS_REPO_PATH", None)


# ---------------------------------------------------------------------------
# Content hashing
# ---------------------------------------------------------------------------


def test_content_hash_changes_when_script_edited(tmp_path):
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "alpha",
        manifest=_valid_script_manifest("alpha"),
        scripts={"fetch.py": "print('one')\n"},
    )
    before = compute_content_hash(skill_dir)
    (skill_dir / "scripts" / "fetch.py").write_text("print('two')\n", encoding="utf-8")
    after = compute_content_hash(skill_dir)
    assert before != after


def test_content_hash_stable_against_state_dir_noise(tmp_path):
    """State-dir writes must not invalidate the skill content hash."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "alpha",
        manifest=_valid_script_manifest("alpha"),
        scripts={"fetch.py": "print('x')\n"},
    )
    before = compute_content_hash(skill_dir)
    # State-dir writes happen in ``data/state/skills/<name>/``, which is
    # outside the skill directory entirely — hash should be unaffected.
    save_enabled(drive_root, "alpha", True)
    save_review_state(
        drive_root,
        "alpha",
        SkillReviewState(status="pass", content_hash=before),
    )
    after = compute_content_hash(skill_dir)
    assert before == after


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------


def test_enabled_round_trip(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    assert load_enabled(drive_root, "x") is False
    save_enabled(drive_root, "x", True)
    assert load_enabled(drive_root, "x") is True
    save_enabled(drive_root, "x", False)
    assert load_enabled(drive_root, "x") is False


def test_review_state_round_trip(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    # Default when no file on disk.
    assert load_review_state(drive_root, "x").status == "pending"
    state = SkillReviewState(
        status="pass",
        content_hash="abcd",
        findings=[{"item": "manifest_schema", "verdict": "PASS", "severity": "critical", "reason": "ok"}],
        reviewer_models=["openai/gpt-5.4"],
        timestamp="2026-04-21T00:00:00+00:00",
        prompt_chars=1234,
        cost_usd=0.5,
    )
    save_review_state(drive_root, "x", state)
    reloaded = load_review_state(drive_root, "x")
    assert reloaded.status == "pass"
    assert reloaded.content_hash == "abcd"
    assert reloaded.reviewer_models == ["openai/gpt-5.4"]
    assert reloaded.prompt_chars == 1234


def test_review_state_unknown_status_clamped_to_pending(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    raw_path = skill_state_dir(drive_root, "x") / "review.json"
    raw_path.write_text(
        json.dumps({"status": "TURBO", "content_hash": "abcd"}),
        encoding="utf-8",
    )
    reloaded = load_review_state(drive_root, "x")
    assert reloaded.status == "pending"
    assert reloaded.content_hash == "abcd"


# ---------------------------------------------------------------------------
# available_for_execution gating
# ---------------------------------------------------------------------------


def test_available_for_execution_requires_pass_review_and_enabled(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(
        repo_root,
        "alpha",
        manifest=_valid_script_manifest("alpha"),
        scripts={"fetch.py": "print('x')\n"},
    )
    # Step 1: pending + disabled → not available.
    assert list_available_for_execution(drive_root, repo_path=str(repo_root)) == []

    # Step 2: enabled but still pending → not available.
    save_enabled(drive_root, "alpha", True)
    assert list_available_for_execution(drive_root, repo_path=str(repo_root)) == []

    # Step 3: pass review with the current hash → available.
    loaded = find_skill(drive_root, "alpha", repo_path=str(repo_root))
    assert loaded is not None
    save_review_state(
        drive_root,
        "alpha",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    available = list_available_for_execution(drive_root, repo_path=str(repo_root))
    assert [s.name for s in available] == ["alpha"]

    # Step 4: edit the script → review goes stale → not available again.
    (loaded.skill_dir / "scripts" / "fetch.py").write_text("print('edited')\n", encoding="utf-8")
    available = list_available_for_execution(drive_root, repo_path=str(repo_root))
    assert available == []


def test_extension_skill_never_executable_in_phase3(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    manifest = (
        "---\n"
        "name: ext1\n"
        "type: extension\n"
        "version: 0.1.0\n"
        "entry: plugin.py\n"
        "permissions: [widget]\n"
        "---\n"
        "body\n"
    )
    skill_dir = _write_skill(repo_root, "ext1", manifest=manifest)
    (skill_dir / "plugin.py").write_text("def register(api): pass\n", encoding="utf-8")
    save_enabled(drive_root, "ext1", True)
    loaded = find_skill(drive_root, "ext1", repo_path=str(repo_root))
    assert loaded is not None
    save_review_state(
        drive_root,
        "ext1",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )
    loaded = find_skill(drive_root, "ext1", repo_path=str(repo_root))
    assert loaded.manifest.is_extension()
    assert loaded.available_for_execution is False, (
        "Phase 3 must defer type=extension execution until Phase 4."
    )


def test_loaded_skill_identity_is_directory_basename_not_manifest_name(tmp_path):
    """Phase 3 round 9 regression: tool schemas advertise ``skill`` as
    the directory name in ``OUROBOROS_SKILLS_REPO_PATH``. ``LoadedSkill.name``
    + the durable state dir key MUST match that so ``skill_exec("weather")``
    resolves ``skills/weather/`` regardless of ``manifest.name`` free-form
    content (``Weather Skill``, localised label, etc.).
    """
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    weird_manifest = (
        "---\n"
        "name: Weather Skill Display\n"
        "description: Check the weather.\n"
        "version: 0.1.0\n"
        "type: script\n"
        "runtime: python3\n"
        "timeout_sec: 30\n"
        "scripts:\n"
        "  - name: fetch.py\n"
        "---\n"
        "body\n"
    )
    _write_skill(
        repo_root,
        "weather",
        manifest=weird_manifest,
        scripts={"fetch.py": "print('ok')\n"},
    )
    loaded = find_skill(drive_root, "weather", repo_path=str(repo_root))
    assert loaded is not None
    assert loaded.name == "weather"
    # Manifest display name preserved as metadata.
    assert loaded.manifest.name == "Weather Skill Display"
    # Addressable by directory name, NOT by the sanitised manifest name.
    from ouroboros.skill_loader import _sanitize_skill_name as _sn
    assert _sn("Weather Skill Display") != loaded.name


def test_hidden_helper_files_are_hashed_and_reviewed(tmp_path):
    """Phase 3 round 10 regression: a blanket "skip all dotfiles" rule
    would let a hand-rolled ``.hidden_helper.py`` be imported by a
    reviewed script without contributing to the content hash. Hidden
    files OTHER than VCS/cache metadata must be hashed + reviewed."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "sneak",
        manifest=_valid_script_manifest("sneak"),
        scripts={"main.py": "import importlib\nimportlib.import_module('.hidden_helper')\n"},
    )
    (skill_dir / ".hidden_helper.py").write_text("X = 1\n", encoding="utf-8")
    before = compute_content_hash(skill_dir, manifest_scripts=[{"name": "main.py"}])
    (skill_dir / ".hidden_helper.py").write_text("X = 'poisoned'\n", encoding="utf-8")
    after = compute_content_hash(skill_dir, manifest_scripts=[{"name": "main.py"}])
    assert before != after, (
        "Hidden helper file must be hashed — the subprocess can still "
        "import it, so a review PASS must stale when it changes."
    )


def test_vcs_cache_dirs_are_not_hashed(tmp_path):
    """Conversely, ``.git``/``__pycache__``/editor scratch directories
    MUST be excluded from the hash so a byte-flip in a cache file does
    not invalidate the review."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "cacheskill",
        manifest=_valid_script_manifest("cacheskill"),
        scripts={"main.py": "print('ok')\n"},
    )
    (skill_dir / ".git").mkdir()
    (skill_dir / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (skill_dir / "__pycache__").mkdir()
    (skill_dir / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x00\x01")
    before = compute_content_hash(skill_dir, manifest_scripts=[{"name": "main.py"}])
    (skill_dir / ".git" / "HEAD").write_text("ref: refs/heads/other\n", encoding="utf-8")
    (skill_dir / "__pycache__" / "main.cpython-311.pyc").write_bytes(b"\x02\x03")
    after = compute_content_hash(skill_dir, manifest_scripts=[{"name": "main.py"}])
    assert before == after, "VCS/cache scratch must be excluded from the hash."


def test_symlink_escape_excluded_from_pack(tmp_path):
    """Phase 3 round 10 regression: a symlink inside ``skill_dir`` whose
    target resolves outside the tree must NOT be hashed — otherwise
    ``compute_content_hash`` + ``_build_skill_file_pack`` would exfiltrate
    arbitrary local file contents to external reviewer models."""
    import os, platform
    if platform.system() == "Windows":
        pytest.skip("symlink creation requires admin on Windows")
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "lnk",
        manifest=_valid_script_manifest("lnk"),
        scripts={"main.py": "print('ok')\n"},
    )
    outside = tmp_path / "outside_secret.txt"
    outside.write_text("SECRET_PAYLOAD\n", encoding="utf-8")
    escape_link = skill_dir / "escape.txt"
    os.symlink(outside, escape_link)
    files = _iter_payload_files_list = None
    # Use the private walker directly — this is the "would the hash /
    # review pack see this file" question.
    from ouroboros.skill_loader import _iter_payload_files
    reviewed = _iter_payload_files(skill_dir, manifest_scripts=[{"name": "main.py"}])
    assert escape_link.resolve() not in {p.resolve() for p in reviewed}
    # Hash is still deterministic (covers in-tree files only).
    assert compute_content_hash(skill_dir, manifest_scripts=[{"name": "main.py"}])


def test_sensitive_files_fail_closed_on_load(tmp_path):
    """Phase 3 round 20: a skill that ships a sensitive-shape file
    (`.env`, `credentials.json`, `.pem`, ...) fails to load. Rationale:
    silently excluding the file from hash/review would let a reviewed
    skill ``open('.env').read()`` at runtime to exfiltrate credentials
    that the reviewer never saw. The loader fails closed via
    ``SkillPayloadUnreadable``; the user must rename / relocate the
    file out of the skill directory."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "secrety",
        manifest=_valid_script_manifest("secrety"),
        scripts={"main.py": "print('ok')\n"},
    )
    (skill_dir / ".env").write_text("SECRET_KEY=leak\n", encoding="utf-8")
    from ouroboros.skill_loader import SkillPayloadUnreadable
    with pytest.raises(SkillPayloadUnreadable):
        compute_content_hash(skill_dir, manifest_scripts=[{"name": "main.py"}])
    # The LoadedSkill reflects the load_error rather than crashing.
    loaded = load_skill(skill_dir, drive_root)
    assert loaded is not None
    assert loaded.load_error
    assert "sensitive" in loaded.load_error.lower()
    assert loaded.available_for_execution is False


def test_sanitized_name_collision_surfaces_as_load_error(tmp_path):
    """Phase 3 round 12 regression: ``skills/hello world/`` and
    ``skills/hello_world/`` both sanitise to the same identity. The
    loader must refuse to merge their state and surface a load_error
    on each collision member."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(
        repo_root,
        "hello world",
        manifest=_valid_script_manifest("hello world"),
    )
    _write_skill(
        repo_root,
        "hello_world",
        manifest=_valid_script_manifest("hello_world"),
    )
    skills = discover_skills(drive_root, repo_path=str(repo_root))
    assert len(skills) == 2
    for s in skills:
        assert s.load_error
        assert "name collision" in s.load_error.lower()
        assert s.available_for_execution is False


def test_toplevel_skill_files_are_hashed_and_reviewed(tmp_path):
    """Phase 3 round 8 regression: runtime surface == reviewed surface.

    A subprocess started with ``cwd=skill_dir`` can ``import`` any
    non-hidden file at the top level. If those files were not part of
    ``_iter_payload_files`` the PASS verdict would not stale when
    they change. This test drops a top-level ``helper.py`` and checks
    that it IS included in the content hash."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = _write_skill(
        repo_root,
        "mixed",
        manifest=_valid_script_manifest("mixed"),
        scripts={"fetch.py": "from helper import X\nprint(X)\n"},
    )
    (skill_dir / "helper.py").write_text("X = 'v1'\n", encoding="utf-8")
    before = compute_content_hash(
        skill_dir,
        manifest_entry="",
        manifest_scripts=[{"name": "fetch.py"}],
    )
    (skill_dir / "helper.py").write_text("X = 'v2-poisoned'\n", encoding="utf-8")
    after = compute_content_hash(
        skill_dir,
        manifest_entry="",
        manifest_scripts=[{"name": "fetch.py"}],
    )
    assert before != after, (
        "Editing a top-level helper.py must invalidate the content hash — "
        "skill_exec runs with cwd=skill_dir so that file is reachable."
    )


def test_extension_status_reflects_persisted_verdict_in_phase4(tmp_path):
    """Phase 4 lifted the old Phase 3 ``pending_phase4`` overlay — now
    that the extension loader exists, a persisted review verdict for a
    ``type: extension`` skill must surface verbatim so operators and
    the Skills UI see the real state."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    manifest = (
        "---\n"
        "name: ext2\n"
        "type: extension\n"
        "version: 0.1.0\n"
        "entry: plugin.py\n"
        "permissions: [widget]\n"
        "---\n"
        "body\n"
    )
    skill_dir = _write_skill(repo_root, "ext2", manifest=manifest)
    (skill_dir / "plugin.py").write_text("def register(api): pass\n", encoding="utf-8")

    loaded_initial = find_skill(drive_root, "ext2", repo_path=str(repo_root))
    assert loaded_initial is not None
    save_review_state(
        drive_root,
        "ext2",
        SkillReviewState(status="pass", content_hash=loaded_initial.content_hash),
    )

    reloaded = find_skill(drive_root, "ext2", repo_path=str(repo_root))
    assert reloaded is not None
    # Real verdict surfaces — Phase 4 retired the ``pending_phase4`` overlay.
    assert reloaded.review.status == "pass"

    os.environ["OUROBOROS_SKILLS_REPO_PATH"] = str(repo_root)
    try:
        summary = summarize_skills(drive_root)
    finally:
        os.environ.pop("OUROBOROS_SKILLS_REPO_PATH", None)
    statuses = {s["name"]: s["review_status"] for s in summary["skills"]}
    assert statuses["ext2"] == "pass"


# ---------------------------------------------------------------------------
# summarize_skills shape
# ---------------------------------------------------------------------------


def test_summarize_skills_shape_contains_counts_and_flat_list(tmp_path):
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(repo_root, "alpha", manifest=_valid_script_manifest("alpha"))
    os.environ["OUROBOROS_SKILLS_REPO_PATH"] = str(repo_root)
    try:
        summary = summarize_skills(drive_root)
    finally:
        os.environ.pop("OUROBOROS_SKILLS_REPO_PATH", None)
    assert summary["count"] == 1
    assert summary["available"] == 0
    assert summary["pending_review"] == 1
    assert summary["failed_review"] == 0
    assert summary["broken"] == 0
    assert [s["name"] for s in summary["skills"]] == ["alpha"]


def test_summarize_skills_reflects_runtime_mode_light(tmp_path, monkeypatch):
    """Phase 3 round 11 regression: a reviewed+enabled skill must NOT
    be reported as ``available`` when ``OUROBOROS_RUNTIME_MODE=light``,
    because ``skill_exec`` refuses to run anything in light mode."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    _write_skill(
        repo_root,
        "alpha",
        manifest=_valid_script_manifest("alpha"),
        scripts={"fetch.py": "print('ok')\n"},
    )
    # Mark reviewed + enabled so the skill would be statically available.
    loaded = find_skill(drive_root, "alpha", repo_path=str(repo_root))
    assert loaded is not None
    save_enabled(drive_root, "alpha", True)
    save_review_state(
        drive_root,
        "alpha",
        SkillReviewState(status="pass", content_hash=loaded.content_hash),
    )

    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(repo_root))

    # advanced → available
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "advanced")
    adv = summarize_skills(drive_root)
    assert adv["available"] == 1
    assert adv["runtime_blocked"] == 0
    assert adv["skills"][0]["available_for_execution"] is True

    # light → NOT available, but runtime_blocked increments
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    light = summarize_skills(drive_root)
    assert light["available"] == 0
    assert light["runtime_blocked"] == 1
    assert light["skills"][0]["available_for_execution"] is False
    assert light["skills"][0]["runtime_blocked_by_mode"] is True
    # The static readiness signal is preserved so the UI can still
    # explain "this would be ready if you were in advanced mode".
    assert light["skills"][0]["static_ready"] is True


def test_valid_review_statuses_exported():
    assert "pass" in VALID_REVIEW_STATUSES
    assert "fail" in VALID_REVIEW_STATUSES
    assert "pending" in VALID_REVIEW_STATUSES
    assert "pending_phase4" in VALID_REVIEW_STATUSES


# ---------------------------------------------------------------------------
# Safety: skill name sanitization
# ---------------------------------------------------------------------------


def test_skill_state_dir_resists_path_escape(tmp_path):
    """A malicious manifest ``name: ../../etc`` cannot escape the state root."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    malicious = "../../etc/passwd"
    state_path = skill_state_dir(drive_root, malicious)
    resolved = state_path.resolve()
    state_root_resolved = (drive_root / "state" / "skills").resolve()
    # The returned path must stay under data/state/skills/.
    assert resolved.is_relative_to(state_root_resolved)


# ---------------------------------------------------------------------------
# Hidden-directory filter: relative-parts only, not absolute parts
# ---------------------------------------------------------------------------


def test_payload_hash_works_in_hidden_parent_dir(tmp_path):
    """Regression: ``_iter_payload_files`` used to drop every payload when
    the skills checkout lived in a hidden parent directory (e.g.
    ``~/.skills``) because it checked absolute ``path.parts`` for
    dotfile components."""
    # Build the skill inside a hidden parent so the resolved absolute
    # path of each payload file contains a ``.xyz`` component.
    hidden_root = tmp_path / ".xyz"
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    skill_dir = hidden_root / "weather"
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_valid_script_manifest(), encoding="utf-8")
    (skill_dir / "scripts" / "fetch.py").write_text("print('hi')\n", encoding="utf-8")

    hashed = compute_content_hash(skill_dir)
    # Hash must cover the script, not just the manifest.
    loaded = load_skill(skill_dir, drive_root)
    assert loaded is not None
    assert loaded.content_hash == hashed
    assert hashed != compute_content_hash(skill_dir.parent / "does-not-exist")

    (skill_dir / "scripts" / "fetch.py").write_text("print('edited')\n", encoding="utf-8")
    assert compute_content_hash(skill_dir) != hashed


# ---------------------------------------------------------------------------
# Manifest entry file is part of the hash (extension-type skills)
# ---------------------------------------------------------------------------


def test_manifest_entry_file_is_hashed_and_invalidates_review(tmp_path):
    """A ``type: extension`` skill's ``entry`` file (e.g. ``plugin.py``)
    must be part of the content hash so editing it staleness-invalidates
    the review. This is the Phase 3 round 2 regression for
    ``_iter_payload_files``."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    manifest = (
        "---\n"
        "name: ext1\n"
        "type: extension\n"
        "version: 0.1.0\n"
        "entry: plugin.py\n"
        "permissions: [widget]\n"
        "---\n"
        "body\n"
    )
    skill_dir = repo_root / "ext1"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(manifest, encoding="utf-8")
    (skill_dir / "plugin.py").write_text("def register(api): pass  # v1\n", encoding="utf-8")

    loaded = load_skill(skill_dir, drive_root)
    assert loaded is not None
    before = loaded.content_hash

    # Edit plugin.py — this must change the hash because the manifest
    # declared it as the entry file.
    (skill_dir / "plugin.py").write_text("def register(api): pass  # v2\n", encoding="utf-8")
    after = compute_content_hash(skill_dir, manifest_entry="plugin.py")
    assert before != after, (
        "Editing the manifest-declared entry file must invalidate the "
        "skill content hash so the review goes stale."
    )


def test_manifest_scripts_outside_scripts_dir_are_hashed(tmp_path):
    """Phase 3 round 6 regression: a manifest ``scripts[].name`` that points
    outside the conventional ``scripts/`` directory (e.g. ``bin/run.sh``)
    must be included in the content hash.

    Before this fix ``skill_exec`` would still execute the declared file,
    but ``compute_content_hash`` ignored it — editing that file would
    NOT stale-invalidate the review, so a malicious skill could ship a
    reviewed manifest and then mutate the actual runnable file.
    """
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = repo_root / "weird"
    (skill_dir / "bin").mkdir(parents=True)
    (skill_dir / "bin" / "run.sh").write_text("#!/bin/sh\necho 'v1'\n", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: weird\n"
            "description: Runs a non-scripts/ script.\n"
            "version: 0.1.0\n"
            "type: script\n"
            "runtime: bash\n"
            "timeout_sec: 5\n"
            "scripts:\n"
            "  - name: bin/run.sh\n"
            "    description: The actual runnable.\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    loaded = load_skill(skill_dir, drive_root)
    assert loaded is not None
    before = loaded.content_hash
    (skill_dir / "bin" / "run.sh").write_text("#!/bin/sh\necho 'v2'\n", encoding="utf-8")
    after = compute_content_hash(
        skill_dir,
        manifest_entry=loaded.manifest.entry,
        manifest_scripts=loaded.manifest.scripts,
    )
    assert before != after, (
        "Editing a manifest-declared script outside scripts/ must "
        "invalidate the skill content hash so the review goes stale."
    )


def test_manifest_entry_outside_skill_dir_is_rejected(tmp_path):
    """A malicious manifest ``entry: ../../etc/passwd`` must not cause
    the hasher to follow the absolute path."""
    drive_root = tmp_path / "drive"
    drive_root.mkdir()
    repo_root = tmp_path / "skills"
    skill_dir = repo_root / "ext1"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        (
            "---\n"
            "name: ext1\n"
            "type: extension\n"
            "version: 0.1.0\n"
            "entry: ../../etc/passwd\n"
            "permissions: [widget]\n"
            "---\n"
            "body\n"
        ),
        encoding="utf-8",
    )
    loaded = load_skill(skill_dir, drive_root)
    # The loader must still succeed (parse error would be a separate
    # finding) but ``compute_content_hash`` must ignore the escape path.
    assert loaded is not None
    # Hash is non-empty (manifest counts) but does not include
    # /etc/passwd content.
    assert loaded.content_hash
