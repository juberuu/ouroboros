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
  memory, and no post-task evolution. Since v6.55.0 the default worker pool is
  `OUROBOROS_MAX_WORKERS=4` — a DISCLOSED scaffold parameter (recorded per run as
  `worker_scaffold_disclosure` in the manifest). The workers are same-model
  subagent slots for decomposition WITHIN one task; they are never independent
  attempts with selection, so the run stays pass@1. Pass `--max-workers 1` for a
  strict-baseline ablation (the pre-v6.55.0 default, which starved subagent
  decomposition).
- **Safety mode is light in bench templates (v6.55.0).** The solver runs against
  a disposable rendered settings/data root; the LLM safety pass added cost and
  latency without protecting anything the deterministic guards don't cover in
  this context, so bench templates pin `OUROBOROS_SAFETY_MODE=light` (LLM check
  retained for integration tools only; deterministic guards unchanged). User
  defaults are untouched. Note the asymmetry with runtime mode: GAIA stays
  `light` runtime BECAUSE it runs without workspace isolation against a live
  repo — safety-mode light does not weaken that boundary.
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

## Answer-leakage audit protocol (publishable web-on runs)

GAIA's validation answers are public on HuggingFace, so a web-capable agent can
in principle look them up instead of solving the task (UC Berkeley RDI reported
~98% on GAIA by pure answer-lookup, 2026; HAL — Holistic Agent Leaderboard,
arXiv 2510.11977 — surfaces this class via LLM-judge inspection of agent logs,
e.g. "searching for the benchmark on HuggingFace instead of solving"). Our
publishable rows deliberately run WITH web access, because the comparison targets
(OpenAI Codex, Claude Code) are themselves web-using harnesses — a web-off
Ouroboros row would be an unfair handicap, not a fairer measurement. Network
egress is therefore NOT isolated; instead honesty is established **post-hoc by
auditing every trace**, which is the HAL-sanctioned alternative to sandboxing.

`audit_leakage.py` implements the audit (diagnostic only — it never changes a
score):

- **Layer 1 — deterministic.** Every web/browser/fetch/shell tool call is scanned.
  A sample is flagged (STRONG signal, "the agent acted on the answer source")
  when it (a) *requested* a URL whose host/path is the GAIA answer source
  (`huggingface.co`/`hf.co`/`datasets-server.huggingface.co`, or a path naming
  the GAIA dataset file), (b) ran a `web_search` query hunting the benchmark
  itself (`gaia (benchmark|dataset|answers|leaderboard)`, or `huggingface … gaia`),
  or (c) *retrieved* the distinctive gold answer verbatim. Leak URLs that merely
  appear in returned search results (not requested by the agent) are recorded as
  a WEAK `result_leak_refs` signal for the judge, not a hard flag — the bare token
  "gaia"/"GAIA" occurs constantly in legitimate content (the ESA Gaia telescope,
  people/works named Gaia) and must never be matched against raw page text.
- **Layer 2 — LLM judge (HAL-style).** A light model scores each sample's web
  activity against a "benchmark answer search / cheating" rubric. Advisory,
  post-hoc, applied identically to every harness including the pre-existing Codex
  row.

**Per-harness audit coverage (disclose this asymmetry):** Ouroboros rows expose
the full tool trace (`ouroboros_data/**/tools.jsonl`); Codex rows expose tool
calls inside the inspect log messages. Claude Code rows run `claude -p
--output-format json`, which returns only the final result — the CLI's own
intermediate WebSearch/WebFetch calls are NOT captured in a parseable trace, so
the Claude Code row's deterministic layer is limited to the final transcript plus
the disclosed fact that Claude Code's allowed tools include `WebSearch`/`WebFetch`;
its leakage audit relies on the LLM judge over that transcript. A fuller Claude
Code audit would require `--output-format stream-json` capture (not changed
mid-experiment to keep the measured harness stable).

## Hermes baseline (cost-reduced k=1)

The Hermes-agent baseline (NousResearch) is run at reduced sampling for cost:
GAIA at pass@1 like every other row, and Terminal-Bench 2.1 at **k=1** (not the
leaderboard-valid k=5). This is a deliberate budget choice — Hermes is included
as an expected-low reference baseline, not a leaderboard-comparable number. Any
Hermes TB2.1 result is stamped `local_low_k` and must NOT be compared directly to
the k=5 rows; disclose the k asymmetry wherever the number appears.
