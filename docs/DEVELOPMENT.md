# DEVELOPMENT.md — Development Principles & Module Guide

## What This File Is

This is Ouroboros's **engineering handbook** — the bridge between philosophy (BIBLE.md) and architecture (ARCHITECTURE.md).

**BIBLE.md** answers *why* and *what matters*.
**ARCHITECTURE.md** describes *what exists right now*.
**DEVELOPMENT.md** answers *how to build* — the concrete principles, patterns, and checklists for writing, modifying, and reviewing code in this project.

## Scope

- **Code style & structure:** naming, file layout, module boundaries, error handling patterns.
- **Module lifecycle:** how to create a new module, what it must include, how it integrates.
- **Review & commit protocol:** what happens before code lands — gates, checks, invariants.
- **Testing standards:** what gets tested, how, minimum expectations.
- **Prompt engineering:** standards for writing and modifying LLM prompts (SYSTEM.md, CONSCIOUSNESS.md, etc.).
- **Integration patterns:** how modules communicate, data flows, shared state.

## What It Is NOT

- Not philosophy — that's BIBLE.md.
- Not an architecture map — that's ARCHITECTURE.md.
- Not a changelog — that's README.md + git log.
- Not aspirational — every rule here must reflect current practice or an immediately enforced standard.

## Relationship to Other Documents

```
BIBLE.md (soul — principles, constraints, identity)
    ↓ informs
DEVELOPMENT.md (hands — how to build, concretely)
    ↓ produces
ARCHITECTURE.md (mirror — what currently exists)
```

Rules in this file must not contradict BIBLE.md.

---

## Naming Convention

### General Rules

- **Language:** All code identifiers, comments, docstrings, and commit messages are in English.
- **Style:** Python PEP 8. Modules and variables — `snake_case`. Classes — `PascalCase`. Constants — `UPPER_SNAKE_CASE`.
- **Self-explanatory names** over abbreviations. A name should tell you what the thing *does*, not just what it *is*. Derived from P6 (Authenticity & Reality Discipline).

### Entity Types

| Entity Type | Purpose | Naming Pattern | Contains Business Logic? | Example |
|-------------|---------|----------------|--------------------------|---------|
| **Gateway** | Thin adapter to an external API. Wraps third-party SDK/HTTP calls into clean Python functions. | `{Platform}Gateway` | No. Pure I/O — translate calls in, translate responses out. | `BrowserGateway` |
| **Service** | Orchestrates a domain concern. May use one or more Gateways, manage state, apply business rules. | `{Domain}Service` | Yes. Coordinates, decides, transforms. | — |
| **Tool** | An LLM-callable function exposed to the agent. Thin wrapper that connects the agent to a Gateway or Service. | `{verb}_{noun}` (snake_case function) | Minimal. Validates input, calls Gateway/Service, formats output. | `repo_read`, `browse_page`, `web_search` |

### Gateway Rules (recommended pattern, not enforced)

When adding a new external API integration, the recommended pattern is a **Gateway** class that isolates transport from business logic. The `ouroboros/gateways/` directory houses external API adapters. As the codebase grows, extract Gateways as needed.

When a Gateway exists, it should follow these guidelines:
- No business logic: no routing, no decisions. Just transport.
- Input/output: takes Python primitives, returns Python primitives.
- Error handling: translates platform-specific errors into consistent return values.
- Stateless where possible.

**Existing Gateways:**
- `ouroboros/gateways/claude_code.py` — Claude Agent SDK gateway. Two paths: `run_edit`
  (edit mode with PreToolUse safety hooks) and `run_readonly` (advisory review, no
  mutating tools). Structured `ClaudeCodeResult` output.

### Relationship Between Entities

```
LLM Agent
    |  calls
Tool (repo_read, web_search, browse_page)
    |  delegates to
Gateway or direct implementation
    |  calls
External API / filesystem / subprocess
```

Not every layer is required for every operation. Simple cases (e.g., `repo_read`) go Tool → filesystem directly.

---

## Module Size & Complexity

Derived from P7 (Minimalism): entire codebase fits in one context window.

