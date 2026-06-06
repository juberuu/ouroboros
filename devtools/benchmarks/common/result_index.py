"""Result indexing utilities shared by benchmark adapters."""

from __future__ import annotations

import json
import pathlib
from typing import Any


def append_result_index(run_dir: pathlib.Path, row: dict[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / "result_index.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
