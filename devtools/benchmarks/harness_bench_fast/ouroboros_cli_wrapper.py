#!/usr/bin/env python3
"""Generic Ouroboros CLI wrapper for harness-bench-fast-style runners."""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys


def _read_prompt(path: str) -> str:
    if path:
        return pathlib.Path(path).expanduser().read_text(encoding="utf-8")
    return sys.stdin.read()


def build_command(*, ouroboros_bin: str, prompt: str, memory_mode: str = "empty") -> list[str]:
    return [
        ouroboros_bin,
        "run",
        "--memory-mode",
        memory_mode,
        "--quiet",
        prompt,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("--ouroboros-bin", default=os.environ.get("OUROBOROS_BIN", "ouroboros"))
    parser.add_argument("--repo-dir", default=os.environ.get("OUROBOROS_REPO_DIR", ""))
    parser.add_argument("--memory-mode", default="empty", choices=["empty", "forked", "shared"])
    parser.add_argument("--timeout-sec", type=int, default=0)
    args = parser.parse_args(argv)

    prompt = _read_prompt(args.prompt_file).strip()
    if not prompt:
        print("empty prompt", file=sys.stderr)
        return 2
    cmd = build_command(ouroboros_bin=args.ouroboros_bin, prompt=prompt, memory_mode=args.memory_mode)
    completed = subprocess.run(
        cmd,
        cwd=args.repo_dir or None,
        text=True,
        timeout=int(args.timeout_sec or 0) or None,
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
