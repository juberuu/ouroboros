"""Declarative official benchmark command builders."""

from __future__ import annotations

import pathlib


def programbench_eval_cmd(run_root: pathlib.Path) -> list[str]:
    return ["programbench", "eval", str(run_root)]


def programbench_info_cmd(run_root: pathlib.Path) -> list[str]:
    return ["programbench", "info", str(run_root)]


def swebench_eval_cmd(dataset_name: str, predictions_path: pathlib.Path, run_id: str, workers: int = 1) -> list[str]:
    return [
        "python",
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        dataset_name,
        "--predictions_path",
        str(predictions_path),
        "--max_workers",
        str(int(workers)),
        "--run_id",
        run_id,
    ]
