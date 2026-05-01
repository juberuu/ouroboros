from __future__ import annotations

from pathlib import Path

from ouroboros.skill_migrations import migrate_generation_skill_names


def _write_skill(root: Path, name: str, version: str, description: str):
    d = root / "skills" / "external" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n", encoding="utf-8")
    return d


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
