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
- SWE-bench Pro: official Pro eval wrapper and patch-capture methodology.
- OSWorld: stop-closed skeleton and logs normalizer only.
