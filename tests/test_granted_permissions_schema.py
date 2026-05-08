import pathlib

from ouroboros.skill_loader import load_skill_grants, save_skill_grants


def test_grants_schema_persists_permission_grants(tmp_path: pathlib.Path) -> None:
    drive_root = tmp_path / "drive"
    drive_root.mkdir()

    save_skill_grants(
        drive_root,
        "skill",
        [],
        content_hash="hash",
        requested_keys=[],
        granted_permissions=["inject_chat"],
        requested_permissions=["inject_chat"],
    )

    grants = load_skill_grants(drive_root, "skill")
    assert grants["requested_permissions"] == ["inject_chat"]
    assert grants["granted_permissions"] == ["inject_chat"]
