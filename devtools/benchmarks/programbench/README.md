# ProgramBench

This adapter **prepares** Ouroboros workspace tasks for official ProgramBench
cleanroom execution. It is prepare/package-only: it writes the per-case
`ouroboros_task_body.json` and submission artifacts but does NOT submit them to
the gateway or run an end-to-end solve loop — there is no built-in e2e runner
(deferred). Drive the prepared tasks through the official `programbench` CLI /
your own submission harness. Run dirs default under `bench_runs/`; set
`OUROBOROS_BENCH_RUNS_ROOT` to redirect (the test suite pins it to a temp dir so
runs never leak into a developer's `bench_runs/`).

Invariants:

- Use official `programbench` CLI for evaluation and summaries.
- Use `task_cleanroom` task images; do not score locally.
- Tool execution for the benchmark workspace runs in a no-network Docker
  backend.
- Reference binaries are declared through
  `resource_policy.protected_artifacts`: execute is allowed, byte reads,
  copy/hash/static introspection/tracing/debugging are denied.
- Submission artifact is `<run>/<instance_id>/submission.tar.gz`.
- `run_programbench.py` writes `run_manifest.json` and `result_index.jsonl`
  sidecars with cleanroom preflight, protected path, submission, and official
  eval command provenance. These files are audit artifacts; official
  `programbench eval/info` output remains the scoring source.
