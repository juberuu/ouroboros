"""Result indexing utilities shared by benchmark adapters."""

from __future__ import annotations

import json
import pathlib
import time
from typing import Any


def append_result_index(run_dir: pathlib.Path, row: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "result_index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def task_result_row(
    *,
    benchmark: str,
    instance_id: str,
    status: str,
    metadata: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Create a denominator-preserving per-task result row."""
    meta = dict(metadata or {})
    for key, value in overrides.items():
        if value is not None:
            meta[key] = value
    return {
        "schema": "ouroboros.benchmark.task_result.v1",
        "ts_unix": time.time(),
        "benchmark": benchmark,
        "instance_id": str(instance_id),
        "status": status,
        "reason_code": str(meta.get("reason_code") or ""),
        "prediction_written": bool(meta.get("prediction_written")),
        "official_eval_status": str(meta.get("official_eval_status") or "not_run"),
        "output_paths": meta.get("output_paths") or {},
        "error": str(meta.get("error") or ""),
        "details": meta.get("details") or {},
    }


def write_result_index(path: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + ("\n" if rows else ""),
        encoding="utf-8",
    )
