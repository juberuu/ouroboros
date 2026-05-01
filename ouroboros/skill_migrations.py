"""One-shot runtime migrations for shipped/official skill names."""

from __future__ import annotations

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