- Module target: ~1000 lines. Crossing that line is P7 pressure and should trigger extraction or an explicit justification.
- Module hard gate: 1600 lines for non-grandfathered modules in `tests/test_smoke.py`. Grandfathered (`GRANDFATHERED_OVERSIZED_MODULES` in `ouroboros/review.py`): `llm.py`, `claude_advisory_review.py`, `review_state.py` — split deferred until each surface stabilises.
- Method target: <150 lines. Crossing that line is a decomposition signal, not an automatic failure by itself.
- Method hard gate: 300 lines in `tests/test_smoke.py`.
- Codebase-wide function-count hard gate: enforced by `tests/test_smoke.py` against the value defined in `ouroboros/review.py::MAX_TOTAL_FUNCTIONS` (single source of truth — bump the constant when adding a feature with an explicit comment justifying the increase).
- Function parameters: <8.
- Net complexity growth per cycle approaches zero.
- If a feature is not used in the current cycle — it is premature.

---

## Core Governance Artifacts

`BIBLE.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT.md` are **core governance artifacts**.
They are the constitutional, architectural, and procedural ground truth of the system.

### Invariant: Full availability in reasoning flows

Any flow that requires architectural, constitutional, or procedural reasoning MUST include
these artifacts as **first-class context sections** — not as optional or opportunistic
inclusions via touched-file packs.

Concrete requirements:

| Flow | BIBLE.md | ARCHITECTURE.md | DEVELOPMENT.md |
|------|----------|-----------------|----------------|
| Main task context (`context.py`) | ✅ full | ✅ full | ✅ full |
| Triad review (`tools/review.py`) | ✅ via preamble | ✅ via `_load_architecture_text` | ✅ via `_load_dev_guide_text` |
| ↳ Anti-thrashing (v4.35.1) | — | — | Open obligations loaded from `review_state` via `load_state(drive_root)` + `make_repo_key(repo_dir)`, injected unconditionally into `_build_review_history_section` prompt context. Same mechanism in `scope_review.py::_build_scope_prompt` (best-effort when `drive_root` available). |
| Background consciousness (`consciousness.py`) | ✅ full | ✅ full | — (not yet required) |
| Advisory pre-review (`tools/claude_advisory_review.py`) | ✅ via `_load_doc` | ✅ via `_load_doc` | ✅ via `_load_doc` |
| Scope review (`tools/scope_review.py`) | via full repo pack | via full repo pack | via full repo pack |
| Deep self-review (`deep_self_review.py`) | via full repo pack | via full repo pack | via full repo pack |

### Invariant: No silent truncation

If a core governance artifact cannot fit in the available context budget:
- Do **not** silently omit it or truncate it without a visible marker.
- Either adjust the budget/flow to accommodate it, or emit an explicit warning
  (`⚠️ OMISSION NOTE: ARCHITECTURE.md omitted due to budget constraints`) so the
  operator and the model both know the context is incomplete.
- A reviewer or agent operating without ARCHITECTURE.md MUST NOT be treated as
  operating with full context — findings may be incomplete.

### Invariant: No "only if touched" gate for core artifacts

Core governance artifacts reach review/reasoning flows unconditionally — NOT only
when they appear in `touched_paths`. The `build_touched_file_pack` function is for
_changed_ files; core artifacts are a separate concern and are loaded independently.

### When adding a new reasoning flow

If you add a new flow that reasons about code structure, system architecture, or
engineering standards, you MUST:
1. Explicitly load `ARCHITECTURE.md` (and BIBLE.md if constitutional reasoning applies).
2. Log a warning if the file is missing or unavailable — do not silently skip.
3. Add a test asserting the file is present in the assembled context/prompt.

---

## Review & Commit Protocol

Reviewed commits now have an explicit **two-step gate**:

1. **Advisory freshness gate**: finish all edits, then run `advisory_pre_review`.
   Without a bypass, `repo_commit` / `repo_write_commit` require a fresh matching
   advisory run, no open obligations from earlier blocked rounds, and no open
   commit-readiness debt. Any edit after advisory makes it stale and requires a
   re-run. When debt remains, `review_status` reports `repo_commit_ready=false`
   plus `retry_anchor=commit_readiness_debt` so the next retry starts from the
   repeated root cause rather than one obligation at a time. `skip_advisory_pre_review=True`
   is an **absolute** escape hatch: it short-circuits the entire commit gate
   after writing an audit entry to `events.jsonl`. Open obligations and open
   commit-readiness debt stay visible in `review_status` (`repo_commit_ready`
   stays `false`) but do NOT block the bypassed commit. Use bypass when advisory
   cannot run (provider outage, rate limit) or when the stale signals are known
   to be obsolete; in both cases subsequent `on_successful_commit()` clears
   them automatically.
