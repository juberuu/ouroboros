"""Unit tests for ouroboros.marketplace.adapter.

Covers field-mapping, refusal cases, and the persisted-artifact contract
(SKILL.md vs SKILL.openclaw.md) on a temporary staging directory.
"""

from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

from ouroboros.marketplace import adapter as adapter_mod
from ouroboros.marketplace.adapter import adapt_openclaw_skill, sanitize_clawhub_slug


def _write_staged_skill(staging_dir: pathlib.Path, frontmatter: str, body: str = "") -> None:
    """Drop a SKILL.md into ``staging_dir`` (helper)."""
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / "SKILL.md").write_text(
        f"---\n{frontmatter.strip()}\n---\n\n{body.strip()}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# sanitize_clawhub_slug
# ---------------------------------------------------------------------------


def test_sanitize_owner_slug_double_underscore():
    assert sanitize_clawhub_slug("steipete/slack") == "steipete__slack"


def test_sanitize_strips_unsafe_chars():
    assert sanitize_clawhub_slug("hello world!") == "hello_world"


def test_sanitize_empty_input_falls_back():
    assert sanitize_clawhub_slug("") == "_clawhub_skill"
    assert sanitize_clawhub_slug("   ") == "_clawhub_skill"


def test_sanitize_caps_at_64():
    long = "a/" + "b" * 200
    sanitized = sanitize_clawhub_slug(long)
    assert len(sanitized) <= 64


# ---------------------------------------------------------------------------
# adapt_openclaw_skill — happy path mapping
# ---------------------------------------------------------------------------


def test_adapter_translates_basic_metadata(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: nano-banana
            description: Generate or edit images via Gemini 3 Pro Image
            version: 1.0.0
            metadata:
              openclaw:
                requires:
                  bins: [python3]
                  env: [GEMINI_API_KEY]
                primaryEnv: GEMINI_API_KEY
                os: [darwin, linux]
            """
        ),
        body="# nano-banana\n\nUse this when generating images.\n",
    )
    (staging / "scripts").mkdir()
    (staging / "scripts" / "main.py").write_text("print('hi')\n", encoding="utf-8")

    result = adapt_openclaw_skill(
        staging,
        slug="anthropics/nano-banana",
        version="1.0.0",
        sha256="0" * 64,
        is_plugin=False,
    )
    assert result.ok, f"unexpected blockers: {result.blockers}"
    assert result.sanitized_name == "anthropics__nano-banana"
    assert result.manifest is not None
    assert result.manifest.type == "script"
    assert result.manifest.runtime == "python3"
    assert "GEMINI_API_KEY" in result.manifest.env_from_settings
    assert "subprocess" in result.manifest.permissions
    assert "net" in result.manifest.permissions  # env credential => net
    assert (staging / "SKILL.openclaw.md").is_file()
    assert (staging / "SKILL.md").is_file()
    sidecar = json.loads((staging / ".clawhub.json").read_text(encoding="utf-8"))
    assert sidecar["source"] == "clawhub"
    assert sidecar["slug"] == "anthropics/nano-banana"
    assert sidecar["adapter_version"] == adapter_mod.ADAPTER_VERSION
    assert sidecar["openclaw_compat"]["metadata_openclaw"]["primaryEnv"] == "GEMINI_API_KEY"


def test_adapter_description_mirrors_into_when_to_use(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: example
            description: Use when summarising research papers.
            version: 0.1.0
            """
        ),
    )
    result = adapt_openclaw_skill(
        staging, slug="example", version="0.1.0", sha256="x" * 64
    )
    assert result.ok
    assert result.manifest.when_to_use == "Use when summarising research papers."


