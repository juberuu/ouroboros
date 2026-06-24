# SWE-bench Pro Methodology Notes

These notes summarize the portable lessons from prior Ouroboros CLI runs on
SWE-bench Pro (`scaleapi/SWE-bench_Pro-os`, dataset `ScaleAI/SWE-bench_Pro`,
images `jefzda/sweap-images:{dockerhub_tag}`, task repositories under `/app`).
They are not a replacement driver or scorer. They document how to prepare
prediction patches and how to inspect official Pro evaluator outputs without
repeating the same failure modes.

Included files:

- `capture_patch.sh`: standalone `model_patch` capture for a task repository.
- `pro_predictions.py`: capture predictions from already-solved prepared repos.
- `e1v2/`: the persistent-agent EVOLUTIONARY harness — solves instances in
  sequence with carried Ouroboros state/source volumes and native post-task
  evolution between tasks (see §3).
- `grade_pro.py`: wrapper that runs the official Pro eval and prints a
  diagnostic, non-leaderboard summary of official per-instance outputs.

## 1. Capturing `model_patch`

Patch capture determines what the official evaluator sees, and it is the most
common source of false failures.

- Capture like the reference SWE-agent/mini-swe-agent scaffold:
  `git add -A && git diff --cached <base_commit>`. A plain
  `git diff <base>` loses new untracked source files, and several real Pro
  fixes add files.

- Write the captured diff to an explicit path outside the Ouroboros repository,
  normally under `/Users/anton/Ouroboros/bench_runs/`. The helper rejects
  repo-internal output paths so benchmark artifacts cannot dirty `devtools/`.

- Remove environment artifacts that `git add -A` can capture. The `JUNK_RE`
  pattern in `capture_patch.sh` intentionally covers runtime dumps, caches,
  dependency folders, build outputs, coverage output, and similar generated
  files. Do not copy broad SWE-agent defaults such as `*.cfg`, `*.toml`, or
  `setup.py`: Pro fixes can legitimately touch configuration and manifests.
  Lockfiles are filtered structurally, not by extension: if a lockfile changes
  while its sibling manifest (`package.json`, `go.mod`, `Cargo.toml`,
  `pyproject.toml`, etc.) did not, and the patch also contains non-lockfile
  source changes, the lockfile is treated as installer/tooling churn and
  dropped. A pure lockfile-only patch is preserved.

- Remove binary blobs. `git diff --cached --numstat <base>` prints
  `-\t-\t<file>` for binary files. Build verification can leave compiled
  binaries in the repository; those can inflate a tiny source patch into a huge
  binary patch. Text additions such as `.go`, `.ts`, and `.py` files remain.

- The E1v2 container entrypoint calls the same `capture_patch.sh` helper mounted
  from `devtools/benchmarks/swe_bench_pro/`, so the persistent-agent path and the
  standalone prediction-capture path share one shell filter. The Python headless
  workspace-patch path applies the same lockfile-without-manifest rule separately
  because it serves real user/workspace artifacts, not just Pro benchmark diffs.

- In workspace mode, capture from the real task repository, usually `/app`, not
  from Ouroboros's internal repository. Verify that `git -C /app status` shows
  the intended modifications after the solve.

- Agent-created scratch files are the agent's responsibility, not a reason to
  over-filter patches. The helper filters environment artifacts and binary
  blobs, not arbitrary source-like files left by the agent.

## 2. Official Pro Eval And Diagnostic Summary

Run the official evaluator:

```bash
python swe_bench_pro_eval.py \
  --use_local_docker \
  --docker_platform linux/amd64 \
  --dockerhub_username jefzda \
  --scripts_dir run_scripts \
  --raw_sample_path <SWE-bench_Pro-os>/helper_code/sweap_eval_full_v2.jsonl \
  --patch_path patches.json
```

`grade_pro.py` wraps this command and then reads official per-instance
`{prefix}_output.json` files to print a diagnostic table. That table is not a
leaderboard result and is not a replacement scorer.

Important details:

- The Pro raw sample uses uppercase `FAIL_TO_PASS` and `PASS_TO_PASS` fields.
  Some Hugging Face-derived rows use lowercase names; handle both when
  inspecting diagnostics.

- If the official progress-bar accuracy aggregator fails or prints a misleading
  zero, inspect per-instance output files directly. The official evaluator
  output remains the source of truth; the local diagnostic only helps debug.

- Pro tamper protection restores test files from the fix commit after applying
  the agent patch. Agent edits to test files do not count as passing fixes.

## 3. Streaming Or Evolutionary Runs

The evolutionary harness is now `e1v2/`. Its hypothesis: solving instances in
sequence *with one self-improvement cycle between each* beats independent frozen
runs, because learned memory and reviewed self-modifications carry forward.

E1v2 contract:

- The task repository is `/app` inside the official SWE-bench Pro image.
- `obo-data` and `obo-repo` volumes carry Ouroboros memory and self-modified
  source across tasks.
