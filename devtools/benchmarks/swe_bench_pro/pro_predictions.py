#!/usr/bin/env python3
"""Capture SWE-bench Pro prediction patches from prepared task repositories."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.run_roots import ensure_file_output_outside_repo, ensure_outside_repo, run_root, safe_benchmark_id


CAPTURE = Path(__file__).resolve().parent / "capture_patch.sh"
REPO_ROOT = Path(__file__).resolve().parents[3]


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _capture_patch(repo_dir: Path, base_commit: str, out_path: Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["bash", str(CAPTURE), str(repo_dir), base_commit, str(out_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"capture_patch.sh failed for {repo_dir}: {proc.stderr or proc.stdout}")
    patch = out_path.read_text(encoding="utf-8", errors="replace")
    if not patch.strip():
        raise RuntimeError(f"capture_patch.sh produced an empty patch for {repo_dir}")
    return patch


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL rows with instance_id, repo_dir/workspace_root, base_commit")
    parser.add_argument("--output", required=True, help="prediction JSONL")
    parser.add_argument("--patch-dir", default="", help="optional directory for captured .diff files")
    parser.add_argument("--model-name", default="ouroboros-pro")
    args = parser.parse_args()

    output = ensure_file_output_outside_repo(Path(args.output), REPO_ROOT)
    patch_dir = Path(args.patch_dir).expanduser() if args.patch_dir else run_root("swe_bench_pro") / "patches"
    ensure_outside_repo(patch_dir, REPO_ROOT)
    predictions: list[dict[str, str]] = []
    for item in _rows(Path(args.input).expanduser()):
        instance_id = str(item.get("instance_id") or "").strip()
        safe_instance_id = safe_benchmark_id(instance_id)
        repo_dir = Path(str(item.get("repo_dir") or item.get("workspace_root") or "")).expanduser()
        base_commit = str(item.get("base_commit") or "").strip()
        if not instance_id or not repo_dir.is_dir() or not base_commit:
            raise RuntimeError("each row must include instance_id, repo_dir/workspace_root, and base_commit")
        patch = _capture_patch(repo_dir, base_commit, patch_dir / f"{safe_instance_id}.diff")
        predictions.append(
            {
                "instance_id": instance_id,
                "model_name_or_path": args.model_name,
                "model_patch": patch,
            }
        )
    output.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in predictions) + ("\n" if predictions else ""),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