def test_adapter_no_scripts_dir_yields_instruction(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: notes-only
            description: Markdown-only playbook.
            version: 0.0.1
            """
        ),
        body="# Notes\n\nJust prose.\n",
    )
    result = adapt_openclaw_skill(
        staging, slug="x/notes-only", version="0.0.1", sha256="z" * 64
    )
    assert result.ok
    assert result.manifest.type == "instruction"
    assert result.manifest.runtime == ""


def test_adapter_normalises_os_field(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: mac-only
            description: Mac thing.
            version: 0.1
            metadata:
              openclaw:
                os: [macos]
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/mac-only", version="0.1", sha256="y" * 64)
    assert result.ok
    assert result.manifest.os == "darwin"


def test_adapter_full_os_set_resolves_to_any(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: cross
            description: All platforms.
            version: 0.1
            metadata:
              openclaw:
                os: [darwin, linux, win32]
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/cross", version="0.1", sha256="0" * 64)
    assert result.manifest.os == "any"


def test_adapter_preserves_openclaw_command_and_gating_metadata(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: commandish
            description: Tool command.
            version: 0.1
            user-invocable: true
            command-dispatch: tool
            command-tool: web_search
            command-arg-mode: raw
            metadata:
              openclaw:
                always: true
                skillKey: commandish-key
                emoji: ":search:"
                homepage: https://example.com/commandish
                requires:
                  config: [browser.enabled]
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/commandish", version="0.1", sha256="0" * 64)
    assert result.ok
    compat = result.provenance["openclaw_compat"]
    assert compat["always"] is True
    assert compat["skill_key"] == "commandish-key"
    assert compat["requires"]["config"] == ["browser.enabled"]
    assert compat["command_fields"]["command-tool"] == "web_search"
    assert any("requires.config" in warning for warning in result.warnings)
    assert any("always=true" in warning for warning in result.warnings)


def test_adapter_scripts_listed_when_runtime_python(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: with-scripts
            description: Foo
            version: 0.1
            metadata:
              openclaw:
                requires:
                  bins: [python3]
            """
        ),
    )
    (staging / "scripts").mkdir()
    (staging / "scripts" / "fetch.py").write_text("print('hi')\n", encoding="utf-8")
    (staging / "scripts" / "helper.py").write_text("def x(): pass\n", encoding="utf-8")
    result = adapt_openclaw_skill(staging, slug="x/with-scripts", version="0.1", sha256="0" * 64)
    assert result.ok
    names = [s["name"] for s in result.manifest.scripts]
    assert "fetch.py" in names and "helper.py" in names


# ---------------------------------------------------------------------------
# Refusal cases
# ---------------------------------------------------------------------------


def test_adapter_refuses_install_specs(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: brew-skill
            description: needs brew
            version: 0.1
            metadata:
              openclaw:
                install:
                  - kind: brew
                    formula: jq
                    bins: [jq]
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/brew-skill", version="0.1", sha256="0" * 64)
    assert not result.ok
    assert any("install specs" in b for b in result.blockers)


@pytest.mark.parametrize("install_value", ["brew jq", "{kind: brew, formula: jq}"])
def test_adapter_refuses_malformed_non_empty_install_specs(tmp_path, install_value):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            f"""
            name: malformed-install
            description: bad install shape
            version: 0.1
            metadata:
              openclaw:
                install: {install_value}
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/malformed-install", version="0.1", sha256="0" * 64)
    assert not result.ok
    assert any("install specs" in b for b in result.blockers)


def test_adapter_converts_forbidden_settings_env_keys_to_grant_requests(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: leaky
            description: leak
            version: 0.1
            metadata:
              openclaw:
                requires:
                  env: [OPENROUTER_API_KEY, SOME_USER_KEY]
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/leaky", version="0.1", sha256="0" * 64)
    assert result.ok
    assert "OPENROUTER_API_KEY" in result.manifest.env_from_settings
    assert result.provenance["requested_key_grants"] == ["OPENROUTER_API_KEY"]
    assert any("explicit per-skill grants" in w for w in result.warnings)


def test_adapter_refuses_plugin_packages(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: node-plugin
            description: a node plugin
            version: 0.1
            """
        ),
    )
    (staging / "openclaw.plugin.json").write_text("{}", encoding="utf-8")
    result = adapt_openclaw_skill(
        staging, slug="x/node-plugin", version="0.1", sha256="0" * 64, is_plugin=True
    )
    assert not result.ok
    assert any("Node/TypeScript plugin" in b for b in result.blockers)


