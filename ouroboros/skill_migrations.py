"""One-shot runtime migrations for shipped/official skill names."""

from __future__ import annotations

import json
import pathlib
import shutil
from typing import Dict

from ouroboros.config import DATA_DIR, ensure_data_skills_dir


_RENAME_SPECS = {
    "image_gen": {
        "new": "nanobanana",
        "signature": ("name: image_gen", "version: 0.2.0", "Nano Banana / Gemini Flash Image"),
        "replacements": {
            "image_gen": "nanobanana",
            "Image generator": "Nano Banana",
            "Image generation widget": "Nano Banana image generation widget",
            "google/gemini-" + "2.5-flash-image-preview": "google/gemini-3.1-flash-image-preview",
            "Nano Banana (Gemini 2.5 Flash)": "Nano Banana (Gemini 3.1 Flash)",
        },
    },
    "audio_gen": {
        "new": "music_gen",
        "signature": ("name: audio_gen", "version: 0.5.0", "Google Lyria"),
        "replacements": {
            "audio_gen": "music_gen",
            "AudioGen": "MusicGen",
            "Audio generation": "Music generation",
        },
    },
}


def _rewrite_text_files(root: pathlib.Path, replacements: Dict[str, str]) -> None:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        new_text = text
        for old, new in replacements.items():
            new_text = new_text.replace(old, new)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")


def _looks_like_known_legacy_skill(payload_dir: pathlib.Path, signature: tuple[str, ...]) -> bool:
    try:
        text = (payload_dir / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return all(token in text for token in signature)


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
            data = json.loads(skill_json.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return False
            data["name"] = new_name
            skill_json.write_text(
                json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            return True
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
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


def migrate_generation_skill_names(data_dir: pathlib.Path | None = None) -> None:
    """Move legacy local generation skills into official OuroborosHub names.

    The migration is intentionally conservative: it only copies when the
    destination is absent, then renames the old payload directory aside as a
    ``.replaced-5.5.0`` backup so discovery ignores it.
    """

    data = pathlib.Path(data_dir or DATA_DIR)
    skills_root = ensure_data_skills_dir(data)
    external_root = skills_root / "external"
    hub_root = skills_root / "ouroboroshub"
    state_root = data / "state" / "skills"
    hub_root.mkdir(parents=True, exist_ok=True)
    for old_name, spec in _RENAME_SPECS.items():
        new_name = str(spec["new"])
        old_payload = external_root / old_name
        new_payload = hub_root / new_name
        if (
            old_payload.is_dir()
            and not new_payload.exists()
            and _looks_like_known_legacy_skill(old_payload, tuple(spec["signature"]))
        ):
            shutil.copytree(old_payload, new_payload)
            _rewrite_text_files(new_payload, dict(spec["replacements"]))
            (new_payload / ".ouroboroshub.json").write_text(
                f'{{"schema_version":1,"source":"ouroboroshub","slug":"{new_name}","migrated_from":"{old_name}"}}\n',
                encoding="utf-8",
            )
            backup = old_payload.with_name(f"{old_payload.name}.replaced-5.5.0")
            if not backup.exists():
                old_payload.rename(backup)
        old_state = state_root / old_name
        new_state = state_root / new_name
        if old_state.is_dir() and not new_state.exists():
            shutil.copytree(old_state, new_state)
