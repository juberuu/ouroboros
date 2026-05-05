from __future__ import annotations

import json
from types import SimpleNamespace

from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
from ouroboros.skill_dependencies import auto_install_specs_for_skill, normalize_declared_dependency_specs


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
