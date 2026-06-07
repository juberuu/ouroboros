# Benchmark Devtools

This directory contains thin adapters around official benchmark harnesses. The
adapters prepare Ouroboros tasks, capture artifacts, and preserve traces; they
do not replace official scoring.

Supported surfaces:

- ProgramBench: official `programbench eval/info` and cleanroom submission
  layout.
- Terminal-Bench: Harbor installed-agent adapter.
- SWE-bench and SWE-bench Verified: standard predictions JSONL for the official
  SWE-bench harness.
- SWE-bench Pro: official Pro eval wrapper and patch-capture methodology. The
  prediction/capture path is ledgered; the grader wrapper stays
  official-output-only.
- OSWorld: stop-closed skeleton and logs normalizer only.

Shared sidecar schemas:

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
