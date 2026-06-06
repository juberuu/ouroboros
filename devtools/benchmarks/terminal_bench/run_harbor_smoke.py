#!/usr/bin/env python3
"""Build a Harbor command for a one-task Terminal-Bench smoke run."""

from __future__ import annotations

import argparse
import json
import pathlib
import shlex
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.run_roots import ensure_outside_repo, repo_root_from_devtools, run_root as default_run_root


AGENT_IMPORT = "devtools.benchmarks.terminal_bench.harbor_installed_agent:OuroborosTerminalBenchAgent"


def harbor_command(
    *,
    task_name: str,
    model: str,
    run_root: pathlib.Path,
    dataset: str = "terminal-bench/terminal-bench-2-1",
    harbor_bin: str = "harbor",
    execute: bool = False,
) -> list[str]:
    cmd = [
        harbor_bin,
        "run",
        "--dataset",
        dataset,
        "--include-task-name",
        task_name,
        "--agent-import-path",
        AGENT_IMPORT,
        "--model",
        f"ouroboros-{model.replace('/', '-')}",
        "--agent-kwarg",
        f"ouroboros_model={model}",
        "--agent-kwarg",
        "install_timeout_sec=1200",
        "--agent-kwarg",
        "server_start_timeout_sec=240",
        "--agent-setup-timeout-multiplier",
        "4",
        "--environment-build-timeout-multiplier",
        "4",
        "--n-concurrent",
        "1",
        "--n-tasks",
        "1",
        "--output-dir",
        str(run_root),
        "--yes",
    ]
    if execute:
        cmd.append("--force-build")
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="terminal-bench/regex-log")
    parser.add_argument("--model", default="openai/gpt-5.5")
    parser.add_argument("--dataset", default="terminal-bench/terminal-bench-2-1")
    parser.add_argument("--harbor-bin", default="harbor")
    parser.add_argument("--run-root", default="")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    run_root = ensure_outside_repo(
        pathlib.Path(args.run_root).expanduser() if args.run_root else default_run_root("terminal_bench"),
        repo_root_from_devtools(),
    )
    cmd = harbor_command(
        task_name=args.task,
        model=args.model,
        run_root=run_root,
        dataset=args.dataset,
        harbor_bin=args.harbor_bin,
        execute=args.execute,
    )
    manifest = {"run_root": str(run_root), "cmd": cmd, "agent_import": AGENT_IMPORT}
    run_root.mkdir(parents=True, exist_ok=True)
    (run_root / "harbor_command.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(shlex.join(cmd))
    if not args.execute:
        return 0
    return subprocess.run(cmd, cwd=pathlib.Path(__file__).resolve().parents[3]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
