# OSWorld Devtools

This directory is intentionally a stop-closed skeleton for v6.19.0-rc.1.

Local material currently available in this workspace is a logs-only bundle:

```text
/Users/anton/Ouroboros/bench_logs/osworld_sample60_seed20260603_opus47_logs_only
```

That bundle is useful for trace inspection, but it is not enough to claim
official OSWorld reproducibility. Official OSWorld local evaluation requires a
runnable OSWorld checkout, desktop/control infrastructure, and an agent adapter
inside the official runner. Public verified leaderboard claims require the
official verification path.

Files:

- `normalize_logs.py` indexes logs-only bundles for analysis.
- `schemas.py` validates the known logs-only JSON layout.
- `osworld_adapter_skeleton.py` refuses to run unless the official environment,
  live Ouroboros server, computer-use payload, and output-root isolation are all
  present. It also requires `computer_use` to have a fresh executable review
  under the blocking review gate (`pass`/`advisory_pass` legacy aliases or
  canonical `clean`/`warnings`) and then pass `skill_readiness_for_execution()`
  for enabled state, grants, and dependencies. It writes fail-closed
  ledger/manifest artifacts for blocked preflights when the output root is
  outside `repo/` and runtime `data/`.
  The readiness probe uses the same runtime skill loader/readiness gate and may
  initialize empty state directories under the declared isolated data root. If
  `--data-root` is omitted, the CLI uses `<output-root>/isolated_data`; it must
  not point at live `/Users/anton/Ouroboros/data` for smoke runs.

No reward calculation, VM reset flow, or leaderboard scoring is implemented in
this release.
