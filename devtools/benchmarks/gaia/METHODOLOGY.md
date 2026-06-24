# GAIA Methodology Notes

GAIA is an answer benchmark, not a code-patch benchmark. This adapter therefore
does not rewrite the scorer or normalize Ouroboros's core `final_answer`.

- **Official score is authoritative.** Use `inspect_evals/gaia` and its
  `gaia_scorer`. `score_gaia.py` may print a local lenient-normalized diagnostic
  to quantify typographic brittleness, but that number is never the headline.
- **Fixed-model Track A.** `settings_base.json` is the committed base template;
  `run_gaia.py` renders a per-run settings file that pins Ouroboros runtime,
  review, and vision model slots to the solve model and disables post-task
  evolution. The default validation model is `google/gemini-2.5-pro`; Sonnet 4.5
  is documented as the later headline comparator against HAL Generalist, not run
  by default here. The adapter disables Ouroboros's `web_search` tool by name so
  a separate OpenAI Responses web-search model cannot contaminate fixed-model
  measurements.
- **Acceptance review is required.** GAIA Track A measures the full Ouroboros
  scaffold chosen for this sprint: `OUROBOROS_TASK_REVIEW_MODE=required`, empty
  memory, one top-level worker, and no post-task evolution.
- **Runtime mode is light by design.** The accepted plan originally sketched
  `pro`, but review corrected this to `light`: GAIA is an answer benchmark, not
  a self-repo modification task, so the adapter must not give benchmark prompts
  protected Ouroboros repo/control-plane write authority. Light mode still permits
  task/artifact/user-file deliverables needed for answer work while keeping the
  system body protected.
- **Structured extraction.** The solver invokes `ouroboros run
  --result-json-out <sample>/result.json` and reads `final_answer` first, falling
  back to `result` only when the structured field is absent. It does not scrape
  the last stdout line.
- **No best-of-N as pass@1.** One attempt per task is pass@1. Multi-seed or
  pass@k runs must be labelled separately.
- **Historical raw material.** `dragunov_traces/gaia_repro/` remains outside the
  repo as forensic input; useful settings and wrapper ideas are copied/adapted
  here rather than imported as live code.
