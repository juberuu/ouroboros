# SWE-bench Pro Devtools

SWE-bench Pro is kept separate from standard SWE-bench because the colleague
materials target the `SWE-bench_Pro-os` evaluator and a Pro-specific patch JSON
handoff.

Files:

- `METHODOLOGY.md` documents the capture and grading assumptions.
- `capture_patch.sh` captures a task-repository patch with untracked text files,
  filters environment junk, drops binary blobs, and requires an explicit output
  path outside the Ouroboros repo.
- `pro_predictions.py` creates Ouroboros-style prediction JSONL by running
  `capture_patch.sh` for prepared task repositories.
- `grade_pro.py` invokes the official Pro evaluator when `--skip-run` is not
  supplied, then aggregates official per-instance outputs.

The aggregation in `grade_pro.py` is not replacement scoring. The official Pro
eval output remains the source of truth.
