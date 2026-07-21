"""v6.74.0 (D1): the light-mode shell guard resolves the cwd BEFORE judging repo targets.

`repo_target_mentioned` used to join the RAW cwd string onto repo_dir, so a
resource-root LABEL (``cwd="task_drive"``) resolved to ``<repo>/task_drive`` —
"inside the repo" by construction — and a legitimate task-drive write was
light-blocked with a message advising the very root that was used. The guard
now receives the RESOLVED work dir from the shared ``resolve_shell_cwd``
resolver; a resolution failure fails closed with the standard cwd block.
"""
from __future__ import annotations

import pathlib

import pytest

from ouroboros.tools.registry import ToolRegistry
from ouroboros.tools.shell_guards import light_shell_repo_mutation, repo_target_mentioned


def _registry(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    reg = ToolRegistry(repo_dir=repo, drive_root=tmp_path / "drive")
    reg._ctx.task_id = "t1"
    return reg


# ---- unit level: the guard judges the RESOLVED work dir -------------------


def test_resource_label_cwd_is_not_read_as_repo_relative(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    drive = tmp_path / "drive" / "task_drives" / "t1"
    drive.mkdir(parents=True)
    # Old behavior: cwd="task_drive" joined onto repo -> <repo>/task_drive ->
    # a relative write target read as inside the repo -> false block.
    assert light_shell_repo_mutation(
        "touch out.txt", repo_dir=repo, cwd="task_drive", work_dir=drive,
    ) is False
    # Legacy fallback (no resolved work_dir) reproduces the historical bug —
    # pinned so the fix is observable and the resolver hoist stays mandatory.
    assert light_shell_repo_mutation(
        "touch out.txt", repo_dir=repo, cwd="task_drive",
    ) is True


def test_external_path_cwd_write_is_allowed(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    assert light_shell_repo_mutation(
        "touch out.txt", repo_dir=repo, cwd=str(external), work_dir=external,
    ) is False
    assert repo_target_mentioned(
        ["touch", "out.txt"], repo_dir=repo, work_dir=external,
    ) is False


def test_genuine_repo_target_still_blocks(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    # Absolute repo path is blocked regardless of the resolved cwd.
    assert light_shell_repo_mutation(
        f"touch {repo}/x.py", repo_dir=repo, cwd=str(external), work_dir=external,
    ) is True
    # Relative target with the repo itself as the resolved work dir is blocked.
    assert light_shell_repo_mutation(
        "touch x.py", repo_dir=repo, work_dir=repo,
    ) is True


# ---- registry level: the resolver is hoisted above the light guard ---------


@pytest.mark.serial
def test_light_mode_task_drive_label_cwd_is_not_light_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    # Portable write (no `touch` on Windows shells — the v6.73.1 class):
    # a python inline write in the resolved task-drive cwd.
    result = reg.execute("run_command", {
        "cmd": ["python", "-c", "import pathlib; pathlib.Path('out.txt').write_text('x')"],
        "cwd": "task_drive",
    })
    assert "LIGHT_MODE_BLOCKED" not in result, result[:300]
    task_drive = pathlib.Path(reg._ctx.task_drive_root())
    assert (task_drive / "out.txt").exists()


def test_light_mode_repo_write_still_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_command", {"cmd": "touch marker.py"})
    assert "LIGHT_MODE_BLOCKED" in result, result[:300]
    assert not (pathlib.Path(reg._ctx.repo_dir) / "marker.py").exists()


def test_light_mode_cwd_resolution_failure_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("OUROBOROS_RUNTIME_MODE", "light")
    reg = _registry(tmp_path)
    result = reg.execute("run_command", {"cmd": "touch out.txt", "cwd": "/etc"})
    assert "SHELL_CWD_BLOCKED" in result, result[:300]
