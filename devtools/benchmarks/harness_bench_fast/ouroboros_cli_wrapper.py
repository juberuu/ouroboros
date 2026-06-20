#!/usr/bin/env python3
"""Thin CLI adapter used by harness-bench-fast ``run-cli``.

``harness_bench run-cli`` invokes the configured command with:

    cwd=<fresh task workspace>
    argv=[...configured command..., <official task prompt>]

This wrapper preserves that contract and delegates to ``ouroboros run`` with the
current working directory as the external workspace root. The wrapper writes
per-task logs outside the transient benchmark workspace so they survive cleanup.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from typing import Any


DEFAULT_REPO = pathlib.Path(__file__).resolve().parents[3]
DEFAULT_DATA = DEFAULT_REPO.parent / "data"
DEFAULT_SETTINGS = DEFAULT_DATA / "settings.json"
DEFAULT_OUROBOROS_BIN = DEFAULT_REPO.parent / ".venv" / "bin" / "ouroboros"


def _safe_slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip("-._")
    return text[:96] or "task"


def _task_id_from_workspace(workspace: pathlib.Path) -> str:
    name = workspace.name
    if name.startswith("hb_cli_"):
        stem = name.removeprefix("hb_cli_")
        parts = stem.split("_")
        if len(parts) > 1:
            return "_".join(parts[:-1]) or stem
    if name.startswith("hb_cli_prom_"):
        stem = name.removeprefix("hb_cli_prom_")
        parts = stem.split("_")
        if len(parts) > 1:
            return "_".join(parts[:-1]) or stem
    return name


def _write_json(path: pathlib.Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _ensure_workspace_git_root(workspace: pathlib.Path) -> dict[str, Any]:
    """Make harness-bench's temp workspace acceptable as Ouroboros external workspace."""

    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    if probe.returncode == 0 and pathlib.Path(probe.stdout.strip()).resolve(strict=False) == workspace:
        return {"created": False, "status": "already_git_root"}
    if probe.returncode == 0:
        return {"created": False, "status": "nested_git_root", "root": probe.stdout.strip()}

    init = subprocess.run(["git", "init"], cwd=workspace, text=True, capture_output=True)
    config_email = subprocess.run(
        ["git", "config", "user.email", "ouroboros-harness-bench@example.invalid"],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    config_name = subprocess.run(
        ["git", "config", "user.name", "Ouroboros Harness Bench"],
        cwd=workspace,
        text=True,
        capture_output=True,
    )
    return {
        "created": init.returncode == 0,
        "status": "initialized" if init.returncode == 0 else "init_failed",
        "init_returncode": init.returncode,
        "init_stderr": init.stderr[-500:],
        "config_email_returncode": config_email.returncode,
        "config_name_returncode": config_name.returncode,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-dir", default=str(DEFAULT_REPO))
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA))
    parser.add_argument("--settings-path", default=str(DEFAULT_SETTINGS))
    parser.add_argument("--ouroboros-bin", default=os.environ.get("OUROBOROS_BIN") or str(DEFAULT_OUROBOROS_BIN))
    parser.add_argument("--model", default="")
    parser.add_argument("--log-root", default="")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--memory-mode", default="empty", choices=["empty", "forked", "shared"])
    parser.add_argument("--no-start-server", action="store_true")
    parser.add_argument("--prompt-file", default="")
    parser.add_argument("prompt", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    prompt_parts = list(args.prompt or [])
    if prompt_parts and prompt_parts[0] == "--":
        prompt_parts = prompt_parts[1:]
    prompt = " ".join(prompt_parts).strip()
    if args.prompt_file:
        prompt = pathlib.Path(args.prompt_file).read_text(encoding="utf-8")
    if not prompt:
        print("harness-bench-fast wrapper: missing prompt", file=sys.stderr)
        return 2

    workspace = pathlib.Path.cwd().resolve(strict=False)
    git_workspace = _ensure_workspace_git_root(workspace)
    repo_dir = pathlib.Path(args.repo_dir).expanduser().resolve(strict=False)
    data_dir = pathlib.Path(args.data_dir).expanduser().resolve(strict=False)
    settings_path = pathlib.Path(args.settings_path).expanduser().resolve(strict=False)
    task_id = _task_id_from_workspace(workspace)
    log_root = pathlib.Path(args.log_root).expanduser().resolve(strict=False) if args.log_root else data_dir / "bench_logs" / "harness_bench_fast"
    run_id = f"{_safe_slug(task_id)}_{int(time.time())}_{os.getpid()}"
    log_dir = log_root / run_id
    log_dir.mkdir(parents=True, exist_ok=True)

    (log_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    summary_path = log_dir / "summary.json"
    result_json = log_dir / "ouroboros-task-result.json"
    started = time.time()

    env = os.environ.copy()
    env.update(
        {
            "OUROBOROS_REPO_DIR": str(repo_dir),
            "OUROBOROS_DATA_DIR": str(data_dir),
            "OUROBOROS_SETTINGS_PATH": str(settings_path),
            "OUROBOROS_RUNTIME_MODE": "pro",
            "OUROBOROS_REVIEW_ENFORCEMENT": "advisory",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if args.model:
        env.update(
            {
                "OUROBOROS_MODEL": args.model,
                "OUROBOROS_MODEL_HEAVY": args.model,
                "OUROBOROS_MODEL_LIGHT": args.model,
                "OUROBOROS_MODEL_FALLBACKS": args.model,
            }
        )

    cmd = [
        str(pathlib.Path(args.ouroboros_bin).expanduser()),
        "run",
        "--workspace",
        str(workspace),
        "--memory-mode",
        args.memory_mode,
        "--quiet",
        "--timeout",
        str(max(0, int(args.timeout))),
        "--result-json-out",
        str(result_json),
        "--actor-id",
        "harness-bench-fast",
    ]
    if not args.no_start_server:
        cmd.append("--start")
    cmd.append(prompt)

    def _run_once() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=str(repo_dir),
            env=env,
            text=True,
            capture_output=True,
            timeout=max(1, int(args.timeout)) + 90,
        )

    # The shared server can return HTTP 503 "supervisor is still starting" on a
    # cold start (or mid-run supervisor restart). That is an infra startup race,
    # not the task's own time budget, so retry a few times instead of scoring
    # the task as an instant failure.
    completed = _run_once()
    startup_retries = 0
    while (
        completed.returncode != 0
        and startup_retries < 18
        and "supervisor is still starting" in (completed.stderr or "")
    ):
        time.sleep(10)
        startup_retries += 1
        completed = _run_once()

    (log_dir / "stdout.txt").write_text(completed.stdout or "", encoding="utf-8")
    (log_dir / "stderr.txt").write_text(completed.stderr or "", encoding="utf-8")
    _write_json(
        summary_path,
        {
            "schema": "ouroboros.harness_bench_fast.wrapper.v1",
            "task_id_guess": task_id,
            "workspace": str(workspace),
            "workspace_git": git_workspace,
            "repo_dir": str(repo_dir),
            "data_dir": str(data_dir),
            "settings_path": str(settings_path),
            "model": args.model,
            "memory_mode": args.memory_mode,
            "returncode": completed.returncode,
            "startup_retries": startup_retries,
            "elapsed_sec": round(time.time() - started, 3),
            "stdout_chars": len(completed.stdout or ""),
            "stderr_chars": len(completed.stderr or ""),
            "result_json": str(result_json),
        },
    )
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
