# Ouroboros Terminal-Bench / Harbor Installed Adapter

## Short Summary

The current adapter runs **full Ouroboros inside each Terminal-Bench task
container**.

High-level flow:

1. Harbor creates the official Terminal-Bench task container.
2. The adapter uploads the current local Ouroboros `repo/` source into that
   container at `/opt/ouroboros-src`.
3. The adapter creates an isolated venv at `/opt/ouroboros-venv`.
4. The adapter installs Ouroboros from the uploaded source.
5. The adapter starts an in-container Ouroboros server/supervisor on
   `127.0.0.1:8765`.
6. The adapter submits the official Terminal-Bench instruction as an external
   workspace task, with `/app` or `/workspace` as the workspace root.
7. Ouroboros solves the task using its normal runtime/tools.
8. Harbor runs the official verifier.

This is intentionally **not** the old host-side terminal bridge. Ouroboros is
not asked to return one shell command per turn. It runs as normal inside the
task container.

## Why Installed Mode

The earlier adapter kept Ouroboros on the host and translated task state into a
JSON command loop. That made traces look artificially weak: Ouroboros saw a
terminal snapshot and had to return one shell command at a time.

The installed adapter evaluates Ouroboros more directly:

- each trial gets a fresh Ouroboros runtime;
- each trial gets a fresh `/logs/agent/ouroboros-data` data directory;
- the task workspace is passed as `workspace_root`;
- Ouroboros uses normal workspace tools and shell tools internally;
- Harbor still owns the task container and verifier.

## What Is Copied Into The Container

The adapter copies the current local source tree:

```text
/Users/anton/Ouroboros/repo -> /opt/ouroboros-src
```

It deliberately excludes local runtime/state noise:

```text
.git
.venv
data
data_evaluated
__pycache__
.pytest_cache
.ruff_cache
build
dist
node_modules
```

So the benchmark container gets current code, but not the operator's main
Ouroboros memory, logs, task results, or chat history.

The host-side adapter writes `source-provenance.json` in the Harbor agent log
directory before upload. It records source commit/version, dirty-state counts,
and hashes; it does not store full diffs or secrets. Publishable runs should use
a clean source tree or preserve this provenance beside the Harbor output.

## Runtime State In The Container

Each trial uses:

```text
OUROBOROS_REPO_DIR=/opt/ouroboros-src
OUROBOROS_DATA_DIR=/logs/agent/ouroboros-data
OUROBOROS_SETTINGS_PATH=/logs/agent/ouroboros-data/settings.json
OUROBOROS_RUNTIME_MODE=pro
OUROBOROS_REVIEW_ENFORCEMENT=advisory
OUROBOROS_TASK_REVIEW_MODE=required
OUROBOROS_MODEL_LIGHT=google/gemini-3.5-flash
OUROBOROS_WORKER_START_METHOD=spawn
```

This means:

```text
1 benchmark task = 1 fresh in-container Ouroboros = 1 unique ouroboros-data folder
```

The host `/Users/anton/Ouroboros/data` is not copied into the container.

## Provider Secret Boundary

Installed-container mode does not inject long-lived provider credentials into
Terminal-Bench task containers by default. If host settings or environment
contain provider keys, the adapter fails closed with a clear error instead of
starting a container that can expose those keys to in-container shell tools.

The intended durable solution is a reviewed host-mediated LLM bridge with scoped
task credentials. For trusted local smoke runs only, an operator may set:

```bash
OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS=1
```

Do not use that opt-in for publishable benchmark runs unless the task container,
logs, and output root are under operator control and the risk is explicitly
accepted.

## Task Instruction Integrity

The adapter now passes the official Terminal-Bench instruction unchanged:

```python
"description": instruction
```

It does not prepend harness notes or task-specific hints.

The only technical wrapper is the API request metadata and `workspace_root`.
For reliability it also passes:

```json
{
  "service_teardown": "keep",
  "timeout_sec": "<Harbor agent timeout when provided>"
}
```

`service_teardown=keep` prevents services started by Ouroboros from being
killed before Harbor's verifier connects to them. Harbor still owns the task
container and final cleanup boundary.

## Workspace Resolution

Most Terminal-Bench tasks use `/app`.

Some images use `/workspace`. The adapter resolves this before starting the
Ouroboros task:

1. use `/app` if it exists;
2. otherwise use `/workspace` if it exists;
3. otherwise create `/app`.

The selected path is passed as external workspace root.

## Lifecycle

Harbor calls:

```python
await agent.setup(environment)
await agent.run(instruction, environment, context)
```

`setup()` is inherited from Harbor's `BaseInstalledAgent`; it calls our
`install()`.

`install()`:

1. uploads clean Ouroboros source into `/opt/ouroboros-src`;
2. installs system basics (`git`, `curl`, `bash`, Python/venv support);
3. if the system Python is older than 3.10, installs Python 3.12 with `uv`;
4. creates `/opt/ouroboros-venv`;
5. installs requirements and editable Ouroboros.

