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

import json
import logging
import os
import pathlib
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from ouroboros.skill_loader import skill_state_dir

log = logging.getLogger(__name__)


_SCHEMA_VERSION = 1
PROVENANCE_FILENAME = "clawhub.json"


def _atomic_write_json(path: pathlib.Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = (
        f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}"
    )
    tmp = path.with_name(tmp_name)
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


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
    now_iso = datetime.now(timezone.utc).isoformat()
    payload.setdefault("installed_at", now_iso)
    payload["updated_at"] = now_iso
    _atomic_write_json(target, payload)
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
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Failed to parse provenance file %s", target, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    return data


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
