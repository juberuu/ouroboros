"""Unified ``SKILL.md`` / ``skill.json`` manifest (v1).

One manifest format describes all three kinds of external packages:

- ``type: instruction`` — pure markdown guide, no executable payload.
- ``type: script``      — markdown guide + one or more scripts invoked
                          through the upcoming ``skill_exec`` tool.
- ``type: extension``   — markdown guide + ``plugin.py``-style entry plus
                          optional routes / ws handlers and future UI-tab
                          declarations.

The parser intentionally works on either::

    ---
    name: weather
    type: script
    ...
    ---
    # body (human readable instructions)
    ...

(YAML frontmatter in ``SKILL.md``) **or** a standalone ``skill.json`` file.

The parser is intentionally tolerant for missing optional fields and
unknown extras, but it FAILS CLOSED on structural contract damage:
invalid JSON/YAML, malformed structured fields (for example ``ui_tab``),
or an unsupported ``schema_version`` all raise ``SkillManifestError``.

To avoid adding a PyYAML dependency at this stage, the YAML frontmatter
parser is a *minimal* key: value reader that covers the subset we actually
use (scalars, inline lists, nested ``ui_tab`` block). When a consumer needs
richer YAML later, it can swap to ``yaml.safe_load`` without changing the
dataclass shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


SKILL_MANIFEST_SCHEMA_VERSION = 1

VALID_SKILL_TYPES = frozenset({"instruction", "script", "extension"})
VALID_SKILL_RUNTIMES = frozenset({"", "python", "python3", "node", "bash"})
VALID_SKILL_PERMISSIONS = frozenset(
    {
        "net",
        "fs",
        "subprocess",
        "widget",
        "ws_handler",
        # Phase 4 ``type: extension`` permissions — kept in sync with
        # ``ouroboros.contracts.plugin_api.VALID_EXTENSION_PERMISSIONS``
        # so ``SkillManifest.validate()`` does not warn "unknown
        # permission" on legitimate extension manifests that declare
        # these Phase-4 surfaces. The single frozen-set remains the
        # SSOT for both script-type and extension-type permissions.
        "route",
        "tool",
        "read_settings",
        "iframe_raw",
    }
)


class SkillManifestError(ValueError):
    """Raised when a manifest is structurally broken (not just missing fields)."""


@dataclass
class SkillManifest:
    """Structural description of one skill package.

    Fields marked optional default to empty values so the evolutionary layer
    can render partial skills in the UI with a ``needs_review`` badge.
    """

    name: str
    description: str
    version: str
    type: str  # instruction | script | extension
    when_to_use: str = ""
    requires: List[str] = field(default_factory=list)
    os: str = "any"
    runtime: str = ""
    timeout_sec: int = 60
    env_from_settings: List[str] = field(default_factory=list)
    # script-typed manifests list their scripts; each item is a mapping with
    # at least ``name`` and optionally ``description``.
    scripts: List[Dict[str, str]] = field(default_factory=list)
    # extension-typed manifests point at a Python entry module.
    entry: str = ""
    permissions: List[str] = field(default_factory=list)
    ui_tab: Optional[Dict[str, Any]] = None
    # Human-readable body from SKILL.md after the closing ``---`` line.
    body: str = ""
    # Anything we didn't understand, preserved for forward-compatibility.
    raw_extra: Dict[str, Any] = field(default_factory=dict)
    schema_version: int = SKILL_MANIFEST_SCHEMA_VERSION

    # --- Convenience --------------------------------------------------

    def is_instruction(self) -> bool:
        return self.type == "instruction"

    def is_script(self) -> bool:
        return self.type == "script"

    def is_extension(self) -> bool:
        return self.type == "extension"

    def validate(self) -> List[str]:
        """Return a list of non-blocking warnings for a parsed manifest.

        Blocking failures are raised by ``parse_skill_manifest_text``; this
        function describes *soft* issues useful to show in review output
        (unknown type, unknown runtime, permissions typo, etc.).
        """
        warnings: List[str] = []
        if self.type not in VALID_SKILL_TYPES:
            warnings.append(
                f"unknown type '{self.type}' (expected one of "
                f"{sorted(VALID_SKILL_TYPES)})"
            )
        if self.runtime not in VALID_SKILL_RUNTIMES:
            warnings.append(
                f"unknown runtime '{self.runtime}' (expected empty or one of "
                f"{sorted(r for r in VALID_SKILL_RUNTIMES if r)})"
            )
        for perm in self.permissions:
            if perm not in VALID_SKILL_PERMISSIONS:
                warnings.append(
                    f"unknown permission '{perm}' (expected one of "
                    f"{sorted(VALID_SKILL_PERMISSIONS)})"
                )
        if self.is_extension() and not self.entry:
            warnings.append("type=extension requires non-empty 'entry'")
        if self.is_script() and not self.scripts:
            warnings.append("type=script requires at least one entry in 'scripts'")
        if self.timeout_sec <= 0:
            warnings.append("timeout_sec must be positive")
        return warnings


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z",
    re.DOTALL,
)


def parse_skill_manifest_text(text: str) -> SkillManifest:
    """Parse a ``SKILL.md`` (frontmatter + body) or a ``skill.json`` document.

    Auto-detects which form the input is in:

    - Starts with ``{`` -> parsed as JSON.
    - Starts with ``---`` -> parsed as YAML-ish frontmatter, the trailing
      body becomes ``manifest.body``.
    - Otherwise treated as an instruction-only markdown file with no
      frontmatter; a best-effort ``name`` is derived from the first heading.

    Raises ``SkillManifestError`` only on structural damage. Missing optional
    fields become empty values; unknown fields are preserved in ``raw_extra``.
    """
    src = text.lstrip("\ufeff")  # strip BOM if any
    stripped = src.lstrip()

    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SkillManifestError(f"invalid skill.json: {exc}") from exc
        if not isinstance(data, dict):
            raise SkillManifestError("skill.json root must be a mapping")
        return _manifest_from_mapping(data, body="")

    match = _FRONTMATTER_RE.match(src)
    if match is not None:
        front, body = match.group(1), match.group(2) or ""
        # Prefer real YAML when PyYAML is available (handles nested
        # block mappings like ``metadata.openclaw.requires.env`` that
        # OpenClaw/ClawHub skills use). Fall back to the minimal
        # in-tree parser for environments without the dependency.
        try:
            import yaml  # type: ignore
            data: Any = yaml.safe_load(front) or {}
        except ImportError:
            try:
                data = _parse_minimal_yaml(front)
            except _MiniYamlError as exc:
                raise SkillManifestError(f"invalid SKILL.md frontmatter: {exc}") from exc
        except yaml.YAMLError as exc:  # type: ignore[name-defined]
            raise SkillManifestError(f"invalid SKILL.md frontmatter: {exc}") from exc
        if not isinstance(data, dict):
            raise SkillManifestError("SKILL.md frontmatter must be a mapping")
        return _manifest_from_mapping(data, body=body.strip())
    # Fallback: body-only markdown, treat as instruction skill.
    # ``stripped.startswith("---")`` is NOT treated as a broken frontmatter
    # fence here — a markdown document that legitimately starts with a
    # thematic break (``---`` on its own line) is a valid instruction
    # skill body. Real frontmatter parse failures (malformed YAML, bad
    # mapping shape) are caught by the branch above which only runs when
    # the full frontmatter regex actually matches.
    name = _derive_name_from_body(src)
    return SkillManifest(
        name=name,
        description="",
        version="",
        type="instruction",
        body=src.strip(),
        schema_version=SKILL_MANIFEST_SCHEMA_VERSION,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _manifest_from_mapping(data: Dict[str, Any], *, body: str) -> SkillManifest:
    known = {
        "name",
        "description",
        "version",
        "type",
        "when_to_use",
        "requires",
        "os",
        "runtime",
        "timeout_sec",
        "env_from_settings",
        "scripts",
        "entry",
        "permissions",
        "ui_tab",
        "schema_version",
    }
    extras: Dict[str, Any] = {
        key: value for key, value in data.items() if key not in known
    }

    timeout_raw = data.get("timeout_sec", 60)
    try:
        timeout_sec = int(timeout_raw) if timeout_raw not in (None, "") else 60
    except (TypeError, ValueError):
        timeout_sec = 60

    scripts_raw = data.get("scripts", [])
    scripts: List[Dict[str, str]] = []
    if scripts_raw in (None, ""):
        scripts_raw = []
    if not isinstance(scripts_raw, list):
        raise SkillManifestError("'scripts' must be a list when provided")
    for item in scripts_raw:
        if isinstance(item, dict):
            scripts.append({str(k): str(v) for k, v in item.items()})
        elif isinstance(item, str):
            scripts.append({"name": item})
        else:
            raise SkillManifestError("each 'scripts' item must be a mapping or string")

    ui_tab = data.get("ui_tab")
    if ui_tab is not None and not isinstance(ui_tab, dict):
        raise SkillManifestError("'ui_tab' must be a mapping when provided")

    schema_version = data.get("schema_version", SKILL_MANIFEST_SCHEMA_VERSION)
    try:
        schema_version_int = int(schema_version)
    except (TypeError, ValueError):
        raise SkillManifestError("'schema_version' must be an integer") from None
    if schema_version_int != SKILL_MANIFEST_SCHEMA_VERSION:
        raise SkillManifestError(
            f"unsupported schema_version {schema_version_int}; "
            f"expected {SKILL_MANIFEST_SCHEMA_VERSION}"
        )

    return SkillManifest(
        name=str(data.get("name") or "").strip(),
        description=str(data.get("description") or "").strip(),
        version=str(data.get("version") or "").strip(),
        type=str(data.get("type") or "instruction").strip().lower(),
        when_to_use=str(data.get("when_to_use") or "").strip(),
        requires=_string_list(data.get("requires")),
        os=str(data.get("os") or "any").strip().lower() or "any",
        runtime=str(data.get("runtime") or "").strip().lower(),
        timeout_sec=timeout_sec,
        env_from_settings=_string_list(data.get("env_from_settings")),
        scripts=scripts,
        entry=str(data.get("entry") or "").strip(),
        permissions=_string_list(data.get("permissions")),
        ui_tab=ui_tab,
        body=body,
        raw_extra=extras,
        schema_version=schema_version_int,
    )


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _derive_name_from_body(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip().lower().replace(" ", "_") or "unnamed"
    return "unnamed"


# ---------------------------------------------------------------------------
# Minimal YAML-ish frontmatter reader (no external dependency).
# ---------------------------------------------------------------------------


class _MiniYamlError(ValueError):
    pass


def _parse_minimal_yaml(text: str) -> Dict[str, Any]:
    """Parse the strict subset of YAML we allow in SKILL.md frontmatter.

    Supported:
      - ``key: value`` scalars (string, bool, int).
      - Inline sequences: ``key: [a, b, "c d"]``.
      - Block sequences with ``- item`` lines (scalars only).
      - Block sequences of mappings (``- name: foo\\n  description: bar``)
        but only one level deep (enough for ``scripts``).
      - A single nested mapping block for ``ui_tab: {…}`` via indentation.

    Not supported (explicitly rejected with ``_MiniYamlError``): anchors,
    tags, multiline scalars, complex nesting. If a manifest needs those,
    it should use ``skill.json`` or wait for Phase 4's full parser.
    """
    result: Dict[str, Any] = {}
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        if raw.startswith(" ") or raw.startswith("\t"):
            raise _MiniYamlError(
                f"top-level line must not start with whitespace: {raw!r}"
            )
        if ":" not in raw:
            raise _MiniYamlError(f"expected 'key: value', got: {raw!r}")
        key, _, rest = raw.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest == "":
            # Possible block value: either a block list (`- item`) or a
            # nested mapping. Scan following indented lines.
            block_lines, consumed = _collect_block(lines, i + 1)
            i += 1 + consumed
            if not block_lines:
                result[key] = ""
                continue
            # A block is a sequence if its *first non-empty* logical entry
            # starts with ``- ``. Indented continuation lines belonging to a
            # sequence item are not themselves prefixed with ``- `` and must
            # not demote the block to a plain mapping.
            first_non_empty = next(
                (ln for ln in block_lines if ln.strip()),
                "",
            )
            first_stripped = first_non_empty.lstrip()
            if first_stripped.startswith("- "):
                result[key] = _parse_block_sequence(block_lines)
            elif first_stripped.startswith("{") or first_stripped.startswith("["):
                # YAML flow block following a bare key (common in
                # OpenClaw-format manifests where ``metadata:`` opens a
                # multi-line JSON object). The structured block must
                # parse cleanly — otherwise the manifest is malformed.
                blob = "\n".join(ln for ln in block_lines if ln.strip())
                try:
                    result[key] = json.loads(blob)
                except Exception as exc:
                    raise _MiniYamlError(
                        f"invalid flow block for {key!r}: {exc}"
                    ) from exc
            else:
                result[key] = _parse_block_mapping(block_lines)
            continue
        if rest.startswith("[") and rest.endswith("]"):
            result[key] = _parse_inline_list(rest)
        elif rest.startswith("{") or rest.startswith("["):
            # YAML flow syntax (`{...}` / `[...]` possibly spanning
            # multiple lines, as used by OpenClaw-format ``metadata``).
            # We greedily consume subsequent lines until the bracket
            # balance returns to zero and then try ``json.loads`` — if
            # that fails the value stays as a raw string so the field
            # still round-trips without breaking the whole manifest.
            collected = [rest]
            depth = _bracket_depth(rest)
            j = i + 1
            while depth > 0 and j < len(lines):
                collected.append(lines[j])
                depth += _bracket_depth(lines[j])
                j += 1
            blob = "\n".join(collected)
            try:
                result[key] = json.loads(blob)
            except Exception as exc:
                raise _MiniYamlError(
                    f"invalid flow value for {key!r}: {exc}"
                ) from exc
            i = j
            continue
        else:
            result[key] = _coerce_scalar(rest)
        i += 1
    return result


def _bracket_depth(text: str) -> int:
    """Rough bracket/brace depth change — ignoring strings is fine for
    skill manifests since the YAML flow blocks in OpenClaw format
    don't contain unescaped quotes that would mess up the count."""
    depth = 0
    for ch in text:
        if ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth


def _collect_block(lines: List[str], start: int) -> Tuple[List[str], int]:
    block: List[str] = []
    consumed = 0
    for line in lines[start:]:
        if not line.strip():
            block.append(line)
            consumed += 1
            continue
        if not (line.startswith(" ") or line.startswith("\t")):
            break
        block.append(line)
        consumed += 1
    # Drop trailing empty lines.
    while block and not block[-1].strip():
        block.pop()
    return block, consumed


def _parse_block_sequence(block_lines: List[str]) -> List[Any]:
    items: List[Any] = []
    current: Dict[str, Any] | None = None
    for line in block_lines:
        stripped = line.lstrip()
        # Blank / comment-only lines never contribute to the sequence.
        # `_collect_block` intentionally preserves interior blank lines so
        # any layout tool that visualises block YAML (e.g. dash between list
        # items) still parses cleanly instead of raising or silently writing
        # an empty ``""`` key into the current item.
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("- "):
            if current is not None:
                items.append(current)
                current = None
            rest = stripped[2:].strip()
            if ":" in rest and not rest.startswith("["):
                key, _, val = rest.partition(":")
                current = {key.strip(): _coerce_scalar(val.strip())}
            elif rest.startswith("[") and rest.endswith("]"):
                items.append(_parse_inline_list(rest))
            else:
                items.append(_coerce_scalar(rest))
        else:
            if current is None:
                raise _MiniYamlError(
                    f"indented line without a current list item: {line!r}"
                )
            if ":" not in stripped:
                raise _MiniYamlError(
                    f"expected 'key: value' in list item continuation, got: {line!r}"
                )
            key, _, val = stripped.partition(":")
            val_stripped = val.strip()
            if val_stripped == "":
                raise _MiniYamlError(
                    f"nested mappings deeper than one level are not supported: {line!r}"
                )
            current[key.strip()] = _coerce_scalar(val_stripped)
    if current is not None:
        items.append(current)
    return items


def _parse_block_mapping(block_lines: List[str]) -> Dict[str, Any]:
    mapping: Dict[str, Any] = {}
    for line in block_lines:
        stripped = line.lstrip()
        if not stripped:
            continue
        if ":" not in stripped:
            raise _MiniYamlError(f"expected 'key: value', got: {stripped!r}")
        key, _, val = stripped.partition(":")
        val_stripped = val.strip()
        if val_stripped == "":
            raise _MiniYamlError(
                f"nested mappings deeper than one level are not supported: {stripped!r}"
            )
        if val_stripped.startswith("[") and val_stripped.endswith("]"):
            mapping[key.strip()] = _parse_inline_list(val_stripped)
        else:
            mapping[key.strip()] = _coerce_scalar(val_stripped)
    return mapping


_INLINE_LIST_SPLIT = re.compile(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)")


def _parse_inline_list(text: str) -> List[Any]:
    inner = text.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    if not inner.strip():
        return []
    parts = [p.strip() for p in _INLINE_LIST_SPLIT.split(inner)]
    return [_coerce_scalar(p) for p in parts if p != ""]


def _coerce_scalar(text: str) -> Any:
    s = text.strip()
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    lower = s.lower()
    if lower in ("true", "false"):
        return lower == "true"
    if lower in ("null", "~", ""):
        return "" if s == "" else None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


__all__ = [
    "SKILL_MANIFEST_SCHEMA_VERSION",
    "VALID_SKILL_TYPES",
    "VALID_SKILL_RUNTIMES",
    "VALID_SKILL_PERMISSIONS",
    "SkillManifest",
    "SkillManifestError",
    "parse_skill_manifest_text",
]
