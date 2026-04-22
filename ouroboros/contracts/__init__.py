"""
Ouroboros — Frozen contracts (v1).

This package defines the minimal, stable ABI between the three logical
layers described in ``docs/ARCHITECTURE.md``:

- ``core``           — runtime kernel, safety, bundle-managed surfaces
- ``evolutionary``   — agent, review, memory, UI, supervisor (the "body")
- ``skill layer``    — external skills/extensions (v1: local checkout)

Phase 1 intentionally keeps this surface small:

- ``ToolContextProtocol`` — the minimum attribute + method set every tool
  handler relies on (6 attributes: ``repo_dir``, ``drive_root``,
  ``pending_events``, ``emit_progress_fn``, ``current_chat_id``, ``task_id``;
  3 methods: ``repo_path``, ``drive_path``, ``drive_logs``). The existing
  ``ouroboros.tools.registry.ToolContext`` dataclass already satisfies it
  structurally; no runtime code changes are needed for the protocol to "bind".

- ``ToolEntryProtocol`` / ``get_tools()`` shape — the ABI every
  ``ouroboros/tools/*`` module is expected to export.

- ``api_v1`` — ``TypedDict`` descriptions of the HTTP/WebSocket envelopes
  that ``server.py`` and ``supervisor/message_bus.py`` already emit. These
  are descriptive contracts, not runtime validators.

- ``SkillManifest`` — the unified ``SKILL.md`` / ``skill.json`` frontmatter
  shape used by the external skill repo (``type: instruction|script|extension``).

- ``schema_versions`` — a small, *opt-in* schema-version helper library
  that future code can layer on top of state files (``state.json``,
  ``queue_snapshot.json``, ``task_results/*.json``). Nothing existing
  depends on it yet; the runtime keeps working untouched.

The rule for this module: **add, do not mutate**. Anything that changes
existing runtime behaviour does not belong here.
"""

from __future__ import annotations

from ouroboros.contracts.tool_context import ToolContextProtocol
from ouroboros.contracts.tool_abi import ToolEntryProtocol, GetToolsProtocol
from ouroboros.contracts.skill_manifest import (
    SKILL_MANIFEST_SCHEMA_VERSION,
    VALID_SKILL_TYPES,
    VALID_SKILL_RUNTIMES,
    VALID_SKILL_PERMISSIONS,
    SkillManifest,
    SkillManifestError,
    parse_skill_manifest_text,
)
from ouroboros.contracts.schema_versions import (
    SCHEMA_VERSION_KEY,
    with_schema_version,
    read_schema_version,
)
from ouroboros.contracts.plugin_api import (
    PluginAPI,
    ExtensionRegistrationError,
    FORBIDDEN_EXTENSION_SETTINGS,
    VALID_EXTENSION_PERMISSIONS,
)

__all__ = [
    "ToolContextProtocol",
    "ToolEntryProtocol",
    "GetToolsProtocol",
    "SKILL_MANIFEST_SCHEMA_VERSION",
    "VALID_SKILL_TYPES",
    "VALID_SKILL_RUNTIMES",
    "VALID_SKILL_PERMISSIONS",
    "SkillManifest",
    "SkillManifestError",
    "parse_skill_manifest_text",
    "SCHEMA_VERSION_KEY",
    "with_schema_version",
    "read_schema_version",
    "PluginAPI",
    "ExtensionRegistrationError",
    "FORBIDDEN_EXTENSION_SETTINGS",
    "VALID_EXTENSION_PERMISSIONS",
]