`run()`:

1. uploads the task instruction to `/logs/agent/instruction.txt`;
2. checks configured provider/network reachability;
3. resolves `/app` vs `/workspace`;
4. ensures the workspace is a git worktree root;
5. starts in-container Ouroboros server;
6. creates an Ouroboros task through `/api/tasks`;
7. polls `/api/tasks/<task_id>` until a final status;
8. saves task result and trace files;
9. by default leaves the in-container server running until Harbor finishes the
   verifier/cleanup boundary, so `service_teardown=keep` services remain
   reachable for hidden verifiers; set `leave_server_running_for_verifier=false`
   only for local debugging where no post-run verifier needs live services.

## Why Direct API Polling

The adapter originally used:

```bash
ouroboros run --jsonl ...
```

That was fragile because the CLI stream could hang or get cancelled while the
internal task already had a final state.

The current adapter uses direct API lifecycle:

```text
POST /api/tasks
GET /api/tasks/<task_id>
POST /api/tasks/<task_id>/cancel
```

This gives the adapter a task id immediately, lets it capture task state on
timeout/cancellation, and avoids depending on an SSE/CLI stream.

## Timeout Semantics

The adapter does **not** set an internal task timeout by default:

```python
task_timeout_sec = None
```

That means Harbor controls agent execution timeout from the task config. When
Harbor provides that timeout to the adapter (`task_timeout_sec` agent-kwarg), it
is forwarded to Ouroboros so the agent sees deadline milestones without changing
official limits:

```text
task.toml [agent].timeout_sec
```

Honesty note: on a standard Terminal-Bench leaderboard run Harbor does **not**
pass `task_timeout_sec` to the agent, and `run_tb.py` does not synthesize one
(injecting an arbitrary deadline would be a methodology override, which
`validate_methodology` forbids). So the deadline-milestone / deadline-derived
`run_command` cap features are inert on leaderboard runs by design — they apply
only to user/headless tasks that carry an explicit `deadline_at`. Pass
`task_timeout_sec` yourself only for local experiments, never for a submission.

Setup and environment timeouts are separate:

- environment build/start: Harbor environment timeout;
- agent setup: Harbor setup timeout;
- agent execution: task `[agent].timeout_sec`;
- verifier: task `[verifier].timeout_sec`.

For heavy Docker builds, use:

```bash
--environment-build-timeout-multiplier 4
```

For installed Ouroboros setup, use:

```bash
--agent-setup-timeout-multiplier 4
```

## Common Commands

### Publishable Terminal-Bench 2.1 run

Use `run_tb.py` for leaderboard-shaped runs. It enforces the public
methodology constraints we care about locally: `k >= 5`,
`timeout_multiplier == 1.0`, no resource overrides, and a generated
`metadata.yaml` under the submission tree.

```bash
PYTHONPATH=/Users/anton/Ouroboros/repo \
python devtools/benchmarks/terminal_bench/run_tb.py \
  --model openai/gpt-5.5 \
  --k 5 \
  --n-concurrent 1 \
  --run-root /Users/anton/Ouroboros/bench_runs/terminal_bench/tb21_gpt55 \
  --submission-root /Users/anton/Ouroboros/bench_runs/terminal_bench/submission \
  --execute
```

For a targeted smoke, add repeated `--task` filters before `--execute`, for
example `--task pypi-server --task hf-model-inference --task qemu-alpine-ssh`.

### Terminal-Bench 2.1 smoke

Ledgered smoke runs should go through the wrapper so `run_manifest.json` and
the denominator-preserving `result_index.jsonl` are written beside the Harbor
official output:

```bash
PYTHONPATH=/Users/anton/Ouroboros/repo \
python devtools/benchmarks/terminal_bench/run_harbor_smoke.py \
  --run-root /Users/anton/Ouroboros/bench_runs/terminal_bench/smoke \
  --task terminal-bench/regex-log \
  --n-concurrent 1 \
  --execute
```

Raw Harbor commands are useful for local debugging of the installed agent, but
they do not write the Ouroboros denominator ledger unless wrapped by
`run_harbor_smoke.py`.

```bash
PYTHONPATH=/Users/anton/Ouroboros/repo \
harbor run \
  --dataset terminal-bench/terminal-bench-2-1 \
  --include-task-name terminal-bench/regex-log \
  --agent-import-path devtools.benchmarks.terminal_bench.harbor_installed_agent:OuroborosTerminalBenchAgent \
  --model ouroboros-gpt-5.5-tb21-smoke \
  --agent-kwarg ouroboros_model=openai/gpt-5.5 \
  --agent-kwarg install_timeout_sec=1200 \
  --agent-kwarg server_start_timeout_sec=240 \
  --agent-setup-timeout-multiplier 4 \
  --n-concurrent 1 \
  --n-tasks 1 \
  --yes \
  --force-build
```

### Full cached Terminal-Bench 2.0-style dataset

