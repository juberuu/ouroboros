"""Tests for the v4.50 skill-storage refactor.

Covers:

- Bootstrap copies seed skills from ``repo/skills/`` into
  ``data/skills/native/`` exactly once and skips on re-run.
- ``discover_skills`` walks the data plane recursively into
  ``native/``, ``clawhub/``, ``external/`` plus the optional
  ``OUROBOROS_SKILLS_REPO_PATH`` checkout, populating the new
  ``source`` field correctly.
- Collision detection still surfaces ``load_error`` for
  duplicate sanitised names across roots.
"""

from __future__ import annotations

import textwrap

import pytest


SKILL_TEMPLATE = textwrap.dedent(
    """
    ---
    name: NAME
    description: Test fixture skill.
    version: 0.1.0
    type: instruction
    ---

    # NAME
    """
).strip() + "\n"


@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Build a hermetic data/repo layout for storage tests.

    Patches ``ouroboros.config.DATA_DIR`` / ``REPO_DIR`` directly with
    ``monkeypatch.setattr`` so the changes are reverted at teardown and
    no module reloads are necessary (which would otherwise create a
    fresh ``LoadedSkill`` class object that breaks ``isinstance``
    checks in unrelated tests).
    """
    data_dir = tmp_path / "data"
    repo_dir = tmp_path / "repo"
    data_dir.mkdir()
    repo_dir.mkdir()
    monkeypatch.setattr("ouroboros.config.DATA_DIR", data_dir, raising=True)
    monkeypatch.setattr("ouroboros.config.REPO_DIR", repo_dir, raising=True)
    yield data_dir, repo_dir


def _write_skill(parent, slug):
    skill_dir = parent / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        SKILL_TEMPLATE.replace("NAME", slug),
        encoding="utf-8",
    )
    return skill_dir


def test_bootstrap_copies_seed_once(isolated_data, monkeypatch):
    data_dir, repo_dir = isolated_data
    seed_dir = repo_dir / "skills"
    seed_dir.mkdir()
    _write_skill(seed_dir, "weather")

    from ouroboros.launcher_bootstrap import ensure_data_skills_seeded

    copied = ensure_data_skills_seeded()
    assert copied == 1
    target = data_dir / "skills" / "native" / "weather"
    assert target.is_dir()
    assert (target / "SKILL.md").is_file()

    # Re-run is idempotent.
    copied2 = ensure_data_skills_seeded()
    assert copied2 == 0


def test_bootstrap_skips_when_target_already_populated(isolated_data):
    data_dir, repo_dir = isolated_data
    seed_dir = repo_dir / "skills"
    seed_dir.mkdir()
    _write_skill(seed_dir, "weather")

    target_native = data_dir / "skills" / "native"
    target_native.mkdir(parents=True)
    _write_skill(target_native, "user-skill")  # user-managed; bootstrap must not clobber

    from ouroboros.launcher_bootstrap import ensure_data_skills_seeded

    copied = ensure_data_skills_seeded()
    assert copied == 0
    assert (target_native / "user-skill").is_dir()
    # weather seed must NOT have been copied because target was non-empty.
    assert not (target_native / "weather").exists()


def test_discover_walks_three_buckets(isolated_data):
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    weather_dir = _write_skill(skills_root / "native", "weather")
    # v4.50 cycle-1 Ouroboros O3 fix: native bucket only carries the
    # ``native`` source tag when the launcher's ``.seed-origin`` marker
    # is present.
    (weather_dir / ".seed-origin").write_text("seeded_from=test\n", encoding="utf-8")

    clawhub_dir = _write_skill(skills_root / "clawhub", "owner__hello")
    # v4.50 cycle-2 Ouroboros review fix: clawhub bucket likewise needs
    # ``.clawhub.json`` provenance to carry the ``clawhub`` tag.
    (clawhub_dir / ".clawhub.json").write_text(
        '{"source": "clawhub"}', encoding="utf-8"
    )

    _write_skill(skills_root / "external", "personal")

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert set(by_name) >= {"weather", "owner__hello", "personal"}
    assert by_name["weather"].source == "native"
    assert by_name["owner__hello"].source == "clawhub"
    assert by_name["personal"].source == "external"


def test_discover_includes_user_repo(isolated_data, tmp_path, monkeypatch):
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    weather_dir = _write_skill(skills_root / "native", "weather")
    (weather_dir / ".seed-origin").write_text("seeded_from=test\n", encoding="utf-8")
    user_root = tmp_path / "user_skills"
    _write_skill(user_root, "my-skill")

    monkeypatch.setenv("OUROBOROS_SKILLS_REPO_PATH", str(user_root))

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=str(user_root))
    by_name = {s.name: s for s in skills}
    assert "weather" in by_name and by_name["weather"].source == "native"
    assert "my-skill" in by_name and by_name["my-skill"].source == "user_repo"


def test_collision_detection_surfaces_load_error(isolated_data):
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    _write_skill(skills_root / "native", "weather")
    _write_skill(skills_root / "clawhub", "weather")

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    weather_entries = [s for s in skills if s.name == "weather"]
    # Both entries should now carry a load_error.
    assert len(weather_entries) == 2
    for entry in weather_entries:
        assert "Skill name collision" in entry.load_error


def test_legacy_repo_skills_fallback_when_data_plane_empty(
    isolated_data, monkeypatch
):
    data_dir, repo_dir = isolated_data
    seed_dir = repo_dir / "skills"
    seed_dir.mkdir()
    _write_skill(seed_dir, "legacy-fixture")

    # Override the auto-mocked _bundled_skills_dir from conftest by
    # restoring the real implementation but pointing it at our seed.
    import ouroboros.skill_loader as loader_mod
    monkeypatch.setattr(loader_mod, "_bundled_skills_dir", lambda: seed_dir)

    skills = loader_mod.discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert "legacy-fixture" in by_name


def test_walk_skill_packages_handles_root_being_a_skill(isolated_data):
    data_dir, _repo_dir = isolated_data
    user_root = data_dir / "single_skill"
    user_root.mkdir()
    _write_skill(user_root, "alone")
    # The user could legitimately point OUROBOROS_SKILLS_REPO_PATH at a
    # parent dir of the skill — confirm we discover it via the standard walk.
    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=str(user_root))
    by_name = {s.name: s for s in skills}
    assert "alone" in by_name


# ---------------------------------------------------------------------------
# Cycle 1 Ouroboros review fixes
# ---------------------------------------------------------------------------


def test_bootstrap_marker_prevents_resurrection_after_user_deletion(
    isolated_data, monkeypatch
):
    """v4.50 cycle-1 Ouroboros review O1.

    User deletes every native skill (legitimate per the docstring).
    A subsequent bootstrap must NOT resurrect them — the marker file
    is the durable "bootstrap-already-happened" anchor.
    """
    data_dir, repo_dir = isolated_data
    seed_dir = repo_dir / "skills"
    seed_dir.mkdir()
    _write_skill(seed_dir, "weather")

    from ouroboros.launcher_bootstrap import (
        ensure_data_skills_seeded,
        _SEED_COMPLETE_MARKER,
    )

    copied_first = ensure_data_skills_seeded()
    assert copied_first == 1
    native_root = data_dir / "skills" / "native"
    marker = native_root / _SEED_COMPLETE_MARKER
    assert marker.is_file()

    # User explicitly deletes the seeded skill.
    import shutil
    shutil.rmtree(native_root / "weather")
    assert not (native_root / "weather").exists()
    # Marker survives.
    assert marker.is_file()

    # Re-running bootstrap MUST NOT resurrect "weather".
    copied_second = ensure_data_skills_seeded()
    assert copied_second == 0
    assert not (native_root / "weather").exists()


def test_user_dropped_skill_under_native_is_classified_external(isolated_data):
    """v4.50 cycle-1 Ouroboros review O3.

    A skill placed in ``data/skills/native/`` WITHOUT a ``.seed-origin``
    marker file (e.g. user manually dropped it) must be tagged
    ``external`` rather than ``native``. The ``native`` badge is
    reserved for launcher-seeded skills.
    """
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    _write_skill(skills_root / "native", "user-dropped")
    # No .seed-origin marker — user-introduced.

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert "user-dropped" in by_name
    assert by_name["user-dropped"].source == "external"


def test_seeded_skill_with_marker_keeps_native_tag(isolated_data):
    """Counterpart to test_user_dropped_skill_under_native_is_classified_external —
    a skill copied by the bootstrap (which writes ``.seed-origin``) keeps
    the ``native`` source tag."""
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    skill_dir = _write_skill(skills_root / "native", "seeded")
    (skill_dir / ".seed-origin").write_text("seeded_from=test\n", encoding="utf-8")

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert by_name["seeded"].source == "native"


def test_user_dropped_skill_under_clawhub_is_classified_external(isolated_data):
    """v4.50 cycle-2 Ouroboros review fix.

    A skill placed under ``data/skills/clawhub/`` WITHOUT a
    ``.clawhub.json`` provenance sidecar must be tagged ``external``,
    not ``clawhub``. The marketplace install pipeline ALWAYS writes
    that sidecar; its absence means the entry was hand-dropped, and
    the UI must NOT attach Update / Uninstall affordances to it.
    """
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    _write_skill(skills_root / "clawhub", "user-dropped-pretend-marketplace")
    # Note: no .clawhub.json sidecar.

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert "user-dropped-pretend-marketplace" in by_name
    assert by_name["user-dropped-pretend-marketplace"].source == "external"


def test_replaced_orphan_directory_is_filtered_from_discovery(isolated_data):
    """Cycle 2 critic finding (Gemini #2) — a leaked
    ``<slug>.replaced-<sha8>`` directory from an interrupted overwrite
    install must NOT surface as a phantom skill in discovery.
    """
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    live_dir = _write_skill(skills_root / "clawhub", "owner__live")
    (live_dir / ".clawhub.json").write_text('{"source": "clawhub"}', encoding="utf-8")
    orphan_dir = _write_skill(skills_root / "clawhub", "owner__live.replaced-deadbeef")

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert "owner__live" in by_name
    # Sanitised orphan name must not appear in the catalogue.
    assert all(".replaced-" not in s.name for s in skills)


def test_clawhub_skill_with_provenance_sidecar_keeps_clawhub_tag(isolated_data):
    """Counterpart — a clawhub skill that owns the marketplace
    provenance sidecar keeps the ``clawhub`` source tag."""
    data_dir, _repo_dir = isolated_data
    skills_root = data_dir / "skills"
    skill_dir = _write_skill(skills_root / "clawhub", "owner__real")
    (skill_dir / ".clawhub.json").write_text(
        '{"source": "clawhub", "slug": "owner/real", "version": "1.0.0"}',
        encoding="utf-8",
    )

    from ouroboros.skill_loader import discover_skills

    skills = discover_skills(data_dir, repo_path=None)
    by_name = {s.name: s for s in skills}
    assert by_name["owner__real"].source == "clawhub"


def test_bundled_fallback_does_not_fire_after_data_plane_initialised(
    isolated_data, monkeypatch
):
    """v4.50 cycle-1 Ouroboros review O2.

    The legacy ``_bundled_skills_dir()`` fallback must only activate
    when ``data/skills/`` does NOT exist at all. Once bootstrap has
    run (even to land zero skills), the user's empty data plane is
    a deliberate choice and must NOT silently resurrect from the
    bundled seed.
    """
    data_dir, repo_dir = isolated_data
    # Initialise the data plane (mkdirs + .bootstrap-seed-complete marker)
    # but leave native/ effectively empty.
    skills_root = data_dir / "skills"
    skills_root.mkdir(parents=True)
    (skills_root / "native").mkdir()
    (skills_root / "clawhub").mkdir()
    (skills_root / "external").mkdir()

    # Pretend a bundled seed exists with a "ghost" skill.
    seed_dir = repo_dir / "skills"
    seed_dir.mkdir()
    _write_skill(seed_dir, "ghost")

    import ouroboros.skill_loader as loader_mod
    monkeypatch.setattr(loader_mod, "_bundled_skills_dir", lambda: seed_dir)

    skills = loader_mod.discover_skills(data_dir, repo_path=None)
    # Empty data plane + initialised → fallback does NOT fire.
    assert skills == []
