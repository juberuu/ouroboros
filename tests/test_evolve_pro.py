"""Unit coverage for the SWE-bench Pro evolutionary driver's non-LLM helpers.

The full driver runs the model and is a manual harness; these tests exercise the
pure, deterministic parts (instance prep, patch capture, isolation) without any
provider calls so the loop's plumbing stays correct.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from devtools.benchmarks.swe_bench_pro import evolve_pro
from devtools.benchmarks.common.run_roots import ensure_outside_repo

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def _has_bash() -> bool:
    """True only if bash actually RUNS. On Windows `bash.exe` is often the WSL
    launcher stub that exits non-zero when no distro is installed; capture_patch.sh
    is a POSIX/Docker devtool, so those tests must skip there rather than fail."""
    if shutil.which("bash") is None:
        return False
    try:
        return subprocess.run(["bash", "-c", "exit 0"], capture_output=True, timeout=10).returncode == 0
    except OSError:
        return False


def test_make_demo_instances_are_valid_git_repos(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 2)
    assert len(rows) == 2
    for row in rows:
        assert row["instance_id"].startswith("demo-")
        assert row["problem_statement"]
        repo = Path(row["repo_dir"])
        assert (repo / ".git").is_dir()
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True)
        assert head.stdout.strip() == row["base_commit"]


def test_prepare_workspace_uses_prepared_repo_dir(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 1)
    repo, base = evolve_pro._prepare_workspace(rows[0], tmp_path)
    assert repo == Path(rows[0]["repo_dir"])
    assert base == rows[0]["base_commit"]


def test_prepare_workspace_rejects_non_git_dir(tmp_path: Path):
    bad = tmp_path / "plain"
    bad.mkdir()
    with pytest.raises(RuntimeError, match="not a git checkout"):
        evolve_pro._prepare_workspace({"instance_id": "x", "repo_dir": str(bad)}, tmp_path)


def test_prepare_workspace_requires_source(tmp_path: Path):
    with pytest.raises(RuntimeError, match="repo_dir.*or repo_url"):
        evolve_pro._prepare_workspace({"instance_id": "x"}, tmp_path)


@pytest.mark.skipif(not _has_bash(), reason="bash required for capture_patch.sh")
def test_capture_patch_captures_source_change(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 1)
    repo = Path(rows[0]["repo_dir"])
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b  # fixed\n", encoding="utf-8")
    patch = evolve_pro._capture_patch(repo, rows[0]["base_commit"], tmp_path / "out" / "p.diff")
    assert "return a + b" in patch


@pytest.mark.skipif(not _has_bash(), reason="bash required for capture_patch.sh")
def test_capture_patch_empty_raises(tmp_path: Path):
    rows = evolve_pro._make_demo_instances(tmp_path, 1)
    repo = Path(rows[0]["repo_dir"])
    with pytest.raises(RuntimeError, match="empty patch"):
        evolve_pro._capture_patch(repo, rows[0]["base_commit"], tmp_path / "out" / "p.diff")


def test_run_outputs_never_under_repo():
    # The driver's two run-root guards must reject repo-internal paths.
    with pytest.raises(ValueError):
        ensure_outside_repo(evolve_pro.REPO_DIR / "devtools" / "x", evolve_pro.REPO_DIR)


def test_seed_settings_enables_post_task_evolution(tmp_path: Path):
    import json

    settings_path = evolve_pro._seed_settings(tmp_path)
    cfg = json.loads(settings_path.read_text(encoding="utf-8"))
    assert cfg["OUROBOROS_POST_TASK_EVOLUTION"] is True
    assert cfg["OUROBOROS_RUNTIME_MODE"] == "advanced"


def _seed_clone(clone: Path) -> None:
    clone.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=clone, check=True)
    (clone / "f.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
                   cwd=clone, check=True)


def test_evolve_cycle_detects_commit(tmp_path: Path, monkeypatch):
    clone = tmp_path / "clone"
    _seed_clone(clone)

    def fake_run(args, env, timeout):
        (clone / "g.txt").write_text("b", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "evo"],
                       cwd=clone, check=True)
        return 0, "committed"

    monkeypatch.setattr(evolve_pro, "_run_ouroboros", fake_run)
    res = evolve_pro._evolve_cycle({"OUROBOROS_REPO_DIR": str(clone)}, 10)
    assert res["committed"] is True
    assert res["rc"] == 0


def test_evolve_cycle_no_commit_when_lesson_only(tmp_path: Path, monkeypatch):
    clone = tmp_path / "clone"
    _seed_clone(clone)
    monkeypatch.setattr(evolve_pro, "_run_ouroboros", lambda *a, **k: (0, "recorded lesson"))
    res = evolve_pro._evolve_cycle({"OUROBOROS_REPO_DIR": str(clone)}, 10)
    assert res["committed"] is False