2. **Unified pre-commit review**: once advisory is fresh, the reviewed commit path
   runs two reviewers in parallel on the exact staged snapshot:
   - **Triad review** (`ouroboros/tools/review.py`): at least 2 reviewer
     models (as configured in `OUROBOROS_REVIEW_MODELS`; ships with 3, hard
     cap `_handle_multi_model_review.MAX_MODELS = 10`) review the staged
     diff against `docs/CHECKLISTS.md`. Quorum requires at least 2 responded
     actors (`_run_unified_review`).
   - **Scope review** (`ouroboros/tools/scope_review.py`): one model reviews
     completeness and cross-module consistency with full-repo context
     (`build_full_repo_pack`).

Both blocking reviewers always run concurrently via `concurrent.futures.ThreadPoolExecutor`
(orchestrated in `ouroboros/tools/parallel_review.py`). The caller receives one
combined verdict with all findings in a single round. Scope review still runs even
when triad blocks, **except** when the fully assembled scope-review prompt exceeds
the model context budget (`_SCOPE_BUDGET_TOKEN_LIMIT`), in which case scope review
is skipped with a non-blocking advisory warning. `docs/CHECKLISTS.md` remains the
single source of truth for review items; do not duplicate or fork checklist policy here.

Preferred workflow for non-trivial edits: choose the right edit tool first —
`str_replace_editor` for one exact replacement, `repo_write` for new files or
intentional full rewrites, and `claude_code_edit` for anything beyond one exact
replacement — then `advisory_pre_review`, then `repo_commit` immediately on the
final diff.

The full pre-commit review checklists live in **`docs/CHECKLISTS.md`** —
the single source of truth (Bible P7: DRY).

This section defines what "DEVELOPMENT.md compliance" means in practice — it is the
detailed expansion of the `development_compliance` item in `docs/CHECKLISTS.md`.

### DEVELOPMENT.md Compliance Checklist

Before every commit, verify the following:

#### Naming Conventions
- [ ] Modules and variables use `snake_case`
- [ ] Classes use `PascalCase`
- [ ] Constants use `UPPER_SNAKE_CASE`
- [ ] Names are self-explanatory

#### Entity Type Rules
- [ ] **Gateway** (if present): contains ONLY transport. No business logic, no routing.
- [ ] **Tool** (`{verb}_{noun}`): thin LLM-callable wrapper. Validates input, formats output.

#### Module Size & Complexity
- [ ] Module stays near one context window (~1000 lines target; 1600 hard gate unless explicitly grandfathered debt)
- [ ] No method exceeds the practical target (150 lines) or the hard gate (300 lines)
- [ ] Total Python function count stays under the current smoke hard gate (consult `ouroboros/review.py::MAX_TOTAL_FUNCTIONS` for the active value; bump with a comment if a feature requires more headroom)
- [ ] No function has more than 8 parameters
- [ ] No gratuitous abstract layers (Bible P7)

#### Structural Rules
- [ ] New Tool? `get_tools()` exports it using the `ToolEntry` pattern from `registry.py`, AND an explicit entry is added to `ouroboros/safety.py::TOOL_POLICY` (`POLICY_SKIP` for trusted built-ins, `POLICY_CHECK` for opaque or outward-facing ones). Without the policy entry the tool falls through to `DEFAULT_POLICY = POLICY_CHECK` and pays a light-model LLM call per invocation, and the `test_tool_policy_covers_all_builtin_tools` invariant will fail.
- [ ] New Gateway (if extracted)? Contains no business logic, only transport.
- [ ] New memory/data files? Should they appear in LLM context (`context.py`)?