def test_adapter_warns_on_unsupported_bin(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: ruby-skill
            description: needs ruby
            version: 0.1
            metadata:
              openclaw:
                requires:
                  bins: [ruby]
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/ruby-skill", version="0.1", sha256="0" * 64)
    assert result.ok  # warning, not blocker
    assert any("outside the allowed runtime" in w for w in result.warnings)
    assert result.manifest.type == "instruction"


def test_adapter_provenance_records_sha_pair(tmp_path):
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: tracked
            description: foo
            version: 0.1
            """
        ),
    )
    result = adapt_openclaw_skill(staging, slug="x/tracked", version="0.1", sha256="0" * 64)
    assert result.ok
    prov = result.provenance
    assert prov["original_manifest_sha256"]
    assert prov["translated_manifest_sha256"]
    assert prov["original_manifest_sha256"] != prov["translated_manifest_sha256"]


def test_adapter_handles_missing_skill_md(tmp_path):
    """Defensive — fetcher should normally guard this, but adapter must not crash."""
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "README.md").write_text("# nothing\n", encoding="utf-8")
    result = adapt_openclaw_skill(staging, slug="x/empty", version="0.1", sha256="0" * 64)
    assert not result.ok
    assert any("Manifest unreadable" in b for b in result.blockers)


# ---------------------------------------------------------------------------
# Cycle 1 regression fixes
# ---------------------------------------------------------------------------


def test_adapter_handles_multiline_description(tmp_path):
    """v4.50 fix: a multi-line ``description`` (block scalar) must not break
    the adapter's self-validate re-parse step. ``_yaml_scalar`` now escapes
    \\n / \\r / \\t when emitting the translated SKILL.md.
    """
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "SKILL.md").write_text(
        textwrap.dedent(
            """
            ---
            name: multiline
            description: |
              Line one of the description
              Line two with extra context
              Line three
            version: 1.0.0
            ---

            # multiline

            body
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    result = adapt_openclaw_skill(
        staging, slug="x/multiline", version="1.0.0", sha256="0" * 64
    )
    assert result.ok, f"unexpected blockers: {result.blockers}"
    assert "Line one" in result.manifest.description
    assert "Line two" in result.manifest.description
    # Confirm the translated SKILL.md is well-formed by re-parsing it.
    rerendered = (staging / "SKILL.md").read_text(encoding="utf-8")
    from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
    parsed = parse_skill_manifest_text(rerendered)
    assert parsed.description == result.manifest.description


def test_adapter_normalises_lowercase_forbidden_settings_env_to_grant(tmp_path):
    """v4.50 fix: case-insensitive denylist comparison.

    A publisher who lowercases the env key (``openrouter_api_key``) used
    to slip past the FORBIDDEN_SKILL_SETTINGS check, which stores
    canonical UPPERCASE values. The adapter now normalises to UPPER at
    the boundary so the denylist holds under any case variation.
    """
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: lowercase-leak
            description: try to leak a credential.
            version: 0.1
            metadata:
              openclaw:
                requires:
                  env: [openrouter_api_key]
            """
        ),
    )
    result = adapt_openclaw_skill(
        staging, slug="x/lowercase-leak", version="0.1", sha256="0" * 64
    )
    assert result.ok
    assert result.manifest.env_from_settings == ["OPENROUTER_API_KEY"]
    assert result.provenance["requested_key_grants"] == ["OPENROUTER_API_KEY"]


def test_adapter_warns_on_unrecognised_allowed_tools_tokens(tmp_path):
    """v4.50 fix: unrecognised ``allowed-tools`` tokens emit a grouped warning.

    Previously the adapter silently dropped tokens like ``Edit``,
    ``Glob``, ``Grep`` — now they are surfaced so the reviewer can
    cross-check the original SKILL.openclaw.md.
    """
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: with-tools
            description: uses claude-code-style allowed-tools.
            version: 0.1
            allowed-tools: "Bash(ls:*) Edit Glob Grep TodoWrite"
            """
        ),
    )
    result = adapt_openclaw_skill(
        staging, slug="x/with-tools", version="0.1", sha256="0" * 64
    )
    assert result.ok
    matched_warning = next(
        (w for w in result.warnings if "allowed-tools" in w and "not mapped" in w),
        None,
    )
    assert matched_warning, f"expected unrecognised-tokens warning, got: {result.warnings}"
    for token in ("Edit", "Glob", "Grep", "TodoWrite"):
        assert token in matched_warning


def test_adapter_provenance_records_homepage_and_license(tmp_path):
    """v4.50: homepage / license / primary_env land in provenance, NOT raw_extra."""
    staging = tmp_path / "staging"
    _write_staged_skill(
        staging,
        textwrap.dedent(
            """
            name: with-meta
            description: x
            version: 0.1
            license: MIT
            homepage: https://example.com/skill
            metadata:
              openclaw:
                primaryEnv: GEMINI_API_KEY
            """
        ),
    )
    result = adapt_openclaw_skill(
        staging, slug="x/with-meta", version="0.1", sha256="0" * 64
    )
    assert result.ok
    assert result.provenance.get("homepage") == "https://example.com/skill"
    assert result.provenance.get("license") == "MIT"
    assert result.provenance.get("primary_env") == "GEMINI_API_KEY"
