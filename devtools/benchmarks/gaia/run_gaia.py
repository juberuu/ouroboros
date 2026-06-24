#!/usr/bin/env python3
"""Generate GAIA predictions with the reviewed Ouroboros CLI adapter.

The adapter intentionally keeps scoring official: it prepares a run root,
records exact settings/argv, and uses an inspect-evals solver wrapper that reads
Ouroboros's structured ``final_answer`` via ``--result-json-out``.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import MODEL_SLOT_KEYS, benchmark_run_manifest, write_json
from devtools.benchmarks.common.run_roots import ensure_outside_repo, run_root
from ouroboros.config import SETTINGS_DEFAULTS

REPO = pathlib.Path(__file__).resolve().parents[3]
HERE = pathlib.Path(__file__).resolve().parent
_GAIA_PINNED_MODEL_KEYS = {
    "OUROBOROS_MODEL",
    "OUROBOROS_MODEL_HEAVY",
    "OUROBOROS_MODEL_LIGHT",
    "OUROBOROS_MODEL_VISION",
    "OUROBOROS_MODEL_CONSCIOUSNESS",
    "OUROBOROS_MODEL_FALLBACKS",
    "OUROBOROS_MODEL_DEEP_SELF_REVIEW",
    "OUROBOROS_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODELS",
    "OUROBOROS_SCOPE_REVIEW_MODEL",
}
_PROVIDER_ENV_KEYS = {
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_COMPATIBLE_API_KEY",
    "OPENAI_COMPATIBLE_BASE_URL",
    "ANTHROPIC_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_API_KEY",
    "CLOUDRU_FOUNDATION_MODELS_BASE_URL",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_USER",
    "GIGACHAT_PASSWORD",
    "GITHUB_TOKEN",
}


def _credential_keys_for_model(model: str) -> set[str]:
    text = str(model or "")
    if text.startswith("openai::"):
        return {"OPENAI_API_KEY", "OPENAI_BASE_URL"}
    if text.startswith("anthropic::"):
        return {"ANTHROPIC_API_KEY"}
    if text.startswith("cloudru::"):
        return {"CLOUDRU_FOUNDATION_MODELS_API_KEY", "CLOUDRU_FOUNDATION_MODELS_BASE_URL"}
    if text.startswith("gigachat::"):
        return {"GIGACHAT_CREDENTIALS", "GIGACHAT_USER", "GIGACHAT_PASSWORD"}
    if text.startswith("openai-compatible::"):
        return {"OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"}
    return {"OPENROUTER_API_KEY"}


def _sanitized_host_env(solve_model: str) -> dict[str, str]:
    blocked = set(SETTINGS_DEFAULTS) | _PROVIDER_ENV_KEYS
    blocked.update(key for key in os.environ if key.startswith("USE_LOCAL_") or key.startswith("OUROBOROS_"))
    keep = {key: value for key, value in os.environ.items() if key not in blocked}
    for key in _credential_keys_for_model(solve_model):
        if os.environ.get(key):
            keep[key] = os.environ[key]
    return keep


def _render_run_settings(base_settings_path: pathlib.Path, solve_model: str, run_dir: pathlib.Path) -> pathlib.Path:
    settings = json.loads(base_settings_path.read_text(encoding="utf-8"))
    for key in MODEL_SLOT_KEYS:
        if key.startswith("OUROBOROS_EFFORT_"):
            continue
        if key not in _GAIA_PINNED_MODEL_KEYS:
            continue
        if key == "OUROBOROS_REVIEW_MODELS":
            settings[key] = ",".join([solve_model] * 3)
        elif key:
            settings[key] = solve_model
    settings["OUROBOROS_RUNTIME_MODE"] = "light"
    settings["OUROBOROS_TASK_REVIEW_MODE"] = "required"
    settings["OUROBOROS_MAX_WORKERS"] = 1
    settings["OUROBOROS_POST_TASK_EVOLUTION"] = "false"
    path = run_dir / "settings.json"
    write_json(path, settings)
    return path


def _settings_env(settings_path: pathlib.Path, solve_model: str, run_dir: pathlib.Path) -> dict[str, str]:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    env = {
        k: str(v)
        for k, v in settings.items()
        if k not in _PROVIDER_ENV_KEYS and v not in (None, "") and not isinstance(v, (list, dict))
    }
    for key in MODEL_SLOT_KEYS:
        if key.startswith("OUROBOROS_EFFORT_"):
            continue
        if key not in _GAIA_PINNED_MODEL_KEYS:
            continue
        if key == "OUROBOROS_REVIEW_MODELS":
            env[key] = ",".join([solve_model] * 3)
        elif key:
            env[key] = solve_model
    env["OUROBOROS_SETTINGS_PATH"] = str(settings_path)
    env["OUROBOROS_DATA_DIR"] = str(run_dir / "ouroboros_data")
    port = 19000 + (os.getpid() % 1000)
    env["OUROBOROS_SERVER_PORT"] = str(port)
    env["GAIA_OUROBOROS_URL"] = f"http://127.0.0.1:{port}"
    return env


def _write_manifest(root: pathlib.Path, args: argparse.Namespace, planned_argv: list[str], settings_path: pathlib.Path) -> None:
    requested = [f"{args.split}:level{args.level}:{idx}" for idx in range(1, int(args.limit) + 1)]
    manifest = benchmark_run_manifest(
        benchmark="gaia",
        run_root=root,
        repo_dir=REPO,
        requested_task_ids=requested,
        metadata={
            "argv": planned_argv,
            "dataset": "inspect_evals/gaia",
            "official_command": planned_argv,
            "settings_path": str(settings_path),
            "isolated_data_root": str(root / "ouroboros_data"),
            "output_paths": {"inspect_logs": str(root / "inspect_logs"), "samples": str(root / "samples")},
            "harness": {"solver": "inspect_solver/ouroboros_solver.py", "official_scorer": "gaia_scorer"},
            "extra": {
                "split": args.split,
                "level": args.level,
                "limit": args.limit,
                "solve_model": args.solve_model,
                "image_input_mode": json.loads(settings_path.read_text(encoding="utf-8")).get("OUROBOROS_IMAGE_INPUT_MODE", ""),
            },
        },
    )
    manifest["model_slots"] = {k: v for k, v in _settings_env(settings_path, args.solve_model, root).items() if k in MODEL_SLOT_KEYS}
    write_json(root / "run_manifest.json", manifest)


def build_inspect_argv(args: argparse.Namespace, run_dir: pathlib.Path) -> list[str]:
    solver = HERE / "inspect_solver" / "ouroboros_solver.py"
    return [
        sys.executable,
        "-m",
        "inspect_ai",
        "eval",
        "inspect_evals/gaia",
        "-T",
        f"subset=2023_level{int(args.level)}",
        "-T",
        f"split={args.split}",
        "--solver",
        f"{solver}@ouroboros_solver",
        "--limit",
        str(args.limit),
        "--log-format",
        "json",
        "--log-dir",
        str(run_dir / "inspect_logs"),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Ouroboros on GAIA via the official inspect_evals task.")
    parser.add_argument("--out-dir", default="", help="output run directory (outside repo/data)")
    parser.add_argument("--settings", default=str(HERE / "settings_base.json"))
    parser.add_argument("--solve-model", default="google/gemini-2.5-pro")
    parser.add_argument("--split", default="validation")
    parser.add_argument("--level", type=int, default=1)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true", help="write manifest and planned argv without spending")
    args = parser.parse_args(argv)

    out = pathlib.Path(args.out_dir).expanduser() if args.out_dir else run_root("gaia")
    out = ensure_outside_repo(out, REPO)
    planned = build_inspect_argv(args, out)
    base_settings_path = pathlib.Path(args.settings).expanduser().resolve(strict=False)
    settings_path = _render_run_settings(base_settings_path, args.solve_model, out)
    _write_manifest(out, args, planned, settings_path)
    if args.dry_run:
        print(json.dumps({"run_root": str(out), "planned_argv": planned}, indent=2))
        return 0
    env = {
        **_sanitized_host_env(args.solve_model),
        **_settings_env(settings_path, args.solve_model, out),
        "GAIA_OUROBOROS_RUN_ROOT": str(out),
        "GAIA_OUROBOROS_SETTINGS": str(settings_path),
        "GAIA_OUROBOROS_SOLVE_MODEL": args.solve_model,
    }
    return subprocess.run(planned, env=env).returncode


if __name__ == "__main__":
    raise SystemExit(main())
