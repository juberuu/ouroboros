"""Opt-in schema-version helpers for durable state files.

Phase 1 only introduces *groundwork* for schema versioning — it does **not**
touch existing readers/writers. The concrete migration engine lives later;
this module gives us:

- a canonical key name (``_schema_version``) to embed in JSON files;
- a tiny ``with_schema_version(payload, version)`` helper callers can use
  when they intentionally opt in;
- a tolerant ``read_schema_version`` that treats missing values as ``0``
  (legacy / pre-versioned) so the existing format keeps working.

Current state of each relevant durable file (factual as of this change):

=========================   =========================   ==================
File                        Versioning present?         Current version
=========================   =========================   ==================
``advisory_review.json``    yes, explicit ``state_version`` in
                            ``ouroboros/review_state.py``                3
``state.json``              no — ``ensure_state_defaults`` only          0 (legacy)
``queue_snapshot.json``     no                                            0 (legacy)
``task_results/*.json``     no                                            0 (legacy)
``settings.json``           no — ``SETTINGS_DEFAULTS`` is the shape SSOT   0 (legacy)
=========================   =========================   ==================

The helpers in this module do not attempt to retro-fit versions into
existing files on disk. They only make it trivial for future code to start
writing versioned payloads without defining its own key name.

The key ``_schema_version`` (with leading underscore) is chosen to avoid
collisions with the existing ``state_version`` inside ``advisory_review.json``
(which is the authority for that file) and to mark the field as
runtime-metadata rather than domain data.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping


SCHEMA_VERSION_KEY = "_schema_version"


def with_schema_version(payload: Mapping[str, Any], version: int) -> Dict[str, Any]:
    """Return a shallow copy of ``payload`` with ``SCHEMA_VERSION_KEY`` set.

    Key shapes are preserved exactly as given — ``dict(payload)`` rather
    than a coercing comprehension, so a payload that (for whatever reason)
    distinguishes ``1`` from ``"1"`` round-trips unchanged. Downstream JSON
    serialisation will reject non-string keys on its own terms; this helper
    does not silently lossy-convert them.

    Never mutates the input. If the input is not a mapping, raises ``TypeError``
    so callers catch the programmer error early.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(
            f"with_schema_version expects a mapping, got {type(payload).__name__}"
        )
    out: Dict[str, Any] = dict(payload)
    out[SCHEMA_VERSION_KEY] = int(version)
    return out


def read_schema_version(payload: Any, default: int = 0) -> int:
    """Return the declared schema version or ``default`` if missing/invalid.

    - If ``payload`` is not a mapping, returns ``default``.
    - If ``SCHEMA_VERSION_KEY`` is absent, returns ``default``.
    - If the value cannot be coerced to ``int``, returns ``default``.

    Callers can pass ``0`` to signal "legacy / pre-versioned" explicitly.
    """
    if not isinstance(payload, Mapping):
        return int(default)
    raw = payload.get(SCHEMA_VERSION_KEY, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)


__all__ = [
    "SCHEMA_VERSION_KEY",
    "with_schema_version",
    "read_schema_version",
]