- The solve phase runs as one root task without `--workspace` (`dig-direct`) so
  native post-task evolution can promote improvements between tasks.
- Patch capture uses Method C: `git add -A` then `git diff --cached <base_commit>`
  with validated junk/binary filters.
- The benchmark-only evolution steer asks for exactly one reviewed commit and a
  restart, AND in this environment forbids release/version bookkeeping (no
  VERSION/CHANGELOG/README/ARCHITECTURE/pyproject edits, no P9 version-bump
  rule). The standing steer already forbids touching those files, so advisory
  review routinely emits a `version_bump`/`forgotten_touchpoints` finding; that
  is expected and is left advisory. The review enforcement mode is
  owner-controlled — the steer is NOT "resolved" by hardcoding those findings to
  block (BIBLE P3). This prevents the self-hardening deadlock where every
  evolution commit becomes uncommittable under advisory mode.
- The bench-local "Option A" heal for dangling evolution transactions is kept as
  a belt-and-braces in `entrypoint_pro.sh`: at task start it marks a committed
  transaction restart-verified at the container boundary (with a
  `git merge-base --is-ancestor` guard that ABANDONS a rolled-back commit
  instead). A current core's own boot reconciliation + supervisor auto-restart
  makes it a no-op; on agents seeded from an older core it prevents a poison-pill
  that wedges enqueue for every later task (E1v2 → E1).
- `owner_chat_id` is seeded into `state.json` BEFORE the per-task budget reset.
  The reset's load-modify-write creates `state.json` with only zeroed budget
  keys on a fresh volume; seeding after it would leave `owner_chat_id` unset and
  silently disable native post-task evolution (E1v2 would equal E0).

Stateful runs introduce failure classes that frozen baseline runs do not have.

- Budget ledgers can accidentally carry over between tasks. Per-task caps should
  reset per task while learned state/code can carry forward as intended. This is
  exactly what the driver's per-instance `reset_per_task_budget` enforces.

- Count API errors by structured event type, not by substring occurrences inside
  nested provider messages. Separate transient transport failures from
  context-overflow recovery.

- Workspace mode often needs `memory_mode=forked`; shared memory can be
  forbidden with an external workspace. Verify that canonical parent reflections
  still grow across tasks.

- If task N has an infrastructure failure, restore state to the snapshot after
  the last clean task and rerun the suffix. Keep per-task snapshots of runtime
  data and source state.

### 3.1 Retry-on-transient policy (report this honestly)

`auto_run.py` resamples a task when its result looks like an infrastructure
transient rather than a genuine model failure. The gate is
`ok = (patch_bytes is not None) and (patch_bytes > 0 or api_errors == 0)`: a run
that produced an EMPTY patch AND had ≥1 API error is treated as transient — the
`obo-data`/`obo-repo` volumes are rolled back to last-good and the SAME task is
re-run after `--retry-wait` (default 300s), up to `--max-retries` (default 24).
Because each retry restores memory volumes, it is a fresh sample.

This must be disclosed in any results write-up: it is best-of-N **conditioned on
empty-patch+API-error failures**, not pass@1. With routine 429 rate-limit
spikes, a legitimate failure that merely brushed one rate-limit error can be
resampled, which can inflate patch/resolve rates on the affected subset. State
the retry policy, `--max-retries`, and how many instances were resampled. The
secret-opt-in gate: a task refused for missing opt-in is classified by the same
`ok` gate; treat a refused/empty run as not-resolved when reporting, not as a
successful sample.

### 3.2 Seed provenance

The agent under test is seeded from the mounted source (`/opt/ouroboros-ro` →
`cp -a` into `/obo-repo`). Mount a clean checkout at a known tag: a dirty working
tree would leak uncommitted local edits into the measured agent and make the run
non-reproducible. Record the exact seed commit/tag with the results.

## 4. Container And Environment Pitfalls

- glibc runtimes mounted into Alpine/musl task images may not run. Use a
  compatible runtime build or glibc-based images when available.

- Readiness checks need wall-clock limits. Dependency installation under
  emulation can block `/api/state` for several minutes, and not every image has
  `curl`; a Python readiness probe from the agent environment is often more
  portable.

- Do not wait for heartbeat files such as `state/queue_snapshot.json` to become
  quiet. Watch durable outputs such as task reflections or task result files.

- On macOS bind mounts, host-side files can lag behind container writes. For
  live monitoring, read files inside the container with `docker exec`.

## 5. Debugging Checklist

1. Is the patch size reasonable? Huge patches often mean binary blobs; zero-byte
   patches often mean the wrong workspace was captured.
2. Inspect the `*.status.txt` emitted by `capture_patch.sh`.
3. Check raw sample field casing for `FAIL_TO_PASS` and `PASS_TO_PASS`.
4. Compare per-instance `{prefix}_output.json` files to see exactly which tests
   are missing.
5. Confirm that the agent did not rely on test-file edits.
6. Classify API errors by event type and failure class.
7. In stateful runs, check startup budget state before blaming solve quality.
