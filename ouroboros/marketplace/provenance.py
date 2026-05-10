"""Durable provenance records for ClawHub-installed skills.

Stored at ``data/state/skills/<sanitized_name>/clawhub.json`` next to the
existing ``enabled.json`` and ``review.json``. Mirrors the atomic-write
pattern used by :mod:`ouroboros.skill_loader` so concurrent reviews +
toggles never see a half-written file.

The record is intentionally append-only at the field level: every field
that the marketplace populates at install time is preserved across
``Update`` operations so the operator can always cross-reference the
current installed slug + version + sha256 against the registry record.

Schema (v1)::

    {
        "schema_version": 1,
        "source": "clawhub",
        "slug": "owner/skill",
        "sanitized_name": "owner__skill",
        "version": "1.0.0",
        "sha256": "<archive sha256>",
        "is_plugin": false,
        "installed_at": "2026-04-25T...",
        "updated_at": "2026-04-25T...",
        "homepage": "https://...",
        "license": "MIT",
        "primary_env": "GEMINI_API_KEY",
        "original_manifest_sha256": "<sha256 of OpenClaw SKILL.md>",
        "translated_manifest_sha256": "<sha256 of adapted SKILL.md>",
        "adapter_warnings": [...],
        "registry_url": "https://clawhub.ai/api/v1"
    }
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any, Dict, Optional

from ouroboros.skill_loader import skill_state_dir
from ouroboros.utils import atomic_write_json, read_json_dict, utc_now_iso

log = logging.getLogger(__name__)


_SCHEMA_VERSION = 1
PROVENANCE_FILENAME = "clawhub.json"


def write_provenance(
    drive_root: pathlib.Path,
    skill_name: str,
    record: Dict[str, Any],
) -> pathlib.Path:
    """Persist a provenance record + return its path on disk.

    Existing fields are preserved when present in ``record``; the helper
    forces ``schema_version=1``, ``source='clawhub'``, ``updated_at``
    (UTC ISO8601 now), and ``installed_at`` (only when not already set).
    """
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    payload = dict(record or {})
    payload.setdefault("schema_version", _SCHEMA_VERSION)
    payload.setdefault("source", "clawhub")
    now_iso = utc_now_iso()
    payload.setdefault("installed_at", now_iso)
    payload["updated_at"] = now_iso
    atomic_write_json(target, payload, trailing_newline=True)
    return target


def read_provenance(
    drive_root: pathlib.Path,
    skill_name: str,
) -> Optional[Dict[str, Any]]:
    """Return the persisted provenance for ``skill_name`` or ``None``."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    if not target.is_file():
        return None
    return read_json_dict(target)


def delete_provenance(drive_root: pathlib.Path, skill_name: str) -> None:
    """Remove the provenance file (idempotent)."""
    state_dir = skill_state_dir(drive_root, skill_name)
    target = state_dir / PROVENANCE_FILENAME
    try:
        if target.is_file():
            target.unlink()
    except OSError:
        log.warning("Failed to delete provenance file %s", target, exc_info=True)


__all__ = [
    "PROVENANCE_FILENAME",
    "delete_provenance",
    "read_provenance",
    "write_provenance",
]
