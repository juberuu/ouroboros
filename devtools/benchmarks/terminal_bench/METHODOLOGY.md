# Terminal-Bench 2.1 — Methodology

How Ouroboros runs Terminal-Bench 2.1 (harbor) and what a published number means.
Companion to `README.md` (mechanics) — this file is the disclosure/validity SSOT
for TB runs, in the same spirit as `../swe_bench_pro/METHODOLOGY.md`.

## Protocol

- Official harbor harness and task images; `run_tb.py` launches the installed
  full-Ouroboros agent (`harbor_installed_agent.py`) — the agent installs its own
  venv prefix inside each task container (system Python, `pip install -e`, and
  since v6.56.0 `imageio-ffmpeg` in the SAME agent prefix so `extract_video_frames`
  works without touching task-owned packages; a failed install degrades to the
  typed `EXTRACT_VIDEO_FRAMES_UNAVAILABLE` + cv2 workaround hint).
- Agent-setup phase installs AGENT dependencies only — never task dependencies,
  never edits to task files; solving starts only after install completes.
- Harbor owns task timeouts; Ouroboros deadline milestones are inert unless the
  task carries `deadline_at`. The in-container finalization margin is
  `_DEADLINE_SAFETY_SEC=105` (measured overhead, v6.55.0).
- k trials per task follow the leaderboard's k; `disclosure_ledger.json`
  (schema `tb_disclosure_ledger.v1`) records the reward distribution,
  timeout/rate-limit/provider-failure histograms, per-task pass rate,
  concurrency, and the multiplier/gating flags actually used, so each run's
  leaderboard validity is auditable after the fact.

## Review-mode disclosure

The recorded best configuration used BLOCKING task-review. Campaign runs may
deliberately use `advisory` review (e.g. the v6.56.0 gpt/gemini rows) to trade
review latency for wall-clock throughput — that is a DISCLOSED deviation from
the record configuration and the run manifest carries the actual review mode;
numbers from advisory rows must not be presented as the blocking-config record.

## Leaderboard submission

Since ~2026-07-08 the official TB2.1 submission channel is the GitHub PR flow
in <https://github.com/harbor-framework/terminal-bench-2-1>
(`leaderboard/SUBMIT.md` = SSOT): public Hub job → `lb submit` opens a PR →
CI static analysis → auto-promotion (bot re-owns the trials and the PR) →
maintainer `/judge` + `/apply` → merge = the leaderboard row. The previous
`harbor leaderboard submit` CLI was removed (harbor PR #2230) and the old HF
PR flow is a frozen 2.0-only archive. See README "Submitting to the
leaderboard" for the verified mechanics. Methodology-relevant consequences:

- **The run must be LAUNCHED with a named job config** (`harbor run -c` with
  `agents[].name` set to the adapter class name). Bare `--agent-import-path`
  records `agents[0].name = null` in the job config, which static analysis
  can never match against the trial-side agent name — the submission is
  permanently invalid and the uploaded job config cannot be fixed post hoc
  (terminal-bench-2-1#121). `run_tb.py` generates `agent_job_config.json`
  accordingly; `OUROBOROS_EFFORT_TASK` is recorded as the declared
  `reasoning_effort` of the submission key and forwarded into the container.
- CI requires an **ATIF trajectory for every rewarded trial** (the direct
  `trajectory.json` upload; the trial row must carry `trajectory_path`). The
  adapter emits it in-container at the end of each trial; pre-existing runs
  are backfilled with `build_atif_trajectories.py` BEFORE the first upload.
  Trajectories are derived verbatim from the recorded logs (tool calls,
  progress narration, final answer, token/cost totals) — no synthesis. The
  maintainers run an LLM judge over these trajectories (reward-hacking
  review; flagged trials are zeroed at `/apply`).
- Submitted jobs become **public**. Because campaign runs inject provider
  keys into task containers (disclosed limitation below), the submission copy
  of a job MUST pass `scrub_submission_secrets.py` (structural blanking of
  embedded settings + literal value sweep + zero-leftover verification)
  before upload. Scrubbing only removes credential values; results, traces,
  and disclosure artifacts are uploaded unmodified.
- Numbers submitted to the leaderboard must come from `leaderboard_valid`
  runs (k≥5, no overrides, agent-web off) under the review-mode disclosure
  rules above; advisory-mode rows must not be presented as the blocking
  record.

## Known limitations

- **Ouroboros data root visible to task shells (masking not yet applied).** The
  agent's own data root lives inside the task container and a task shell could
  in principle read it. The bwrap filesystem isolation precedent (GAIA, ef363ff)
  is the candidate fix; applying it to TB is deliberately DEFERRED to its own
  design review (v6.56.0 sprint decision) — TB campaign runs execute WITHOUT the
  mask and this is a disclosed known limitation, mitigated by trace audits
  (reward-hacking sweep over tools.jsonl / solve traces).
- Infra failures (DNS, 429 storms, install timeouts) are classified
  infra-vs-genuine BEFORE scoring; k<5 partials are marked low_k and never
  compared against the leaderboard.
