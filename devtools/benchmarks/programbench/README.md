# ProgramBench

This adapter prepares Ouroboros workspace tasks for official ProgramBench
cleanroom execution.

Invariants:

- Use official `programbench` CLI for evaluation and summaries.
- Use `task_cleanroom` task images; do not score locally.
- Tool execution for the benchmark workspace runs in a no-network Docker
  backend.
- Reference binaries are declared through
  `resource_policy.protected_artifacts`: execute is allowed, byte reads,
  copy/hash/static introspection/tracing/debugging are denied.
- Submission artifact is `<run>/<instance_id>/submission.tar.gz`.