#### LLM Call Rules
- [ ] New LLM calls go through the shared `LLMClient` / `llm.py` layer — no ad-hoc HTTP clients or direct provider SDKs outside that layer. **Exception (v5.7.0+):** skill / extension `plugin.py` modules may call providers directly because they have not yet been migrated to a host-mediated `api.invoke_llm(...)` bridge. When that bridge lands, the exception goes away. Runtime callers (anything inside `ouroboros/`) must still use `LLMClient`.

#### Loop / State-Machine Changes
- [ ] Changes to `loop.py` or other task state-machine logic include adversarial tests for malformed output, false-completion prevention, replay/log durability, and failure modes — not just the happy path.
- [ ] Audit/checkpoint rounds must not silently reuse the normal final-answer path unless that invariant is explicitly tested and documented.

#### Cognitive Artifact Integrity
- [ ] Cognitive artifacts (identity.md, scratchpad, task reflections, review outputs, pattern register) must NOT use hardcoded `[:N]` truncation. If content must be shortened, include an explicit omission note (e.g. `⚠️ OMISSION NOTE: truncated at N chars`).
- [ ] `BIBLE.md`, `docs/ARCHITECTURE.md`, and `docs/DEVELOPMENT.md` are **core governance artifacts**. All primary reasoning flows (triad review, consciousness, advisory pre-review, deep review) include them as first-class sections — see the "Core Governance Artifacts" table. If you add a new reasoning flow, it MUST follow this contract, not rely on touched-file inclusions.

---

*This section is the authoritative definition of "DEVELOPMENT.md compliance" referenced in the `development_compliance` item in `docs/CHECKLISTS.md`.*

---

## Platform Abstraction Rule

All platform-specific code **MUST** go through `ouroboros/platform_layer.py`.

### What counts as platform-specific

- Direct use of: `os.kill`, `os.setsid`, `os.killpg`, `os.getpgid`, `signal.SIGKILL`, `signal.SIGTERM`
- Unix-only modules: `fcntl`, `resource`, `grp`, `pwd`
- Windows-only modules: `msvcrt`, `winreg`, `ctypes.windll`
- `subprocess` with platform-conditional flags: `start_new_session`, `creationflags`
- Hardcoded path separators (`/` or `\\`) in filesystem logic (use `pathlib` instead)

### Rules

1. **All platform-specific calls live in `platform_layer.py`** — the rest of the codebase imports cross-platform wrappers from there.
2. **Platform-specific modules are imported inside `platform_layer.py` only**, guarded by `IS_WINDOWS` / `IS_MACOS` / `IS_LINUX` checks.
3. **No top-level imports of Unix-only or Windows-only modules** outside `platform_layer.py`. If you need `fcntl` — you're in the wrong file.
4. **Use `pathlib.Path`** for filesystem paths. Never construct paths with string concatenation using `/` or `\\`.

### Enforcement

- **AST-based test** (`tests/test_platform_guard.py`): scans `.py` files under `ouroboros/`, `supervisor/`, and `server.py` for:
  - Top-level imports of platform-specific modules (`fcntl`, `msvcrt`, `winreg`, `resource`)
  - Direct `os.kill`, `os.killpg`, `os.setsid`, `os.getpgid` attribute access
  - Direct `signal.SIGKILL`, `signal.SIGTERM` attribute access
  
  Not scanned by the AST guard: `launcher.py` (immutable outer shell, intentionally excluded) and subprocess flag patterns (`creationflags`, `start_new_session`). For subprocess isolation, use `subprocess_new_group_kwargs()` and `subprocess_hidden_kwargs()` from `platform_layer.py` — enforced by code review and the `cross_platform` checklist item.
