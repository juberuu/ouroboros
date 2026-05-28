"""Tests for skill dependency specs, manifest auto-install, secret keys, and SkillToken.

Merged in v5.15.x from four single-purpose micro-files:

- ``test_skill_dependencies.py``         — manifest/sidecar → auto_install_specs_for_skill
- ``test_skill_dependency_specs.py``     — normalize_install_specs (pip extras/ranges) + _manifest_install_specs
- ``test_skill_requested_secret_keys.py``— requested_core_setting_keys allowlist
- ``test_skill_token.py``                — SkillToken redaction + serialization/copy block

All four exercise narrow corners of the skill plumbing; merged here so the
boilerplate (imports, file headers) is paid once.
"""
from __future__ import annotations

import copy
import json
import pickle
from types import SimpleNamespace

import pytest

from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
from ouroboros.marketplace.install_specs import normalize_install_specs
from ouroboros.skill_dependencies import (
    _manifest_install_specs,
    auto_install_specs_for_skill,
    normalize_declared_dependency_specs,
)
from ouroboros.skill_loader import requested_core_setting_keys
from ouroboros.skill_token import SkillToken


# ---------------------------------------------------------------------------
# auto_install_specs_for_skill + normalize_declared_dependency_specs
# ---------------------------------------------------------------------------


def test_bare_dependency_list_defaults_to_python_packages():
    auto, manual, warnings = normalize_declared_dependency_specs(["ddgs"])

    assert manual == []
    assert warnings == []
    assert auto == [{"kind": "pip", "package": "ddgs", "bins": [], "mode": "auto", "raw": {"kind": "pip", "package": "ddgs"}}]


def test_manifest_dependencies_are_skill_dependency_source(tmp_path):
    skill_dir = tmp_path / "skills" / "external" / "duckduckgo"
    skill_dir.mkdir(parents=True)
    manifest = parse_skill_manifest_text(
        "---\n"
        "name: duckduckgo\n"
        "type: extension\n"
        "entry: plugin.py\n"
        "dependencies: [ddgs]\n"
        "---\n"
    )
    loaded = SimpleNamespace(name="duckduckgo", skill_dir=skill_dir, manifest=manifest)

    specs = auto_install_specs_for_skill(tmp_path, loaded)

    assert specs[0]["kind"] == "pip"
    assert specs[0]["package"] == "ddgs"


def test_payload_sidecar_dependencies_override_manifest(tmp_path):
    skill_dir = tmp_path / "skills" / "ouroboroshub" / "duckduckgo"
    skill_dir.mkdir(parents=True)
    (skill_dir / ".ouroboroshub.json").write_text(
        json.dumps(
            {
                "install_specs": {
                    "auto": [{"kind": "pip", "package": "ddgs", "bins": [], "mode": "auto"}]
                }
            }
        ),
        encoding="utf-8",
    )
    manifest = parse_skill_manifest_text("---\nname: duckduckgo\ntype: extension\nentry: plugin.py\n---\n")
    loaded = SimpleNamespace(name="duckduckgo", skill_dir=skill_dir, manifest=manifest)

    specs = auto_install_specs_for_skill(tmp_path, loaded)

    assert specs == [{"kind": "pip", "package": "ddgs", "bins": [], "mode": "auto"}]


# ---------------------------------------------------------------------------
# normalize_install_specs (pip extras/version ranges) + manifest installspecs
# ---------------------------------------------------------------------------


def test_pip_specs_allow_extras_and_version_ranges() -> None:
    auto, manual, warnings = normalize_install_specs([
        {"kind": "pip", "package": "a2a-sdk[http-server]>=1.0.0,<2.0.0"},
        {"kind": "pip", "package": "protobuf<6"},
    ])

    assert not warnings
    assert not manual
    assert [item["package"] for item in auto] == [
        "a2a-sdk[http-server]>=1.0.0,<2.0.0",
        "protobuf<6",
    ]


def test_manifest_install_specs_are_auto_installable() -> None:
    manifest = parse_skill_manifest_text(
        """---
name: a2a
description: A2A
version: 1.0.0
type: extension
entry: plugin.py
install_specs:
  - kind: pip
    package: "protobuf<6"
---
# A2A
"""
    )

    assert _manifest_install_specs(manifest)[0]["package"] == "protobuf<6"


# ---------------------------------------------------------------------------
# requested_core_setting_keys allowlist
# ---------------------------------------------------------------------------


def test_known_transport_secret_is_grantable_when_absent():
    assert requested_core_setting_keys(["TELEGRAM_BOT_TOKEN", "SLACK_WEBHOOK_URL"]) == [
        "TELEGRAM_BOT_TOKEN",
    ]


def test_ouroboros_internal_settings_are_not_skill_grantable():
    assert "OUROBOROS_RUNTIME_MODE" not in requested_core_setting_keys(["OUROBOROS_RUNTIME_MODE"])


# ---------------------------------------------------------------------------
# SkillToken redaction + serialization/copy block
# ---------------------------------------------------------------------------


def test_skill_token_redacts_string_forms() -> None:
    token = SkillToken("secret-token")

    assert "secret-token" not in repr(token)
    assert "secret-token" not in str(token)
    assert "secret-token" not in f"{token}"
    assert token.use_in_request() == "secret-token"


def test_skill_token_blocks_serialization_and_copy() -> None:
    token = SkillToken("secret-token")

    with pytest.raises(TypeError):
        pickle.dumps(token)
    with pytest.raises(Exception):
        copy.copy(token)
    with pytest.raises(Exception):
        copy.deepcopy(token)
