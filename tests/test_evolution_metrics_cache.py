"""Regression tests for Evolution metrics caching."""
from __future__ import annotations

import asyncio
import json
import subprocess

from ouroboros.utils import collect_evolution_metrics


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_collect_evolution_metrics_reuses_cache_and_preserves_order(tmp_path):
    repo = tmp_path / "repo"
    data_dir = tmp_path / "data"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")

    (repo / "prompts").mkdir()
    (repo / "BIBLE.md").write_text("bible\n", encoding="utf-8")
    (repo / "prompts" / "SYSTEM.md").write_text("system\n", encoding="utf-8")
    (repo / "a.py").write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v1")
    _git(repo, "tag", "v1")

    (repo / "b.py").write_text("print('two')\nprint('three')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v2")
    _git(repo, "tag", "v2")

    tag_lines = _git(
        repo,
        "tag",
        "-l",
        "--sort=creatordate",
        "--format=%(refname:short)\t%(creatordate:iso-strict)",
    ).stdout.strip().splitlines()
    tag_dates = dict(line.split("\t", 1) for line in tag_lines)

    cache_path = data_dir / "state" / "evolution_metrics_cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps({
            "schema": 1,
            "points": {
                "v1": {
                    "tag": "v1",
                    "date": tag_dates["v1"],
                    "code_lines": 123,
                    "bible_kb": 1.0,
                    "system_kb": 2.0,
                    "identity_kb": 0.0,
                    "scratchpad_kb": 0.0,
                    "memory_kb": 0.0,
                }
            },
        }),
        encoding="utf-8",
    )
    memory_dir = data_dir / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "identity.md").write_text("identity-live", encoding="utf-8")
    (memory_dir / "scratchpad.md").write_text("scratchpad-live", encoding="utf-8")

    points = asyncio.run(collect_evolution_metrics(str(repo), data_dir=str(data_dir)))

    assert [point["tag"] for point in points] == ["v1", "v2"]
    assert points[0]["code_lines"] == 123
    assert points[1]["code_lines"] == 3
    assert points[1]["identity_kb"] > 0
    assert points[1]["scratchpad_kb"] > 0

    saved = json.loads(cache_path.read_text(encoding="utf-8"))
    assert saved["schema"] == 1
    assert "v1" in saved["points"]
    assert "v2" in saved["points"]


def test_collect_evolution_metrics_ignores_non_object_cache(tmp_path):
    repo = tmp_path / "repo"
    data_dir = tmp_path / "data"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "prompts").mkdir()
    (repo / "BIBLE.md").write_text("bible\n", encoding="utf-8")
    (repo / "prompts" / "SYSTEM.md").write_text("system\n", encoding="utf-8")
    (repo / "a.py").write_text("print('one')\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "v1")
    _git(repo, "tag", "v1")

    cache_path = data_dir / "state" / "evolution_metrics_cache.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("[]", encoding="utf-8")

    points = asyncio.run(collect_evolution_metrics(str(repo), data_dir=str(data_dir)))

    assert [point["tag"] for point in points] == ["v1"]
    assert points[0]["code_lines"] == 1
