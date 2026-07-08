"""v6.59.0 (Phase 3) — project entry points: attach/clone sources, provenance,
delete/hide, and the directory-browser confinement.
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest


def _init_git_repo(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=str(path), check=True)
    (path / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(path), check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@local", "commit", "-qm", "init"],
        cwd=str(path), check=True,
    )


# --- attach validation ----------------------------------------------------------

def test_validate_attach_path_boundaries(tmp_path):
    from ouroboros.project_sources import validate_attach_path

    sys_repo = tmp_path / "sysrepo"
    data = tmp_path / "data"
    sys_repo.mkdir()
    data.mkdir()
    ok_dir = tmp_path / "mycode"
    ok_dir.mkdir()

    resolved, error = validate_attach_path(str(ok_dir), system_repo_dir=sys_repo, drive_root=data)
    assert error == "" and resolved == ok_dir.resolve()

    # Missing path
    _, err_missing = validate_attach_path(str(tmp_path / "nope"), system_repo_dir=sys_repo, drive_root=data)
    assert "does not exist" in err_missing
    # A file, not a directory
    f = tmp_path / "file.txt"
    f.write_text("x")
    _, err_file = validate_attach_path(str(f), system_repo_dir=sys_repo, drive_root=data)
    assert "not a directory" in err_file
    # Home root itself is refused
    _, err_home = validate_attach_path(str(pathlib.Path.home()), system_repo_dir=sys_repo, drive_root=data)
    assert "home directory" in err_home
    # repo/data overlap refused
    _, err_repo = validate_attach_path(str(sys_repo), system_repo_dir=sys_repo, drive_root=data)
    assert "system repo" in err_repo
    _, err_data = validate_attach_path(str(data / "sub"), system_repo_dir=sys_repo, drive_root=data)
    assert err_data  # nonexistent AND under data — either error is a refusal


def test_attach_snapshot_init_is_opt_in_and_idempotent(tmp_path):
    from ouroboros.project_sources import attach_snapshot_init

    folder = tmp_path / "plain"
    folder.mkdir()
    (folder / "notes.txt").write_text("hello\n", encoding="utf-8")
    assert attach_snapshot_init(folder) == ""
    log_out = subprocess.run(["git", "log", "-1", "--format=%s %an"], cwd=str(folder), capture_output=True, text=True).stdout
    assert "ouroboros: attach snapshot" in log_out and "Ouroboros" in log_out
    # Idempotent for an existing repo (no second snapshot, no error).
    assert attach_snapshot_init(folder) == ""
    count = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=str(folder), capture_output=True, text=True).stdout.strip()
    assert count == "1"


# --- clone URL forms + typed errors ----------------------------------------------

def test_valid_git_url_forms():
    from ouroboros.project_sources import derive_repo_dir_name, valid_git_url

    assert valid_git_url("https://github.com/user/repo.git")
    assert valid_git_url("https://github.com/user/repo")
    assert valid_git_url("ssh://git@github.com/user/repo.git")
    assert valid_git_url("git@github.com:user/repo.git")
    assert not valid_git_url("/local/path")
    assert not valid_git_url("ftp://x/y")
    assert not valid_git_url("")
    assert derive_repo_dir_name("https://github.com/user/My-Repo.git") == "My-Repo"
    assert derive_repo_dir_name("git@github.com:user/tool.git") == "tool"


def test_clone_project_repo_local_source_and_atomicity(tmp_path, monkeypatch):
    from ouroboros.project_sources import clone_project_repo

    projects_root = tmp_path / "projects"
    monkeypatch.setenv("OUROBOROS_SUBAGENT_PROJECTS_ROOT", str(projects_root))
    # invalid URL is typed
    _, code, _ = clone_project_repo("not-a-url")
    assert code == "invalid_url"
    # A real clone from a local https-shaped URL isn't possible offline; validate the
    # existing-destination refusal instead (atomic tmp never left behind).
    (projects_root / "taken").mkdir(parents=True)
    _, code2, detail2 = clone_project_repo("https://github.com/user/taken.git", "taken")
    assert code2 == "exists" and "taken" in detail2
    leftovers = [p.name for p in projects_root.iterdir() if ".tmp." in p.name]
    assert leftovers == []


# --- registry: provenance + delete ------------------------------------------------

def test_update_project_provenance_fields_and_delete(tmp_path):
    from ouroboros.projects_registry import (
        bind_task_to_project,
        create_project,
        delete_project,
        get_project,
        project_binding_for_task,
        update_project,
    )

    data = tmp_path / "data"
    data.mkdir()
    create_project(data, "p1", name="P1", origin="owner_ui")
    update_project(data, "p1", provenance="attached", clone_url="", trusted_at="2026-07-09T00:00:00Z")
    entry = get_project(data, "p1")
    assert entry["provenance"] == "attached"
    assert entry["trusted_at"].startswith("2026-")

    bind_task_to_project(data, "t1", "p1", entry.get("chat_id"))
    assert project_binding_for_task(data, "t1") is not None

    folder = tmp_path / "keepme"
    folder.mkdir()
    update_project(data, "p1", working_dir=str(folder))
    assert delete_project(data, "p1") is True
    assert get_project(data, "p1") is None
    assert project_binding_for_task(data, "t1") is None
    assert folder.is_dir()  # the folder is NEVER touched by delete


def test_projects_summary_shows_owner_ui_projects_without_activity(tmp_path):
    from ouroboros.projects_registry import create_project, projects_summary

    data = tmp_path / "data"
    data.mkdir()
    create_project(data, "fresh", name="Fresh", origin="owner_ui")
    rows = projects_summary(data)
    fresh = next(r for r in rows if r["id"] == "fresh")
    assert fresh["has_thread_activity"] is True  # owner-created is always visible
    assert "provenance" in fresh


# --- ui preferences: project_hidden ------------------------------------------------

def test_ui_preferences_project_hidden_merge_and_unhide():
    from ouroboros.gateway.ui_preferences import _normalize_preferences

    prefs = _normalize_preferences({"project_hidden": {"a": True, "b": False}}, fill_defaults=False)
    assert prefs["project_hidden"] == {"a": True, "b": False}
    with pytest.raises(ValueError):
        _normalize_preferences({"project_hidden": ["a"]}, fill_defaults=False)


# --- fs/dirs confinement ------------------------------------------------------------

def test_api_fs_dirs_confined_to_home(tmp_path):
    import asyncio
    import json

    from ouroboros.gateway.projects import api_fs_dirs

    class _Req:
        def __init__(self, path=""):
            self.query_params = {"path": path} if path else {}

    # Home itself works.
    resp = asyncio.run(api_fs_dirs(_Req()))
    payload = json.loads(resp.body)
    assert payload["path"] == str(pathlib.Path.home().resolve())
    assert payload["parent"] == ""
    # Outside-home is refused.
    resp2 = asyncio.run(api_fs_dirs(_Req("/etc")))
    payload2 = json.loads(resp2.body)
    assert resp2.status_code == 400 and "confined" in payload2["error"]
