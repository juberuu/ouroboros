from __future__ import annotations

from pathlib import Path

from ouroboros.tools.review_context_atlas import (
    ReviewContextAtlasRequest,
    compile_review_context_atlas,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _coverage(pack):
    return {row["path"]: row for row in pack.manifest["coverage"]}


def test_atlas_accounts_for_every_tracked_path_and_excludes_unrelated_tests(tmp_path):
    _write(tmp_path / "app.py", "import helper\n\ndef run():\n    return helper.value()\n")
    _write(tmp_path / "helper.py", "def value():\n    return 42\n")
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "main.py", "from .helper import thing\n\nanswer = thing()\n")
    _write(tmp_path / "pkg" / "helper.py", "def thing():\n    return 7\n")
    _write(tmp_path / "tests" / "test_app.py", "def test_app():\n    assert True\n")
    _write(tmp_path / "docs" / "CHECKLISTS.md", "canonical checklist\n")

    tracked = (
        "app.py",
        "helper.py",
        "pkg/__init__.py",
        "pkg/main.py",
        "pkg/helper.py",
        "tests/test_app.py",
        "docs/CHECKLISTS.md",
    )
    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=tracked,
            anchors=("app.py",),
            already_included=frozenset({"docs/CHECKLISTS.md"}),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
            include_tests=False,
        )
    )

    coverage = _coverage(pack)
    assert set(coverage) == set(tracked)
    assert coverage["docs/CHECKLISTS.md"]["disposition"] == "already_included"
    assert coverage["tests/test_app.py"]["disposition"] == "excluded_test"
    assert "pkg.helper" in coverage["pkg/main.py"]["imports"]
    assert "def test_app" not in pack.text


def test_atlas_include_tests_allows_test_files(tmp_path):
    _write(tmp_path / "tests" / "test_app.py", "def test_app():\n    assert True\n")

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("tests/test_app.py",),
            anchors=("tests/test_app.py",),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
            include_tests=True,
        )
    )

    coverage = _coverage(pack)
    assert coverage["tests/test_app.py"]["disposition"] == "full"
    assert "def test_app" in pack.text


def test_atlas_compact_manifest_keeps_full_coverage_out_of_prompt(tmp_path):
    _write(tmp_path / "app.py", "import helper\n\nprint(helper.VALUE)\n")
    _write(tmp_path / "helper.py", "VALUE = 42\n")
    _write(tmp_path / "other.py", "def unused():\n    return 'ok'\n")

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("app.py", "helper.py", "other.py"),
            anchors=("app.py",),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
            compact_manifest=True,
        )
    )

    assert pack.manifest["compact_manifest_in_prompt"] is True
    assert {row["path"] for row in pack.manifest["coverage"]} == {
        "app.py",
        "helper.py",
        "other.py",
    }
    assert '"coverage": [' not in pack.text
    assert '"coverage_in_prompt": "compact_full_index_plus_bounded_samples"' in pack.text
    assert '"coverage_samples"' in pack.text
    assert '"coverage_sample_counts"' in pack.text
    assert '"coverage_index_count": 3' in pack.text
    assert "### Compact full coverage index" in pack.text
    for rel_path in ("app.py", "helper.py", "other.py"):
        assert f"\t{rel_path}" in pack.text
    assert "compact coverage mode" in pack.text


def test_atlas_force_includes_protected_workflow_even_under_skipped_github_dir(tmp_path):
    _write(tmp_path / ".github" / "workflows" / "ci.yml", "name: CI\n")
    _write(tmp_path / "ouroboros" / "tools" / "review_context_atlas.py", "ATLAS = True\n")
    _write(tmp_path / "assets" / "logo.txt", "asset text\n")
    _write(tmp_path / "main.py", "print('main')\n")

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=(
                ".github/workflows/ci.yml",
                "ouroboros/tools/review_context_atlas.py",
                "assets/logo.txt",
                "main.py",
            ),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
        )
    )

    coverage = _coverage(pack)
    assert coverage[".github/workflows/ci.yml"]["disposition"] == "full"
    assert coverage["ouroboros/tools/review_context_atlas.py"]["disposition"] == "full"
    assert "name: CI" in pack.text
    assert coverage["assets/logo.txt"]["disposition"] == "excluded_dir"
    assert "asset text" not in pack.text


def test_atlas_devtools_manifest_only_unless_touched(tmp_path):
    _write(tmp_path / "devtools" / "benchmarks" / "programbench" / "run.py", "VALUE = 'devtools full text'\n")
    _write(tmp_path / "ouroboros" / "core.py", "print('core')\n")

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("devtools/benchmarks/programbench/run.py", "ouroboros/core.py"),
            anchors=("ouroboros/core.py",),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
        )
    )

    coverage = _coverage(pack)
    assert coverage["devtools/benchmarks/programbench/run.py"]["disposition"] == "excluded_dir"
    assert "devtools full text" not in pack.text
    assert coverage["ouroboros/core.py"]["disposition"] == "full"

    touched = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("devtools/benchmarks/programbench/run.py",),
            anchors=("devtools/benchmarks/programbench/run.py",),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
        )
    )

    touched_coverage = _coverage(touched)
    assert touched_coverage["devtools/benchmarks/programbench/run.py"]["disposition"] == "full"
    assert "devtools full text" in touched.text


