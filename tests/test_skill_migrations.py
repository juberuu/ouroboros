from __future__ import annotations

import importlib.util
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_legacy_skill_migrations_module_is_retired():
    """The v5.8 native-to-external topology repair window is closed."""
    assert importlib.util.find_spec("ouroboros.skill_migrations") is None


def test_server_startup_no_longer_runs_native_topology_migration():
    source = (REPO / "server.py").read_text(encoding="utf-8")
    assert "skill_migrations" not in source
    assert "migrate_unseeded_native_skills_to_external" not in source


def test_extensions_index_no_longer_mutates_skill_topology_on_read():
    source = (REPO / "ouroboros" / "gateway" / "extensions.py").read_text(encoding="utf-8")
    assert "skill_migrations" not in source
    assert "migrate_unseeded_native_skills_to_external" not in source


def _write_bucket_skill(root: Path, bucket: str, name: str, *, manifest_name=None):
    d = root / "skills" / bucket / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {manifest_name or name}\ndescription: test\nversion: 1.0.0\n---\n",
        encoding="utf-8",
    )
    return d


def test_unseeded_native_payloads_are_left_for_manual_upgrade(tmp_path):
    native = _write_bucket_skill(tmp_path, "native", "anime_shorts")
    external_root = tmp_path / "skills" / "external"
    external_root.mkdir(parents=True)

    assert native.exists()
    assert not (external_root / "anime_shorts").exists()


def test_seeded_native_payloads_remain_in_native_bucket(tmp_path):
    seeded = _write_bucket_skill(tmp_path, "native", "weather")
    (seeded / ".seed-origin").write_text("seeded_from=test\n", encoding="utf-8")

    assert seeded.exists()
    assert not (tmp_path / "skills" / "external" / "weather").exists()


def test_existing_external_payloads_are_not_collided_by_retired_migration(tmp_path):
    _write_bucket_skill(tmp_path, "external", "anime_shorts")
    native = _write_bucket_skill(tmp_path, "native", "anime_shorts")
    old_state = tmp_path / "state" / "skills" / "anime_shorts"
    old_state.mkdir(parents=True)
    (old_state / "review.json").write_text('{"status":"pass"}\n', encoding="utf-8")
    (old_state / "enabled.json").write_text('{"enabled":true}\n', encoding="utf-8")
    (old_state / "jobs").mkdir()

    assert native.exists()
    assert not (tmp_path / "skills" / "external" / "anime_shorts_migrated").exists()
    assert (old_state / "review.json").exists()
    assert (old_state / "enabled.json").exists()