Debug-only raw Harbor form; for publishable ledgered runs, mirror these options
through `run_harbor_smoke.py` or write an explicit wrapper that emits
`run_manifest.json` and `result_index.jsonl`.

```bash
PYTHONPATH=/Users/anton/Ouroboros/repo \
harbor run \
  --path /Users/anton/Ouroboros/data/harbor_local_datasets/terminal_bench_full_cached_89 \
  --agent-import-path devtools.benchmarks.terminal_bench.harbor_installed_agent:OuroborosTerminalBenchAgent \
  --model ouroboros-gpt-5.5-full \
  --agent-kwarg ouroboros_model=openai/gpt-5.5 \
  --agent-kwarg install_timeout_sec=1200 \
  --agent-kwarg server_start_timeout_sec=240 \
  --agent-setup-timeout-multiplier 4 \
  --n-concurrent 1 \
  --yes \
  --force-build
```

### Full Terminal-Bench 2.1

Debug-only raw Harbor form; it preserves Harbor's official output but not the
Ouroboros denominator ledger.

```bash
PYTHONPATH=/Users/anton/Ouroboros/repo \
harbor run \
  --dataset terminal-bench/terminal-bench-2-1 \
  --agent-import-path devtools.benchmarks.terminal_bench.harbor_installed_agent:OuroborosTerminalBenchAgent \
  --model ouroboros-gpt-5.5-tb21-full \
  --agent-kwarg ouroboros_model=openai/gpt-5.5 \
  --agent-kwarg install_timeout_sec=1200 \
  --agent-kwarg server_start_timeout_sec=240 \
  --agent-setup-timeout-multiplier 4 \
  --environment-build-timeout-multiplier 4 \
  --n-concurrent 1 \
  --yes \
  --force-build
```

## Model Selection

Harbor's `--model` is metadata for the Harbor result.

The actual Ouroboros model is passed via:

```bash
--agent-kwarg ouroboros_model=<provider/model>
```

Examples:

```bash
--agent-kwarg ouroboros_model=openai/gpt-5.5
--agent-kwarg ouroboros_model=google/gemini-3.5-flash
--agent-kwarg ouroboros_model=anthropic/claude-opus-4-7
```

The adapter sets:

```text
OUROBOROS_MODEL
OUROBOROS_MODEL_CODE
```

to the measured model inside the container. `OUROBOROS_MODEL_LIGHT` defaults to
`google/gemini-3.5-flash` and can be overridden with
`--agent-kwarg ouroboros_light_model=<provider/model>` or `run_tb.py
--light-model ...`. This avoids accidentally running safety checks and
lightweight JSON decisions on the expensive measured model.

## Infra-Failure Semantics

OpenRouter credit exhaustion used to produce quiet zero-reward tails. The
adapter now:

- runs a host-side OpenRouter credit preflight when a key is configured
  (`OUROBOROS_BENCH_OPENROUTER_MIN_CREDIT_USD`, default `$5`);
- treats `llm_api_error` / `infra_failed` as adapter errors rather than
  ordinary semantic failures;
- writes `openrouter-credit-preflight.json` beside the agent logs.

## Trace Locations

For each Harbor trial:

```text
<trial>/agent/ouroboros-data/
```

contains the fresh in-container Ouroboros data directory.

Useful files:

```text
<trial>/agent/ouroboros-data/logs/events.jsonl
<trial>/agent/ouroboros-data/logs/progress.jsonl
<trial>/agent/ouroboros-data/logs/supervisor.jsonl
<trial>/agent/ouroboros-data/state/headless_tasks/<task_id>/data/logs/tools.jsonl
<trial>/agent/ouroboros-task-result.json
<trial>/agent/ouroboros-run.jsonl
<trial>/agent/ouroboros-run-summary.json
<trial>/verifier/test-stdout.txt
<trial>/verifier/reward.txt
```

Heavy files usually come from:

```text
<trial>/agent/ouroboros-data/task_results/artifacts/<task_id>/workspace.patch
<trial>/agent/ouroboros-data/task_results/artifacts/<task_id>/workspace_patch.json
```

Those can be omitted when creating logs-only bundles.

## Known Infrastructure Notes

- Old task images with Python 3.9 require adapter-installed Python 3.12 via
  `uv`; this is handled automatically.
- Some task Docker builds need more than 600 seconds; use
  `--environment-build-timeout-multiplier`.
- Some tasks still hit Harbor `AgentTimeoutError`; verifier can still produce a
  reward if the workspace has enough final state.
- `RuntimeError` from the adapter should not be used for ordinary Ouroboros
  `status=failed`; the adapter records task status and returns control so Harbor
  can run the verifier.

## Files To Share With Developers

Minimum:

```text
repo/devtools/benchmarks/terminal_bench/harbor_installed_agent.py
```

Recommended:

```text
repo/devtools/benchmarks/terminal_bench/README.md
```

Useful example result:

```text
data/harbor_jobs/ouroboros_v650_tb21_smoke_gpt55/2026-05-29__00-39-23/result.json
```