def test_atlas_marks_sensitive_binary_oversized_and_vendored_files(tmp_path):
    _write(tmp_path / ".env.example", "TOKEN=secret\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x00")
    _write(tmp_path / "script.min.js", "minified();\n")
    (tmp_path / "huge.py").write_bytes(b"x" * (1_048_576 + 1))
    normal_source = "\n".join(f"import pkg_{idx}" for idx in range(30))
    normal_source += '\nDATABASE_URL = "postgres://alice:secretpw@db.local/app"\n'
    normal_source += "\n".join(f"def f_{idx}():\n    return {idx}\n" for idx in range(20))
    _write(tmp_path / "normal.py", normal_source)

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=(".env.example", "image.png", "script.min.js", "huge.py", "normal.py"),
            fixed_prompt_tokens=100,
            target_total_tokens=20_000,
            hard_total_tokens=25_000,
        )
    )

    coverage = _coverage(pack)
    assert coverage[".env.example"]["disposition"] == "sensitive"
    assert coverage[".env.example"]["sha256"] == ""
    assert coverage[".env.example"]["size"] == 0
    assert coverage["image.png"]["disposition"] == "binary_media"
    assert coverage["script.min.js"]["disposition"] == "vendored_minified"
    assert coverage["huge.py"]["disposition"] == "oversized"
    assert coverage["normal.py"]["disposition"] == "full"
    assert coverage["normal.py"]["imports_total"] == 30
    assert coverage["normal.py"]["symbols_total"] >= 20
    assert len(coverage["normal.py"]["imports"]) <= 12
    assert "secretpw" not in pack.text
    assert "postgres://***REDACTED***@db.local/app" in pack.text


def test_atlas_respects_total_prompt_target_and_reports_budget_manifest_only(tmp_path):
    tracked = []
    for idx in range(8):
        rel = f"pkg/mod_{idx}.py"
        tracked.append(rel)
        _write(tmp_path / rel, ("def f():\n    return 'x'\n" * 120))

    pack = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=tuple(tracked),
            fixed_prompt_tokens=100,
            target_total_tokens=5_000,
            hard_total_tokens=8_000,
        )
    )

    assert pack.manifest["estimated_total_tokens"] <= 8_000
    assert pack.manifest["selected_count"] < len(tracked)
    assert any(row["disposition"] == "manifest_only" for row in pack.manifest["coverage"])
    assert pack.status in {"budget_constrained", "ok"}

    _write(tmp_path / "BIBLE.md", "constitution\n" * 500)
    overflow = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=("BIBLE.md",),
            fixed_prompt_tokens=100,
            target_total_tokens=300,
            hard_total_tokens=350,
        )
    )
    assert overflow.status == "budget_exceeded"
    assert _coverage(overflow)["BIBLE.md"]["disposition"] == "budget_omitted"


def test_atlas_centrality_scores_default_off_is_identical(tmp_path):
    """Empty centrality_scores (the scope/plan path) must produce byte-identical
    selection to the heuristic baseline — D2 is strictly additive."""
    tracked = [f"mod_{i}.py" for i in range(6)]
    for rel in tracked:
        _write(tmp_path / rel, ("def f():\n    return 'x'\n" * 60))

    def _compile(**extra):
        return compile_review_context_atlas(
            ReviewContextAtlasRequest(
                repo_dir=tmp_path,
                tracked_paths=tuple(tracked),
                fixed_prompt_tokens=100,
                target_total_tokens=4_000,
                hard_total_tokens=6_000,
                **extra,
            )
        )

    baseline = _compile()
    explicit_empty = _compile(centrality_scores={})
    assert [r.rel_path for r in baseline.selected] == [r.rel_path for r in explicit_empty.selected]
    assert baseline.text == explicit_empty.text


def test_atlas_centrality_scores_boost_selection_order(tmp_path):
    """A centrality bonus must pull a hub module into the bounded selection
    ahead of equal-sized peers, without touching required/anchor tiers."""
    tracked = [f"mod_{i}.py" for i in range(6)]
    for rel in tracked:
        _write(tmp_path / rel, ("def f():\n    return 'x'\n" * 60))

    # Target budget fits only ~3 of 6 equal-sized files (selection pressure);
    # hard budget leaves real headroom past the atlas's 5K hard reserve.
    boosted = compile_review_context_atlas(
        ReviewContextAtlasRequest(
            repo_dir=tmp_path,
            tracked_paths=tuple(tracked),
            fixed_prompt_tokens=100,
            target_total_tokens=2_300,
            hard_total_tokens=7_000,
            centrality_scores={"mod_5.py": 600.0},
        )
    )

    selected = [r.rel_path for r in boosted.selected]
    assert selected, "tight budget must still select at least one file"
    assert selected[0] == "mod_5.py", "the centrality-boosted hub must be picked first"
    assert "mod_2.py" not in selected, "the boost must displace an unboosted peer"
    cov = _coverage(boosted)
    assert "graph_centrality" in cov["mod_5.py"]["reason"]
