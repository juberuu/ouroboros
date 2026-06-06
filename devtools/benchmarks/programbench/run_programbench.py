#!/usr/bin/env python3
"""ProgramBench adapter entrypoint.

This script intentionally stops before reinventing ProgramBench orchestration.
It prepares task bodies/submissions for official cleanroom runs and delegates
evaluation to the official `programbench` CLI.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import write_json
from devtools.benchmarks.common.run_roots import ensure_outside_repo, run_root, safe_join_under
from devtools.benchmarks.programbench.programbench_adapter import (
    build_ouroboros_task_body,
    create_submission_tarball,
    preflight_cleanroom_container,
    run_official_eval,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", default=str(pathlib.Path(__file__).resolve().parents[3]))
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--instruction-file", required=True)
    parser.add_argument("--container-name", required=True)
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--run-id", default="")
    parser.add_argument("--protected-path", action="append", default=[], help="protected reference path inside cleanroom; repeatable")
    parser.add_argument("--eval", action="store_true", help="run official programbench eval/info after writing submission")
    args = parser.parse_args()

    out_root = ensure_outside_repo(run_root("programbench", args.run_id), pathlib.Path(args.repo_dir))
    instance_dir = safe_join_under(out_root, args.instance_id)
    preflight = preflight_cleanroom_container(args.container_name)
    protected_paths = args.protected_path or ["/workspace/executable", "executable"]
    body = build_ouroboros_task_body(
        instruction=pathlib.Path(args.instruction_file).read_text(encoding="utf-8"),
        workspace_host_path=pathlib.Path(args.workspace),
        container_name=args.container_name,
        protected_backend_paths=protected_paths,
    )
    body.setdefault("metadata", {})["cleanroom_preflight"] = preflight
    write_json(instance_dir / "ouroboros_task_body.json", body)
    create_submission_tarball(
        pathlib.Path(args.workspace),
        instance_dir / "submission.tar.gz",
        protected_paths=protected_paths,
    )
    if args.eval:
        run_official_eval(out_root)
    print(instance_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
