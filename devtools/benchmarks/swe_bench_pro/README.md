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
  supplied, then aggregates official per-instance outputs. It intentionally
  remains official-output-only; the Ouroboros denominator ledger is emitted by
  `pro_predictions.py` for the prediction/capture phase.
- `e1v2/` contains the persistent-agent evolutionary harness: sequential Pro
  tasks with carried Ouroboros data/source volumes and native post-task
  evolution between tasks.
- `task_order_pro_70.csv` records the 70-task order plus the frozen E0 baseline
  verdicts used for E1v2 comparisons.

The aggregation in `grade_pro.py` is not replacement scoring. The official Pro
eval output remains the source of truth.

`pro_predictions.py` writes the official prediction JSONL plus sidecars:

- `<predictions>.ledger.jsonl` records every requested instance, including
  capture failures and empty patches.
- `<predictions>.errors.jsonl` records failed capture rows.
- `<predictions>.run_manifest.json` records source/model/output provenance.

`capture_patch.sh` deliberately keeps source/config fixes such as `setup.py`,
`pyproject.toml`, and lockfiles. It filters environment junk and binary blobs,
not broad config-like paths.

`evolve_pro.py` was removed after v6.26. It used external workspace tasks, which
structurally suppressed cross-task self-evolution in older Ouroboros releases.
Use `e1v2/run_pro.py` / `e1v2/auto_run.py` for evolutionary runs and
`pro_predictions.py` for frozen prepared-repo predictions.
