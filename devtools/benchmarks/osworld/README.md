# OSWorld Devtools

This directory contains OSWorld utilities for both logs-only audits and a
submission-shaped external step-loop runner. Official OSWorld reproducibility
still requires a runnable OSWorld checkout plus VM/desktop control
infrastructure; public verified leaderboard claims require the official
verification path.

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
- `run_step_agent.py` is the external OSWorld step-loop runner. It resets an
  official OSWorld VM, saves each screenshot beside the task trajectory under
  the result directory, calls `ouroboros run --attach <screenshot>` for the next
  structured action, executes those actions through `env.step(...)`, and records
  the official trajectory plus denominator-preserving ledgers. It is the runnable
  adapter; the skeleton remains a stricter installed-agent preflight path.

Important step-loop details:

- Screenshots are passed as native image attachments to the model. `vlm_query`
  remains a fallback for non-vision models.
- Shell actions are written into a temporary in-VM script and executed by path;
  the raw command is base64-encoded inside the action snippet. This prevents
  `pkill -f <pattern>` from matching the wrapper process's own argv.
- The prompt is in-app first: when a task names an application, work in that
  application or reopen/verify direct file edits in that application before
  `done`.
- The agent may return a `notes` field; the runner carries recent notes across
  otherwise stateless Ouroboros steps.

Example smoke:

```bash
python devtools/benchmarks/osworld/run_step_agent.py \
  --osworld-root /path/to/OSWorld \
  --task evaluation_examples/examples/multi_apps/48d05431-6cd5-4e76-82eb-12b60d823f7d.json \
  --result_dir results/osworld_step_agent \
  --model anthropic/claude-opus-4-7 \
  --max_steps 5
```

For current official OSWorld-Verified comparisons, run on the official
environment/architecture. Google Drive tasks need `client_secrets.json`; if it
is unavailable, use the documented 361-task exclusion path rather than counting
harness setup crashes as model failures.
