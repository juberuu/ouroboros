# SWE-bench Pro E1v2

This directory is the cleaned repo-tracked port of the colleague `bench_kit`
E1v2 harness.

E1v2 measures a persistent Ouroboros agent across a SWE-bench Pro task order:

- the task repository lives at `/app` inside each Pro image;
- Ouroboros state and Ouroboros source are carried across tasks through
  `obo-data` and `obo-repo` volumes;
- each solve task is followed by native post-task evolution
  (`OUROBOROS_POST_TASK_EVOLUTION=true`, cadence `every_n:1`);
- patches are captured from `/app` with Method C (`git add -A` then
  `git diff --cached <base_commit>`, with validated junk/binary filters);
- grading remains offline through the official SWE-bench Pro evaluator.

Dangling evolution transactions between tasks are healed two ways, which
compose safely. (1) The current Ouroboros core handles them through boot
reconciliation + supervisor auto-restart. (2) The harness keeps the kit's
bash-level "Option A" heal at task start as a belt-and-braces for agents seeded
from an older core that lacks (1): it mirrors the verified path (marking the
committed transaction restart-verified at the container boundary) with a
`git merge-base --is-ancestor` guard that ABANDONS instead when the commit was
rolled back. With a reconciling core, Option A finds no `active_transaction` and
is a no-op. Without it, Option A prevents a poison-pill that would wedge
`enqueue_evolution_task_if_needed` for every later task (E1v2 → E1).

Files:

- `run_pro.py` - one task in one Pro image.
- `auto_run.py` - sequential orchestrator over `task_order_pro_70.csv`.
- `entrypoint_pro.sh` - in-container solve/evolution/capture flow.
- `prompt_e1v2.txt` - solve prompt.
- `prompt_evolution_steer.txt` - benchmark-only evolution steer.
- `settings_base.json` / `_run_settings.example.json` - non-secret settings
  template and example materialized settings.
- `build_predictions.py` - build predictions from consolidated E1v2 patches.

Bench-only steer caveat: `prompt_evolution_steer.txt` limits churn (at most one
reviewed commit plus one restart per cycle) AND, in this benchmark environment
only, forbids release bookkeeping (no VERSION/CHANGELOG/README/ARCHITECTURE/
pyproject edits, no P9 version-bump rule). The standing evolution steer already
forbids touching those files, so advisory review will routinely flag a
`version_bump` / `forgotten_touchpoints` finding — that is expected here and is
left as advisory. The steer must NOT be "resolved" by hardcoding those findings
to block; the review enforcement mode is owner-controlled (BIBLE P3). This
mirrors the colleague kit and prevents the self-hardening deadlock where every
evolution commit becomes uncommittable under advisory mode.

## Diagnosing SIGKILL / OOM

A worker logged as `crashed with signal 9 — terminal (no retry)` (or a container
exiting 137) is almost always a kernel OOM kill, not a code crash. `run_pro.py`
sets `--memory`/`--memory-swap` to `--mem-limit` (default 8g) so a runaway
allocation OOMs the container cleanly (exit 137, noted in `container.log`)
instead of the host OOM-killer ambiguously killing the worker. The most common
trigger was an unbounded `search_code` whose root resolved to `/` under the
container's `HOME=/`; core now pre-enumerates a policy-gated file list and feeds
it to ripgrep in batches (no giant single argv → no `E2BIG`), caps the scan at a
file-count limit, and skips non-regular files (`/dev`, `/proc`). To
investigate a fresh OOM: check `container.log` for the 137 note, raise
`--mem-limit`, and read the host kernel log (`dmesg`) for the OOM line.
