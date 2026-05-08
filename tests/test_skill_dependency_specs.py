from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
from ouroboros.marketplace.install_specs import normalize_install_specs
from ouroboros.skill_dependencies import _manifest_install_specs


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
