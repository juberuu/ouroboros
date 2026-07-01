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
  by default here. GAIA permits web browsing; the fixed-model purity boundary is
  whether a *second reasoning model* enters the scaffold. The `strict_ddgs`
  profile keeps Ouroboros's `web_search` tool enabled but pins
  `OUROBOROS_WEBSEARCH_BACKEND=ddgs`, a pure-retrieval backend with no provider
  key and no second LLM. The `web_off_baseline` profile disables `web_search`
  for apples-to-apples comparison with older web-off runs. The
  `quality_openrouter_web` profile injects OpenRouter's server-side web-search
  tool into the main solve-model call; it is still single-model reasoning when
  the solve route supports it, but it is a disclosed scaffold change.
- **Acceptance review is required.** GAIA Track A measures the full Ouroboros
  scaffold chosen for this sprint: `OUROBOROS_TASK_REVIEW_MODE=required`, empty
  memory, one top-level worker, and no post-task evolution. Raising
  `OUROBOROS_MAX_WORKERS` above 1 is a disclosed scaffold change for same-model
  decomposition; it is not pass@1 if used as independent attempts with selection.
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
- **Answer-format prompt (adapter only).** The solver appends GAIA's standard
  format instruction (a number / as few words as possible / no units unless asked;
  the `FINAL ANSWER:` template), shared as one SSOT constant
  (`inspect_solver.GAIA_FORMAT_INSTRUCTION`) across the Ouroboros/codex/Claude
  solvers. This is GAIA's own intended format/prefix prompt: it shapes the AGENT'S
  OWN answer using only the public task contract, never the gold answer. GAIA's
  quasi-exact-match scorer normalizes whitespace/case/punctuation and selected
  numeric punctuation, but NOT articles, units, scale, or wording, so the format
  prompt is the methodology-sanctioned alignment surface.
  Ouroboros's core `final_answer` and `extract_final_answer` are untouched (a core
  answer-normalizer would harm ordinary users, where units/wording are often part
  of the requested answer).
- **Agent-visible deadline (honesty: visible == real budget − reserve).** GAIA
  imposes no per-task wall-clock limit — the sample timeout is an OPERATOR budget.
  The solver passes `--timeout = GAIA_SAMPLE_TIMEOUT_SEC − reserve` (reserve = 10%,
  capped at 240s) so Ouroboros's existing deadline-awareness (50/25/10% milestones
  + a save-at-10% nudge, `loop.py`) activates and the agent converges to a saved
  answer instead of being killed mid-thought. The visible deadline is STRICTLY
  tighter than the outer hard-kill backstop (`subprocess.run(timeout=…)`), so the
  agent is never told a deadline it is killed before reaching. The deadline conveys
  only time, no answer content. Disclosed here because GAIA is scaffold-sensitive.
- **Attachment access (general runtime capability).** GAIA task files are passed to
  `ouroboros run` via `--attach`; the runtime stages every attachment into the
  task-readable `artifact_store/attachments/` and surfaces a ready-to-read manifest
  (plus native image blocks for images). When Inspect exposes real file paths, the
  adapter passes them directly with `--attach`; when a GAIA prompt still names a
  legacy `/shared_files/...` path and Inspect's TaskState is empty, the adapter
  resolves it under the operator-supplied `GAIA_SHARED_FILES_ROOT` and rewrites the
  prompt toward the attachment manifest. This keeps the runtime prompt consistent
  with the actual sandbox instead of asking the agent to hunt the host filesystem.
- **No best-of-N as pass@1.** One attempt per task is pass@1. Multi-seed or
  pass@k runs must be labelled separately.
- **Historical raw material.** `dragunov_traces/gaia_repro/` remains outside the
  repo as forensic input; useful settings and wrapper ideas are copied/adapted
  here rather than imported as live code.
