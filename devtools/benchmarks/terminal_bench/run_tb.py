#!/usr/bin/env python3
"""Terminal-Bench 2.1 runner/submission helper for Ouroboros.

The official leaderboard requires at least k=5 trials, default timeout/resource
settings, metadata.yaml, and full Harbor artifacts. This wrapper keeps those
methodology constraints visible instead of relying on ad-hoc shell history.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shlex
import subprocess
import sys
from dataclasses import dataclass

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import benchmark_run_manifest, write_json
from devtools.benchmarks.common.run_roots import default_settings_path, ensure_outside_repo, repo_root_from_devtools
from devtools.benchmarks.terminal_bench.run_harbor_smoke import AGENT_IMPORT


DEFAULT_DATASET = "terminal-bench/terminal-bench-2-1"


@dataclass
class HarborCommandConfig:
    dataset: str
    model: str
    k: int
    jobs_dir: pathlib.Path
    harbor_bin: str
    n_concurrent: int
    task_filters: list[str]
    settings_path: pathlib.Path
    execute: bool
    light_model: str


def _effective_helper_models(measured_model: str, light_model: str) -> list[tuple[str, str]]:
    """Resolve EVERY model that materially assists a measured run, with its role.

    With ``task_review_mode=required`` the host forces a multi-model
    task-acceptance review whose feedback re-enters the measured agent's
    context, so the review triad / scope / light / web-search models genuinely
    assist the run. Declaring only the measured model in metadata.yaml would
    misrepresent the submission. Values mirror what the container resolves
    (env override else the shipped config defaults) so the declared set matches
    reality. Returns ordered (model_id, role) pairs, deduped by model id.
    """
    review_default = "openai/gpt-5.5,google/gemini-3.5-flash,anthropic/claude-opus-4.8"
    websearch_default = "gpt-5.2"
    scope_default = "openai/gpt-5.5"
    review = os.environ.get("OUROBOROS_REVIEW_MODELS", review_default) or review_default
    scope = (os.environ.get("OUROBOROS_SCOPE_REVIEW_MODELS")
             or os.environ.get("OUROBOROS_SCOPE_REVIEW_MODEL") or scope_default)
    websearch = os.environ.get("OUROBOROS_WEBSEARCH_MODEL", websearch_default) or websearch_default
    ordered: list[tuple[str, str]] = [(measured_model, "agent")]
    for m in review.split(","):
        if m.strip():
            ordered.append((m.strip(), "commit_review_triad"))
    for m in scope.split(","):
        if m.strip():
            ordered.append((m.strip(), "scope_review"))
    if light_model.strip():
        ordered.append((light_model.strip(), "light_safety"))
    if websearch.strip():
        ordered.append((websearch.strip(), "web_search"))
    deduped: dict[str, str] = {}
    for model_id, role in ordered:
        if model_id in deduped:
            if role not in deduped[model_id].split("+"):
                deduped[model_id] = deduped[model_id] + "+" + role
        else:
            deduped[model_id] = role
    return list(deduped.items())


def leaderboard_metadata(*, agent_name: str, org_name: str, model: str, light_model: str = "") -> str:
    lines = [
        "agent_url: https://github.com/razzant/ouroboros",
        f"agent_display_name: {json.dumps(agent_name)}",
        f"agent_org_display_name: {json.dumps(org_name)}",
        "models:",
    ]
    for model_id, role in _effective_helper_models(model, light_model):
        provider = model_id.split("/", 1)[0] if "/" in model_id else "openrouter"
        display = model_id.split("/", 1)[1] if "/" in model_id else model_id
        lines.append(f"  - model_name: {json.dumps(model_id)}")
        lines.append(f"    model_provider: {json.dumps(provider)}")
        lines.append(f"    model_display_name: {json.dumps(display)}")
        lines.append(f"    model_org_display_name: {json.dumps(provider)}")
        lines.append(f"    role: {json.dumps(role)}")
    return "\n".join(lines) + "\n"


def validate_methodology(*, k: int, timeout_multiplier: float, resource_overrides: list[str]) -> None:
    if int(k) < 5:
        raise ValueError("Terminal-Bench leaderboard mode requires k >= 5")
    if float(timeout_multiplier) != 1.0:
        raise ValueError("Terminal-Bench leaderboard mode requires timeout_multiplier == 1.0")
    if resource_overrides:
        raise ValueError(f"Terminal-Bench leaderboard mode forbids resource overrides: {resource_overrides}")


def harbor_command(config: HarborCommandConfig) -> list[str]:
    cmd = [
        config.harbor_bin,
        "run",
        "--dataset",
        config.dataset,
        "--agent-import-path",
        AGENT_IMPORT,
        "--model",
        f"ouroboros-{config.model.replace('/', '-')}",
        "--agent-kwarg",
        f"ouroboros_model={config.model}",
        "--agent-kwarg",
        f"ouroboros_light_model={config.light_model}",
        "--agent-kwarg",
        f"host_settings_path={config.settings_path}",
        "--agent-kwarg",
        "task_review_mode=required",
        "--agent-kwarg",
        "install_timeout_sec=1200",
        "--agent-kwarg",
        "server_start_timeout_sec=240",
        "--n-concurrent",
        str(int(config.n_concurrent)),
        "-k",
        str(int(config.k)),
        "--jobs-dir",
        str(config.jobs_dir),
        "--yes",
    ]
    for task in config.task_filters:
        cmd.extend(["--include-task-name", task])
    if config.execute:
        cmd.append("--force-build")
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--model", required=True)
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--n-concurrent", type=int, default=1)
    parser.add_argument("--task", action="append", default=[], help="optional include-task-name; repeatable")
    parser.add_argument("--run-root", default="")
    parser.add_argument("--submission-root", default="")
    parser.add_argument("--settings-path", default="")
    parser.add_argument("--harbor-bin", default="harbor")
    parser.add_argument("--light-model", default="google/gemini-3.5-flash")
    parser.add_argument("--timeout-multiplier", type=float, default=1.0)
    parser.add_argument("--resource-override", action="append", default=[])
    parser.add_argument("--agent-name", default="Ouroboros")
    parser.add_argument("--org-name", default="Ouroboros")
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)

    validate_methodology(
        k=args.k,
        timeout_multiplier=args.timeout_multiplier,
        resource_overrides=list(args.resource_override or []),
    )

    repo = repo_root_from_devtools()
    run_root = ensure_outside_repo(
        pathlib.Path(args.run_root).expanduser() if args.run_root else pathlib.Path.cwd() / "tb21_ouroboros_run",
        repo,
    )
    settings_path = pathlib.Path(args.settings_path).expanduser() if args.settings_path else default_settings_path()
    submission_root = ensure_outside_repo(
        pathlib.Path(args.submission_root).expanduser()
        if args.submission_root
        else run_root / "submission",
        repo,
    )
    job_dir = submission_root / "submissions" / "terminal-bench" / "2.1" / f"ouroboros__{args.model.replace('/', '-')}" / "job"
    job_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = job_dir.parent / "metadata.yaml"
    metadata_path.write_text(
        leaderboard_metadata(agent_name=args.agent_name, org_name=args.org_name, model=args.model, light_model=args.light_model),
        encoding="utf-8",
    )

    cmd = harbor_command(HarborCommandConfig(
        dataset=args.dataset,
        model=args.model,
        k=args.k,
        jobs_dir=job_dir,
        harbor_bin=args.harbor_bin,
        n_concurrent=args.n_concurrent,
        task_filters=list(args.task or []),
        settings_path=settings_path,
        execute=bool(args.execute),
        light_model=args.light_model,
    ))
    write_json(
        run_root / "run_manifest.json",
        benchmark_run_manifest(
            benchmark="terminal_bench",
            run_root=run_root,
            repo_dir=repo,
            requested_task_ids=list(args.task or []),
            metadata={
                "dataset": args.dataset,
                "k": int(args.k),
                "timeout_multiplier": float(args.timeout_multiplier),
                "resource_overrides": list(args.resource_override or []),
                "leaderboard_submission_root": str(submission_root),
                "metadata_yaml": str(metadata_path),
                "official_command": cmd,
            },
        ),
    )
    (run_root / "harbor_command.txt").write_text(shlex.join(cmd) + "\n", encoding="utf-8")
    print(shlex.join(cmd))
    if not args.execute:
        return 0
    completed = subprocess.run(cmd, cwd=repo, env={**os.environ, "PYTHONPATH": str(repo)})
    return int(completed.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
