# Contributing to Ouroboros

Thanks for thinking about contributing. Human contributions are absolutely
welcome — Ouroboros lands changes from both its own self-modification loop
and from external contributors, and the same review machinery applies to
both. See PRs [#46](https://github.com/joi-lab/ouroboros-desktop/pull/46),
[#48](https://github.com/joi-lab/ouroboros-desktop/pull/48), and
[#51](https://github.com/joi-lab/ouroboros-desktop/pull/51) for examples
of merged human-authored PRs.

This guide is intentionally short. The substantive project rules live in
four canonical documents — read them in order; this file just routes you
to them.

| Document | What it answers |
|----------|-----------------|
| [`BIBLE.md`](BIBLE.md) | *Why* the project exists and the 13 constitutional principles (P0–P12) that win all design conflicts. |
| [`README.md`](README.md) | What Ouroboros is, how to install, how to run from source, what the data layout looks like. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | *What exists today* — every module, page, endpoint, and data flow. |
| [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) | *How to build, concretely* — naming, module/method size budgets, platform layer, review protocol. |
| [`docs/CHECKLISTS.md`](docs/CHECKLISTS.md) | The single source of truth for all pre-commit review checklists. |

Per principle **P7 (Minimalism / SSOT / DRY)**, nothing in this file
duplicates those documents. When in doubt, edit the canonical doc, not
this one.

---

## TL;DR

1. Open or pick up an issue. ([Good first
   issues](#finding-something-to-work-on))
2. Fork, branch as `<type>/<short-slug>` (`fix/...`, `feat/...`,
   `docs/...`).
3. Make the smallest change that solves the problem.
4. Run `pytest -q` locally; keep the change inside the module-size
   budgets documented in `docs/DEVELOPMENT.md`.
5. Open a PR against `main` using [Conventional
   Commits](https://www.conventionalcommits.org/) in the title (e.g.
   `fix(tools): …`).
6. Reference the issue with `Closes #N` in the PR body.

---

## What makes this project unusual

A few things that surprise first-time contributors:

- **Self-modifying agent.** Ouroboros writes its own code through a
  reviewed `repo_commit` pipeline (advisory pre-review → triad review →
  scope review). That pipeline lives inside the runtime and is fully
  described in `docs/DEVELOPMENT.md` §"Review & Commit Protocol". It
  runs when the agent commits to its own checkout. **It does not run on
  your PR** — your PR goes through ordinary human review plus CI.
- **Constitution-first.** A change that is technically clean but
  contradicts a `BIBLE.md` principle will be rejected. The constitution
  wins.
- **Size budgets are enforced by smoke gates.** Modules target ~1000
  lines (1600 hard gate), methods target <150 lines (300 hard gate),
  and the total Python function count is gated by
  `ouroboros/review.py::MAX_TOTAL_FUNCTIONS` (currently 2000). Read the
  "Module Size & Complexity" section of `docs/DEVELOPMENT.md` before
  adding a new module or splitting an existing one.
- **No ad-hoc HTTP clients or provider SDKs.** Runtime LLM calls go
  through `ouroboros/llm.py::LLMClient`. The skill `plugin.py`
  exception is explicitly documented as transitional.
- **No platform-specific code outside `ouroboros/platform_layer.py`.**
  An AST-based test (`tests/test_platform_guard.py`) enforces it.

---

## Reporting bugs and asking for features

Open a [GitHub issue](https://github.com/joi-lab/ouroboros-desktop/issues).
A few habits that get issues triaged quickly — the existing Cloud.ru
series ([#39](https://github.com/joi-lab/ouroboros-desktop/issues/39)
through
[#45](https://github.com/joi-lab/ouroboros-desktop/issues/45)) is a
good reference shape:

- **Context** — Ouroboros version, OS, the model and provider you
  configured, how you launched the app (desktop / `python server.py` /
  Docker).
- **Steps to reproduce** — concrete prompt, tool call, or UI sequence.
- **Actual result** — exact error text or the smallest log excerpt
  that shows the failure.
- **Expected result** — what you thought should have happened.
- **Impact** — what this blocks in practice.
- **Suggested fix** (optional) — even a "could be A or B" helps.

Security-relevant findings (sandbox bypass, write outside the policy
boundary, provider-key exfiltration, etc.) should be reported via a
non-public channel where possible — see the maintainers listed at the
bottom of `README.md`.

---

## Finding something to work on

Issues labelled
[`good first issue`](https://github.com/joi-lab/ouroboros-desktop/labels/good%20first%20issue)
when present. If none are labelled at the moment, the following are
naturally well-scoped starter targets:

- **Bug fixes** for already-filed issues. Reproduce first, propose the
  smallest possible fix, add a regression test.
- **`docs/DEVELOPMENT.md`-advertised debts** — splits for grandfathered
  oversize modules (`llm.py`, `review_state.py`, `git.py`, etc.) are
  explicitly invited.
- **Tool descriptions and error messages.** Small clarity improvements
  to agent-facing strings have outsized impact on agent reliability and
  are easy to validate.
- **Cross-platform fixes.** CI runs on Ubuntu, Windows, and macOS;
  Windows-only or Linux-only regressions are common entry points.

If you're unsure whether a change is in scope, open a draft issue
first — it's cheaper than a rejected PR.

---

## Setting up a dev environment

Follow the **Run from Source** section in `README.md`. The short
version:

```bash
git clone https://github.com/joi-lab/ouroboros-desktop.git
cd ouroboros-desktop
pip install -r requirements.txt
pytest -q
```

`pytest` defaults exclude environment-heavy lanes (`integration`,
`browser`, `ui_browser`, `ui_browser_docker`, `portable_detail`) — see
the "Pytest marker lanes" section of `docs/DEVELOPMENT.md`. CI opts
into them explicitly. You should not need any provider API keys to
make most local tests green.

---

## Making a change

1. **Branch.** Branch off `main`. Naming: `<type>/<short-slug>`, for
   example `fix/issue-40-cwd-observability` or `docs/add-contributing-guide`.
2. **Read the relevant section of `docs/DEVELOPMENT.md`** before
   touching:
   - `loop.py` or other state-machine logic
   - `safety.py::TOOL_POLICY` or `runtime_mode_policy.py`
   - anything under `ouroboros/tools/` (new tool? must have an explicit
     `TOOL_POLICY` entry)
   - platform-specific code (must route through `platform_layer.py`)
   - cognitive artifacts (`identity.md`, scratchpad, review outputs,
     pattern register) — no hardcoded `[:N]` truncation; use an
     explicit omission note
3. **Keep the diff small.** Prefer one logical change per PR. If your
   change naturally splits into "observability" + "behaviour", open
   two PRs.
4. **Add a test.** Bug fixes need a regression test that fails on
   `main` and passes on your branch. New behaviour needs coverage of
   both the happy and adversarial paths (see the "Loop / state-machine
   changes" item in `docs/DEVELOPMENT.md`).
5. **Run the smoke gates.** `pytest tests/test_smoke.py` catches
   module-size, method-size, and function-count regressions before CI
   does.
6. **Commit with [Conventional
   Commits](https://www.conventionalcommits.org/)**:
   `fix(tools): …`, `feat(skills): …`, `docs: …`, `refactor(git): …`.
   Maintainer release commits additionally carry a `vX.Y.Z:` prefix —
   contributor commits should NOT.

---

## Opening a PR

- Open against `joi-lab/ouroboros-desktop:main` from your fork branch.
- **Title:** Conventional Commits, mirroring the most relevant commit.
- **Body:**
  - One short paragraph explaining the *why* (not the *what* — the
    diff shows that).
  - `Closes #N` if it resolves an issue (GitHub will auto-link).
  - A short **Test plan** section listing the test files / commands
    you ran.
  - If your change affects agent-facing output strings, tool
    descriptions, or any contract documented in `docs/ARCHITECTURE.md`,
    say so up front — reviewers will look at the downstream parsers.
- Mark as **Draft** if you want early feedback on a half-finished
  change.

### CI expectations

`.github/workflows/ci.yml` defines four tiers (full details in the
workflow header):

- **Tier 1 (Quick)** — Ubuntu-only on `ouroboros` branch pushes;
  small/code-only paths.
- **Tier 2 (Full)** — Full Ubuntu/Windows/macOS matrix on
  `ouroboros-stable`, manual, or tag pushes.
- **Tier 2.5 (Integration)** — Real-provider tests; runs when the
  corresponding API key secrets are configured.
- **Tier 3 (Build + Release)** — PyInstaller build + GitHub Release on
  `v*` tags.

PRs from forks generally trigger Tier 1 / Tier 2 (no provider
secrets). If an integration test fails for a reason that obviously
depends on a missing secret, say so in the PR — maintainers can re-run
with secrets when needed.

---

## Code review

- Human review by a maintainer is the primary gate for external PRs.
  Expect comments on naming, size, test coverage, and constitutional
  alignment.
- The agent's `repo_commit` review machinery (advisory + triad + scope)
  does **not** run on PRs — it runs only inside the agent's own
  self-modification loop. You don't need to invoke it, but reading
  `docs/CHECKLISTS.md` once is a good way to see what reviewers care
  about.
- If a reviewer asks for a substantive change, push a follow-up commit
  rather than force-pushing — it preserves review history. Squash on
  merge is the maintainer's call.
- Be patient. The project releases frequently but reviews come from
  humans on real schedules.

---

## Style and conventions

All conventions live in `docs/DEVELOPMENT.md`. The short shape:

- **Language:** all code identifiers, comments, docstrings, and commit
  messages in English.
- **Python:** PEP 8, `snake_case` modules and variables, `PascalCase`
  classes, `UPPER_SNAKE_CASE` constants.
- **Tools:** `{verb}_{noun}` (`repo_read`, `web_search`,
  `browse_page`).
- **No `style=""` in JS** that generates HTML. Use CSS classes — this
  is enforced by reviewer policy.
- **No inline truncation of cognitive artifacts.** If content must be
  shortened, emit a visible omission note.

---

## License

Ouroboros is released under the [MIT License](LICENSE). By submitting
a contribution you agree that it can be released under the same
license. No CLA is required.

---

## Getting in touch

For non-issue questions, contact the maintainers listed at the end of
`README.md`.
