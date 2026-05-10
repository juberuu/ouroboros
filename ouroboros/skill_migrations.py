"""One-shot runtime migrations for shipped/official skill names.

v5.15.0: removed ``migrate_generation_skill_names`` (image_gen → nanobanana,
audio_gen → music_gen) — the rename window for users upgrading from
pre-OuroborosHub builds (pre-v5.5) is closed. Users still on those layouts
need a clean reinstall.
"""

from __future__ import annotations

import pathlib
import shutil
from typing import Dict

from ouroboros.config import DATA_DIR, ensure_data_skills_dir
from ouroboros.utils import atomic_write_json, read_json_dict


def _unique_external_name(external_root: pathlib.Path, base_name: str) -> str:
    candidate = base_name
    if not (external_root / candidate).exists():
        return candidate
    stem = f"{base_name}_migrated"
    candidate = stem
    idx = 2
    while (external_root / candidate).exists():
        candidate = f"{stem}_{idx}"
        idx += 1
    return candidate


def _rewrite_manifest_name(payload_dir: pathlib.Path, new_name: str) -> bool:
    skill_json = payload_dir / "skill.json"
    if skill_json.is_file():
        try:
            data = read_json_dict(skill_json)
            if data is None:
                return False
            data["name"] = new_name
            atomic_write_json(skill_json, data, trailing_newline=True)
            return True
        except OSError:
            return False

    skill_md = payload_dir / "SKILL.md"
    if not skill_md.is_file():
        return False
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.startswith("---"):
        return False
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            break
        if lines[idx].startswith("name:"):
            lines[idx] = f"name: {new_name}"
            skill_md.write_text("\n".join(lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
            return True
    return False


def _copy_state_for_migrated_identity(
    data: pathlib.Path,
    old_name: str,
    new_name: str,
    *,
    trust_state_is_stale: bool,
) -> None:
    if old_name == new_name:
        return
    state_root = data / "state" / "skills"
    old_state = state_root / old_name
    new_state = state_root / new_name
    if not old_state.is_dir() or new_state.exists():
        return
    try:
        shutil.copytree(old_state, new_state)
    except OSError:
        return
    if not trust_state_is_stale:
        return
    for filename in ("enabled.json", "review.json", "grants.json", "deps.json"):
        try:
            (new_state / filename).unlink()
        except OSError:
            pass


def migrate_unseeded_native_skills_to_external(data_dir: pathlib.Path | None = None) -> Dict[str, str]:
    """Relocate user-managed skills that were accidentally left in ``native/``.

    ``data/skills/native`` is reserved for launcher-seeded skills carrying a
    per-skill ``.seed-origin`` marker. A directory without that marker is
    user-managed content, so leaving it under ``native/`` creates a dead end:
    discovery honestly reports ``source=external`` while Repair rejects the
    physical ``skills/native/...`` payload root. This migration restores the
    topology by moving such payloads into ``external/``.
    """

    data = pathlib.Path(data_dir or DATA_DIR)
    skills_root = ensure_data_skills_dir(data)
    native_root = skills_root / "native"
    external_root = skills_root / "external"
    external_root.mkdir(parents=True, exist_ok=True)
    migrated: Dict[str, str] = {}
    if not native_root.is_dir():
        return migrated

    for payload in sorted(native_root.iterdir()):
        if not payload.is_dir() or payload.name.startswith(".") or ".replaced-" in payload.name:
            continue
        if (payload / ".seed-origin").is_file():
            continue
        if not any((payload / candidate).is_file() for candidate in ("SKILL.md", "skill.json")):
            continue
        old_name = payload.name
        new_name = _unique_external_name(external_root, old_name)
        target = external_root / new_name
        try:
            payload.rename(target)
        except OSError:
            try:
                shutil.copytree(payload, target)
                shutil.rmtree(payload)
            except OSError:
                if target.exists():
                    shutil.rmtree(target, ignore_errors=True)
                continue
        if new_name != old_name:
            if not _rewrite_manifest_name(target, new_name):
                # Keep the payload discoverable even if the manifest cannot be
                # rewritten; discovery will surface the load error/collision.
                pass
            _copy_state_for_migrated_identity(
                data,
                old_name,
                new_name,
                trust_state_is_stale=True,
            )
        migrated[old_name] = new_name
    return migrated
