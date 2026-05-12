"""Skills layout contracts (runtime topology auto-migration removed in v5.17.x)."""

from __future__ import annotations

import pathlib

from ouroboros.config import ensure_data_skills_dir


def test_ensure_data_skills_dir_creates_native_and_external(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "data"
    skills = ensure_data_skills_dir(root)
    assert skills.is_dir()
    assert (skills / "native").is_dir()
    assert (skills / "external").is_dir()


def test_native_payload_without_seed_marker_is_not_auto_relocated(tmp_path: pathlib.Path) -> None:
    """Unseeded native directories are no longer moved at runtime; users must fix layout."""
    root = tmp_path / "data"
    skills = ensure_data_skills_dir(root)
    native = skills / "native" / "myskill"
    native.mkdir(parents=True)
    (native / "SKILL.md").write_text("---\nname: myskill\n---\n", encoding="utf-8")
    assert native.is_dir()
    assert not (native / ".seed-origin").exists()
    assert (native / "SKILL.md").is_file()


def test_external_skill_payload_stays_under_external(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "data"
    skills = ensure_data_skills_dir(root)
    ext = skills / "external" / "foo"
    ext.mkdir(parents=True)
    (ext / "SKILL.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
    assert (ext / "SKILL.md").is_file()