- **Pre-commit review**: checklist item `cross_platform` (#14) catches violations during code review.
- **CI matrix**: tests run on Ubuntu, Windows, and macOS to catch runtime failures.

### Adding new platform-specific code

1. Add the cross-platform wrapper to `platform_layer.py`.
2. Import and use the wrapper in callers.
3. Add platform-conditional tests if behavior differs across OSes.

---

## Design System

Ouroboros uses **glassmorphism** as its visual language. All interactive surfaces follow this pattern:

```css
background: rgba(26, 21, 32, 0.62–0.88);
backdrop-filter: blur(8–16px);
border: 1px solid rgba(255, 255, 255, 0.06–0.12);
```

### Floating overlay transparency (v5.7.0+)

Floating chrome that overlays scrolling content (chat header, sticky tab
strips inside Settings/Dashboard/Skills, files preview gradient) follows ONE
shared formula and never relies on a separate fade-overlay element:

1. The chrome element is `position: absolute` with the appropriate edge
   (`top: 0` for headers, `bottom: 0` for bottom overlays, etc.) and
   covers the whole horizontal axis.
2. Its background is a **single 4-stop linear gradient** that fades from
   the dense brand background at the chrome's anchor edge to fully
   transparent at the opposite edge.
3. `backdrop-filter: blur(10–14px)` is applied on the same element
   (the host always supplies `-webkit-` prefix in lockstep).
4. **A CSS `mask-image` matching the gradient direction fades the blur
   in lockstep**: `mask-image: linear-gradient(0deg, black 0%, black 70%, transparent 100%)`.
   This is the rule that prevents the visible "glass edge" the v5.6.x
   chat dock had — without the mask the blur creates its own hard
   horizontal line at the gradient's transparent stop.
5. The scrollable surface reserves enough top/bottom padding so content is
   reachable outside the overlay's dense zone.

**Chat input dock exception:** the bottom composer intentionally splits the
formula. `#chat-input-area` is a compact absolute bottom overlay with a
darkening gradient only (no wrapper `backdrop-filter`), so message text fades
under the dock without a tall smeared blur band. The active textarea itself
is the frosted surface (`background: rgba(26,21,32,0.55);
backdrop-filter: blur(20px)`). `#chat-messages` reserves bottom padding
through `--chat-input-reserve`, which JS sets from the actual dock height
plus a small buffer; mobile adds safe-area on top of that.
`updateMessagesPadding()`
preserves scroll stickiness only; it must not mutate DOM padding.

Do NOT introduce a separate `.chat-bottom-fade` (or analogous overlay)
layer. A second fade layer compounds the gradient and can produce a visible
"double dim" especially over short messages.

### Navigation rail spacing (v5.7.0+)

The desktop `#nav-rail` uses Material 3 / Apple HIG navigation-rail
spacing norms: `padding: 28px 0 16px; gap: 10px;`. The previous
`12px / 4px` was visibly cramped (the first button hugged the top edge
of the viewport). Bump these values together when adding new nav
buttons; resist tightening them.

On mobile (`@media (max-width: 640px)`) the rail flips to a horizontal
bottom bar with `justify-content: safe center`. The `safe` keyword
keeps the row centered when content fits and gracefully degrades to
flex-start when content overflows on very narrow phones. `min-width:
60px` per `.nav-btn` keeps labels like "Dashboard" from truncating in
space-evenly mode.

The mobile `.scroll-tabs` pattern (settings/dashboard/skills) uses
horizontal-scroll pills with `scrollIntoView({ inline: 'center' })`
on activation so the active pill is always visible. Do not reintroduce
the v5.6.0 drill-down accordion (`settings-subtab-open` /
`settings-mobile-back`) — it traded one tap for two.

### Accent colors

| Role | Value | Usage |
|------|-------|-------|
| Primary | `rgba(201, 53, 69, ...)` = `#c93545` | Nav buttons, chat cards, borders |
| Hover/focus | `rgba(232, 93, 111, ...)` = `#e85d6f` | Focus glow, settings hover |

Use the primary accent for new features. Avoid introducing additional red/crimson shades.

### Border radius scale

| Token | Value | Usage |
|-------|-------|-------|
| `--radius-xs` | `3px` | Micro accents (progress bars) |
| `--radius-sm` | `8px` | Small controls, filter chips |
| `--radius` | `12px` | Inputs, inner cards |
| `--radius-lg` | `16px` | Nav buttons, chat/live cards |
| `--radius-xl` | `20px` | Logo images, large media |
| *(no token)* | `18px` | Section cards (settings, form panels) |
| *(no token)* | `24px` | Modal/wizard shells, chat input |

Use CSS variables where possible. Do not introduce new hardcoded radius values.
When a new radius value is needed, add it to `:root` in `web/style.css` first.

### Interactive states

```css
hover:  transform: scale(1.02–1.04) + border-color +1 step brightness
active: background rgba(201,53,69, 0.12) + crimson glow
focus:  border-color rgba(232,93,111,0.4) + box-shadow 0 0 0 3px rgba(201,53,69,0.10)
```

### Button conventions

All normal application buttons use the shared `.btn` base class plus exactly
one semantic variant:

| Variant | Purpose |
|---------|---------|
| `.btn-primary` | Primary action in the current surface: enable, install, update, start |
| `.btn-secondary` | Neutral secondary action next to a primary action: reload, cancel, install runtime |
| `.btn-default` | Low-emphasis utility action: refresh, details, open related view |
| `.btn-ghost` | Very quiet action on an already-strong surface |
| `.btn-save` | Persist settings or budget changes |
| `.btn-danger` | Destructive or emergency action |

Size modifiers are `.btn-xs`, `.btn-sm`, `.btn-md`, and `.btn-lg`. Omit a size
modifier for the default medium size. Do not combine semantic variants (for
example, `.btn-default.btn-primary` is invalid), and do not invent one-off
button schemes in feature modules. Onboarding and modal buttons use the same
`.btn` variants as the main SPA.

Buttons are horizontally centered by default. If a control intentionally uses a
menu-row layout, use a named menu-item class (for example `.skills-menu-item`)
rather than overloading `.btn`.

### "Working" phase color

Use **crimson** (`rgba(248, 130, 140, ...)`) for active/working states everywhere — not blue.
The Logs page phase badges now match Chat live card colors.

### No inline styles in JS

JS modules that generate HTML must use CSS class names, not `style=""` attributes.
This is enforced by reviewer policy — `.style.*` assignments on DOM elements (e.g.
`element.style.display`, `element.style.color`) will produce a REVIEW_BLOCKED finding.
Existing classes (`.stat-card`, `.page-header`, `.about-*`, `.costs-*`) cover common layouts.
Add new classes to `web/style.css` when needed.
Before staging any `web/modules/*.js` file: `grep -n "\.style\." web/modules/*.js`
and fix any hits.

### Declarative widget UI

Extension widgets should prefer host-owned declarative render schemas.
`web/modules/widgets.js` is the single host for `register_ui_tab`
declarations: legacy `iframe` remains sandboxed with no relaxed tokens,
legacy `inline_card` remains weather-shaped, and `kind: "declarative"` /
`schema_version: 1` covers forms, actions, markdown, JSON, key/value
summaries, tables, progress, files, galleries, image/audio/video media, and
v5.7.0 map/calendar/kanban components. New common widget capabilities should
extend that declarative schema and its tests.

v5.7.0 adds one deliberate exception for rare custom UI: `kind: "module"`
loads reviewed skill-provided `widget.js` into a sandboxed `srcdoc` iframe
(`sandbox="allow-scripts"`, **no** `allow-same-origin`). The parent host
fetches the reviewed JS from `/api/extensions/<skill>/module/<entry>` and
injects a constrained `fetch` bridge that only proxies
`/api/extensions/<skill>/...` routes. This is not same-origin SPA execution;
the module cannot access app cookies or `localStorage`.

Rules for widget changes:

- Escape every untrusted string with `escapeHtml`; use DOMPurify only for
  markdown blocks.
- Media sources must be extension routes under `/api/extensions/<skill>/...`
  or explicitly safe `data:` URLs for image/audio/video MIME types.
- Long-running user actions (image/music/research generation) must use the
  declarative async job contract: start route returns `job_id`, status route
  returns `queued|running|done|error`, and the widget host resumes polling by
  `job_id` after tab switches. Do not implement long generation as a single
  foreground HTTP request that can be lost when the widget remounts.
- Download controls must use the host download helper (`data-widget-download-url`
  / desktop bridge / fetch-blob fallback). Raw in-app navigation links are not
  acceptable for downloads because desktop WebView may replace the Ouroboros UI
  with the media file.
- Do not load arbitrary JS modules from skill directories into the SPA origin.
  `kind: "module"` is allowed only through the sandboxed iframe + parent fetch
  bridge above, and must be covered by the `widget_module_safety` review item.
- Add/update `tests/test_widgets_ui_static.py` for every new component kind or
  media policy.

---

## Build & CI

### GitHub Actions: secrets in step-level `if:` conditions

GitHub Actions **rejects** `secrets.*` references inside step-level `if:`
expressions with `Unrecognized named-value: 'secrets'`. The workflow file
fails to parse and the job never runs. Step-level `env:` blocks **are also
not visible to that step's own `if:`** — only job-level `env:` is.

When a step needs to gate on whether a secret is configured, **map the
secret into the build job's `env:` block, then reference `env.*` in the
step `if:`**. The step itself can then either use the env var directly
(it inherits from the job) or assume it is present.

```yaml
jobs:
  build:
    runs-on: macos-latest
    env:
      # job-level: visible to step-level `if:` via env.*
      BUILD_CERTIFICATE_BASE64: ${{ secrets.BUILD_CERTIFICATE_BASE64 }}
      P12_PASSWORD: ${{ secrets.P12_PASSWORD }}
    steps:
      - name: Import Apple signing certificate
        # ✅ env.* — visible inside step-level if
        if: env.BUILD_CERTIFICATE_BASE64 != '' && env.P12_PASSWORD != ''
        run: |
          echo "${BUILD_CERTIFICATE_BASE64}" | base64 -d > cert.p12
          security import cert.p12 -P "${P12_PASSWORD}" ...
      - name: Cleanup keychain
        if: always() && env.BUILD_CERTIFICATE_BASE64 != ''
        run: security delete-keychain ...
```

```yaml
# ❌ WRONG — workflow fails to parse
- name: Bad
  if: secrets.BUILD_CERTIFICATE_BASE64 != ''   # parse error
  env:                                          # not visible to this step's if:
    P12_PASSWORD: ${{ secrets.P12_PASSWORD }}
```

This pattern is enforced by `tests/test_build_scripts.py::TestMacOSSigning::
test_ci_uses_env_context_for_condition`, which parses every `if:` block in
`.github/workflows/ci.yml` (including multi-line continuations) and asserts
no occurrence of `secrets.` ever appears inside one.

### Apple signing & notarization (macOS Build job)

When `BUILD_CERTIFICATE_BASE64`, `P12_PASSWORD`, `KEYCHAIN_PASSWORD`, and
`APPLE_TEAM_ID` are configured as repository secrets, the macOS build job
imports the Developer ID certificate into a temporary keychain and runs
`bash build.sh` — `build.sh` then signs the `.app` and the `.dmg` using
the env-overridable `SIGN_IDENTITY`. Each Apple secret is mapped at the
build job's `env:` block with a `${{ matrix.os == 'macos-latest' && secrets.X || '' }}`
guard so the Apple credentials reach the macOS matrix shard only; Linux
and Windows sibling shards (running `build_linux.sh` / `build_windows.ps1`,
neither of which needs Apple creds) receive empty strings. When `APPLE_ID` and
`APPLE_APP_SPECIFIC_PASSWORD` are also present, `build.sh` runs
`xcrun notarytool submit ... --wait` followed by `xcrun stapler staple` to
attach the notarization ticket; otherwise the entire notarization block is
skipped and the DMG ships **signed but not notarized** (users still need
right-click → **Open** on first launch). The stapler call is wrapped in
its own guard so a transient stapler failure after a successful notarytool
submission becomes a soft warning rather than a hard build failure (the
DMG is genuinely notarized — Gatekeeper just fetches the ticket online on
first launch instead of from the embedded staple). The `notarytool submit`
call is wrapped the same way: an Apple-side outage / wrong-credential typo
prints a `WARNING` and lets the DMG ship signed-but-not-notarized, instead
of aborting the build under `set -e` and silently dropping the macOS
artifact from the release. A single `NOTARIZE_OUTCOME` enum (`success` /
`staple_failed` / `submit_failed` / `unconfigured`) drives the build's
final summary line so the WARN message and the summary always agree on
the actual artifact state, plus a defensive `*)` arm so any future enum
drift is loud. The `Cleanup keychain`
step runs with `if: always() && matrix.os == 'macos-latest' && env.BUILD_CERTIFICATE_BASE64 != ''`
— `always()` ensures cleanup fires on build failures too, the `matrix.os`
gate keeps the bash-only `security` invocation off Linux/Windows shards,
and the env guard skips when no keychain was created (no signing secrets).
Signing material never persists across runs.
