from __future__ import annotations

from pathlib import Path

from ouroboros.skill_migrations import (
    migrate_generation_skill_names,
    migrate_unseeded_native_skills_to_external,
)


def _write_skill(root: Path, name: str, version: str, description: str):
    d = root / "skills" / "external" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n", encoding="utf-8")
    return d


def _write_bucket_skill(root: Path, bucket: str, name: str, *, manifest_name: str | None = None):
    d = root / "skills" / bucket / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {manifest_name or name}\ndescription: test\nversion: 1.0.0\n---\n",
        encoding="utf-8",
    )
    return d


def test_migrate_unseeded_native_skill_to_external(tmp_path):
    native = _write_bucket_skill(tmp_path, "native", "anime_shorts")

    migrated = migrate_unseeded_native_skills_to_external(tmp_path)

    target = tmp_path / "skills" / "external" / "anime_shorts"
    assert migrated == {"anime_shorts": "anime_shorts"}
    assert target.exists()
    assert not native.exists()


def test_migrate_unseeded_native_keeps_seeded_skills(tmp_path):
    seeded = _write_bucket_skill(tmp_path, "native", "weather")
    (seeded / ".seed-origin").write_text("seeded_from=test\n", encoding="utf-8")

    migrated = migrate_unseeded_native_skills_to_external(tmp_path)

    assert migrated == {}
    assert seeded.exists()
    assert not (tmp_path / "skills" / "external" / "weather").exists()


def test_migrate_unseeded_native_collision_rewrites_identity_and_stales_state(tmp_path):
    _write_bucket_skill(tmp_path, "external", "anime_shorts")
    _write_bucket_skill(tmp_path, "native", "anime_shorts")
    old_state = tmp_path / "state" / "skills" / "anime_shorts"
    old_state.mkdir(parents=True)
    (old_state / "review.json").write_text('{"status":"pass"}\n', encoding="utf-8")
    (old_state / "enabled.json").write_text('{"enabled":true}\n', encoding="utf-8")
    (old_state / "jobs").mkdir()

    migrated = migrate_unseeded_native_skills_to_external(tmp_path)

    target = tmp_path / "skills" / "external" / "anime_shorts_migrated"
    new_state = tmp_path / "state" / "skills" / "anime_shorts_migrated"
    assert migrated == {"anime_shorts": "anime_shorts_migrated"}
    assert target.exists()
    assert "name: anime_shorts_migrated" in (target / "SKILL.md").read_text(encoding="utf-8")
    assert (new_state / "jobs").is_dir()
    assert not (new_state / "review.json").exists()
    assert not (new_state / "enabled.json").exists()


def test_migrate_generation_skill_names_skips_arbitrary_external_skill(tmp_path):
    old = _write_skill(tmp_path, "image_gen", "9.9.9", "private unrelated image skill")
    migrate_generation_skill_names(tmp_path)
    assert old.exists()
    assert not (tmp_path / "skills" / "ouroboroshub" / "nanobanana").exists()


def test_migrate_generation_skill_names_moves_known_legacy_skill(tmp_path):
    old = _write_skill(tmp_path, "image_gen", "0.2.0", "Generate images from a text prompt via OpenRouter's image generation API (Nano Banana / Gemini Flash Image).")
    migrate_generation_skill_names(tmp_path)
    migrated = tmp_path / "skills" / "ouroboroshub" / "nanobanana"
    assert migrated.exists()
    assert (migrated / ".ouroboroshub.json").is_file()
    assert not old.exists()
    assert old.with_name("image_gen.replaced-5.5.0").exists()
