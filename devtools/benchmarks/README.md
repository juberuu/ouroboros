# Ouroboros Benchmark Devtools

This directory contains tracked operator tooling for reproducible benchmark
work. These files are reviewed when touched, but are not imported by the
runtime core and are not packaged as app runtime code.

## Integrations

- `terminal_bench/` — Harbor installed-agent adapter for Terminal-Bench 2.1.
  Use `run_tb.py` for leaderboard-shaped k-trial runs and submission layout;
  use `run_harbor_smoke.py` for small local smoke runs.
- `osworld/` — OSWorld logs tooling plus `run_step_agent.py`, an official
  env.step-shaped runner that passes VM screenshots as native image attachments
  to Ouroboros.
- `swe_bench_pro/` — SWE-bench Pro patch capture/grading. Frozen prepared
  repos use `pro_predictions.py`; persistent evolutionary runs use
  `e1v2/run_pro.py` / `e1v2/auto_run.py`.
- `swe_bench/` — standard SWE-bench prediction helpers.
- `programbench/` — ProgramBench cleanroom runner.
- `harness_bench_fast/` — Ouroboros CLI wrapper and methodology notes for the
  public `ai-forever/harness-bench-fast` runner.
- `common/` — shared manifests, result ledgers, safe run roots, secret hygiene,
  and official command builders.

## Output Roots

Write generated run artifacts under an explicit benchmark output root outside
`repo/` and outside live runtime `data/`, typically
`/Users/anton/Ouroboros/bench_runs/...`. Tests must set
`OUROBOROS_BENCH_RUNS_ROOT` to a temporary directory so local test runs do not
pollute real benchmark bundles.

## Shared Sidecar Schemas

- Run manifests record non-secret provenance: requested task ids where the
  benchmark runner exposes them before execution, requested counts/selection
  slots for deterministic first-N runs such as Terminal-Bench, exact argv,
  official command shape, output paths, model slots, source commit, dirty-state
  counts, and hashes. Defaults are adapter-specific (`run_manifest.json`,
  `<predictions>.run_manifest.json`, or `osworld_preflight.run_manifest.json`).
- Result ledgers are denominator-preserving Ouroboros JSONL files. They record
  every requested instance, including setup failures, timeouts, and empty
  patches, even when the official benchmark prediction/submission format only
  accepts successful rows. Defaults are adapter-specific (`result_index.jsonl`,
  `<predictions>.ledger.jsonl`, or `osworld_preflight.ledger.jsonl`).

These sidecars are audit artifacts, not replacement scoring. Official benchmark
harnesses and official result files remain the scoring authority.

## Methodology Rule

Benchmark changes must be general-purpose harness improvements first. Do not add
task-specific answers, hidden verifier knowledge, or resource/timeout overrides
that violate a benchmark's official submission rules.
