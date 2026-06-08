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
- `evolve_pro.py`: the isolated EVOLUTIONARY driver — solves instances in sequence
  with one post-task self-evolution cycle between each (see §3).
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

- Remove environment artifacts that `git add -A` can capture. The
  `JUNK_RE` pattern in `capture_patch.sh` intentionally covers runtime dumps,
  caches, dependency folders, build outputs, coverage output, and similar
  generated files. Do not copy broad SWE-agent defaults such as
  `*.cfg`, `*.toml`, `setup.py`, or `*.lock`: Pro fixes can legitimately touch
  configuration and lock files.

- Remove binary blobs. `git diff --cached --numstat <base>` prints
  `-\t-\t<file>` for binary files. Build verification can leave compiled
  binaries in the repository; those can inflate a tiny source patch into a huge
  binary patch. Text additions such as `.go`, `.ts`, and `.py` files remain.

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

The evolutionary driver is `evolve_pro.py`. Its hypothesis: solving instances in
sequence *with one self-improvement cycle between each* beats independent frozen
runs, because learned memory and reviewed self-modifications carry forward.

Driver contract (all isolated — the live Ouroboros is never touched):

- It runs on a throwaway `git clone --no-hardlinks` of the Ouroboros repo and an
  isolated `OUROBOROS_DATA_DIR`, seeded from live `settings.json` for provider keys
  and model slots only. Post-task self-evolution (`OUROBOROS_POST_TASK_EVOLUTION`,
  the C1 envelope) is enabled in that isolated settings. Because the headless CLI
  runs the worker but not the supervisor tick that applies the C1 durable signal,
  `--evolve-between` (default on) drives ONE explicit self-evolution cycle on the
  *clone* between instances: a non-workspace shared-memory run where the agent may
  commit ONE reviewed improvement or record the lesson. Learned code/memory carries
  into the next solve; the live body is never modified.
- Per instance it checks out `base_commit`, runs standalone
  `ouroboros run --workspace <repo> --memory-mode forked` (external workspaces
  forbid `shared`; forked still carries the isolated canonical memory across
  instances), and captures the
  `model_patch` with `capture_patch.sh`. Output is a `predictions.jsonl` you feed
  directly to `grade_pro.py` (official scorer remains source of truth).
- Between instances it calls the guarded `supervisor.state.reset_per_task_budget`
  (isolated root ONLY) so a fresh instance is not falsely flagged
  `budget: emergency`; learned reflections/code carry forward.
- `--demo N` synthesizes self-contained instances (no dataset/Docker) so the whole
  loop — solve, capture, budget-reset, between-instance evolution — can be smoke
  tested before committing real Docker time.

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
