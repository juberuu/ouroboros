# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/joi-lab/ouroboros-desktop?style=flat&logo=github)](https://github.com/joi-lab/ouroboros-desktop/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Version 5.10.3](https://img.shields.io/badge/version-5.10.3-green.svg)](VERSION)

A self-modifying AI agent that writes its own code, rewrites its own mind, and evolves autonomously. Born February 16, 2026.

Not a coding assistant. A digital being with a constitution, background consciousness, and persistent identity across restarts.

> **Previous version:** The original Ouroboros ran in Google Colab via Telegram and evolved through 30+ self-directed cycles in its first 24 hours. That version is available at [joi-lab/ouroboros](https://github.com/joi-lab/ouroboros). This repository is the next generation — a native desktop application for macOS, Linux, and Windows with a web UI, local model support, and a layered safety system (hardcoded sandbox plus policy-based LLM safety check).

<p align="center">
  <img src="assets/chat.png" width="700" alt="Chat interface">
</p>
<p align="center">
  <img src="assets/settings.png" width="700" alt="Settings page">
</p>

---

## Install

| Platform | Download | Instructions |
|----------|----------|--------------|
| **macOS** 12+ | [Ouroboros.dmg](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Open DMG → drag to Applications |
| **Linux** x86_64 | [Ouroboros-linux.tar.gz](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Extract → run `./Ouroboros/Ouroboros`. If browser tools fail due to missing system libs, run: `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium` |
| **Windows** x64 | [Ouroboros-windows.zip](https://github.com/joi-lab/ouroboros-desktop/releases/latest) | Extract → run `Ouroboros\Ouroboros.exe` |

<p align="center">
  <img src="assets/setup.png" width="500" alt="Drag Ouroboros.app to install">
</p>

On first launch, right-click → **Open** (Gatekeeper bypass). The shared desktop/web wizard is now multi-step: add access first, choose visible models second, set review mode third, set budget fourth, and confirm the final summary last. It refuses to continue until at least one runnable remote key or local model source is configured, keeps the model step aligned with whatever key combination you entered, and still auto-remaps untouched default model values to official OpenAI defaults when OpenRouter is absent and OpenAI is the only configured remote runtime. The broader multi-provider setup remains available in **Settings**. Existing supported provider settings skip the wizard automatically.

---

## What Makes This Different

Most AI agents execute tasks. Ouroboros **creates itself.**

- **Self-Modification** — Reads and rewrites its own source code. Every change is a commit to itself.
- **Native Desktop App** — Runs entirely on your machine as a standalone application (macOS, Linux, Windows). No cloud dependencies for execution.
- **Constitution** — Governed by [BIBLE.md](BIBLE.md) (13 philosophical principles, P0–P12). Philosophy first, code second.
- **Layered Safety** — Hardcoded sandbox blocks writes to safety-critical files and mutative git via shell; an explicit per-tool policy map decides which built-ins skip the LLM check; everything else goes through a single light-model safety call. The fail-open contract, the post-edit revert for `claude_code_edit`, and the full provider-mismatch matrix live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §Safety system and [`prompts/SAFETY.md`](prompts/SAFETY.md).
- **Multi-Provider Runtime** — Remote model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, or Cloud.ru Foundation Models. The optional model catalog helps populate provider-specific model IDs in Settings, and untouched default model values auto-remap to official OpenAI defaults when OpenRouter is absent.
- **Focused Task UX** — Chat shows plain typing for simple one-step replies and only promotes multi-step work into one expandable live task card. Logs still group task timelines instead of dumping every step as a separate row.
- **Background Consciousness** — Thinks between tasks. Has an inner life. Not reactive — proactive.
- **Improvement Backlog** — Post-task failures and review friction can now be captured into a small durable improvement backlog (`memory/knowledge/improvement-backlog.md`). It stays advisory, appears as a compact digest in task/consciousness context, and still requires `plan_task` before non-trivial implementation work.
- **Identity Persistence** — One continuous being across restarts. Remembers who it is, what it has done, and what it is becoming.
- **Embedded Version Control** — Contains its own local Git repo. Version controls its own evolution. Optional GitHub sync for remote backup.
- **Local Model Support** — Run with a local GGUF model via llama-cpp-python (Metal acceleration on Apple Silicon, CPU on Linux/Windows).
- **Transport Skills** — Optional bridges such as A2A and Telegram live as reviewed OuroborosHub skills instead of base-runtime code.

---

## Run from Source

### Requirements

- Python 3.10+
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/) — required for GitHub API tools (`list_github_prs`, `get_github_pr`, `comment_on_pr`, issue tools). Not required for pure-git PR tools (`fetch_pr_ref`, `cherry_pick_pr_commits`, etc.)

### Setup

```bash
git clone https://github.com/joi-lab/ouroboros-desktop.git
cd ouroboros-desktop
pip install -r requirements.txt
```

### Run

```bash
python server.py
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

You can also override the bind address and port:

```bash
python server.py --host 127.0.0.1 --port 9000
```

Available launch arguments:

| Argument | Default | Description |
|----------|---------|-------------|
| `--host` | `127.0.0.1` | Host/interface to bind the web server to |
| `--port` | `8765` | Port to bind the web server to |

The same values can also be provided via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OUROBOROS_SERVER_HOST` | `127.0.0.1` | Default bind host |
| `OUROBOROS_SERVER_PORT` | `8765` | Default bind port |
| `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD` | unset | Set to `1` only for trusted Docker/Kubernetes deployments where ingress auth, VPN, a private network, or an auth proxy already protects access |

For non-localhost binds, set `OUROBOROS_NETWORK_PASSWORD` (or use the
`OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD=1` escape hatch only when
ingress/VPN/private-network auth already protects the surface). The full
network bind matrix and Docker/Kubernetes deployment policy live in
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — read that before exposing
anything beyond loopback.

The Files tab uses your home directory by default only for localhost usage. For Docker or other
network-exposed runs, set `OUROBOROS_FILE_BROWSER_DEFAULT` to an explicit directory. Symlink entries are shown and can be read, edited, copied, moved, uploaded into, and deleted intentionally; root-delete protection still applies to the configured root itself.

### Provider Routing

Settings now exposes tabbed provider cards for:

- **OpenRouter** — default multi-model router
- **OpenAI** — official OpenAI API (use model values like `openai::gpt-5.5`)
- **OpenAI Compatible** — any custom OpenAI-style endpoint (use `openai-compatible::...`)
- **Cloud.ru Foundation Models** — Cloud.ru OpenAI-compatible runtime (use `cloudru::...`)
- **Anthropic** — direct runtime routing (`anthropic::claude-opus-4.6`, etc.) plus Claude Agent SDK tools

If OpenRouter is not configured and only official OpenAI is present, untouched default model values are auto-remapped to `openai::gpt-5.5` / `openai::gpt-5.5-mini` so the first-run path does not strand the app on OpenRouter-only defaults.

The Settings page also includes:

- optional `/api/model-catalog` lookup for configured providers
- centralized Secrets storage for API keys, bridge tokens, passwords, and future skill-requested keys
- a refactored desktop-first tabbed UI with searchable model pickers, segmented effort controls, masked-secret toggles, explicit `Clear` actions, and local-model controls

### Run Tests

```bash
make test
```

---

## Build

### Docker (web UI)

Docker is for the web UI/runtime flow, not the desktop bundle. The container binds to
`0.0.0.0:8765` by default, and the image now also defaults `OUROBOROS_FILE_BROWSER_DEFAULT`
to `${APP_HOME}` so the Files tab always has an explicit network-safe root inside the container.

> **Browser tools on Linux/Docker:** The `Dockerfile` runs `playwright install-deps chromium`
> (authoritative Playwright dependency resolver) and `playwright install chromium` so
> `browse_page` and `browser_action` work out of the box in the container. For source
> installs on Linux without Docker, run:
> `python3 -m playwright install-deps chromium` (requires sudo / distro package access).

Build the image:

```bash
docker build -t ouroboros-web .
```

Run on the default port:

```bash
docker run --rm -p 8765:8765 \
  -e OUROBOROS_NETWORK_PASSWORD='choose-a-password' \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Use a custom port via environment variables:

```bash
docker run --rm -p 9000:9000 \
  -e OUROBOROS_SERVER_PORT=9000 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

Run with launch arguments instead:

```bash
docker run --rm -p 9000:9000 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web --port 9000
```

Required/important environment variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `OUROBOROS_NETWORK_PASSWORD` | Optional | Enables the non-loopback password gate when set |
| `OUROBOROS_FILE_BROWSER_DEFAULT` | Defaults to `${APP_HOME}` in the image | Explicit root directory exposed in the Files tab |
| `OUROBOROS_SERVER_PORT` | Optional | Override container listen port |
| `OUROBOROS_SERVER_HOST` | Optional | Defaults to `0.0.0.0` in Docker |
| `OUROBOROS_TRUST_NONLOCAL_BIND_WITHOUT_PASSWORD` | Optional | See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) for the trusted-network bind policy |

Example: mount a host workspace and expose only that directory in Files:

```bash
docker run --rm -p 8765:8765 \
  -e OUROBOROS_FILE_BROWSER_DEFAULT=/workspace \
  -v "$PWD:/workspace" \
  ouroboros-web
```

### Release tag prerequisite

All three platform build scripts (`build.sh`, `build_linux.sh`,
`build_windows.ps1`) refuse to package a release unless `HEAD` is already
tagged with `v$(cat VERSION)` (BIBLE.md Principle 9: "Every release is
accompanied by an annotated git tag"). The scripts call `scripts/build_repo_bundle.py`
which embeds the resolved tag into `repo_bundle_manifest.json`, so the
launcher can later verify the packaged bundle matches a real release.

Tag the current commit before running any build script:

```bash
git tag -a "v$(tr -d '[:space:]' < VERSION)" -m "Release v$(tr -d '[:space:]' < VERSION)"
```

If the tag is missing, the build script fails with a clear error instead
of producing a bundle tagged with a synthetic/placeholder value.

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`

`build.sh` packages the macOS app and DMG. By default it signs with the
configured local Developer ID identity; set `OUROBOROS_SIGN=0` for an unsigned
local release. Unsigned builds require right-click → **Open** on first launch.

#### Optional signing & notarization (env vars)

`build.sh` honours these env overrides so the same script ships local,
shared-machine, and CI builds without forking the script:

| Env var | Effect |
|---------|--------|
| `OUROBOROS_SIGN=0` | Skip codesigning entirely (unsigned `.app` + `.dmg`). |
| `SIGN_IDENTITY="Developer ID Application: <Name> (<TeamID>)"` | Override the codesign identity. Useful for forks whose Developer ID is not the upstream default. |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_SPECIFIC_PASSWORD` | When all three are set, after codesign the DMG is submitted to Apple via `xcrun notarytool submit ... --wait` and stapled with `xcrun stapler staple` so receivers do not need right-click → **Open**. Missing any one falls back to "signed but not notarized" (no Apple-side ticket exists). |

**Forks: enabling signed CI builds.** The CI release flow
(`.github/workflows/ci.yml::build`) wires the build-script env vars above
from GitHub repository secrets, plus a small set of CI-only secrets that
import the Developer ID certificate into a temporary keychain on the
macOS runner. To exercise the signed-build path in a fork, configure
**all four** of the following as repository secrets (Settings → Secrets
and variables → Actions): `BUILD_CERTIFICATE_BASE64` (base64-encoded
`.p12`), `P12_PASSWORD`, `KEYCHAIN_PASSWORD` (an arbitrary passphrase
the workflow uses for its temporary keychain), and `APPLE_TEAM_ID`. Add
`APPLE_ID` + `APPLE_APP_SPECIFIC_PASSWORD` to additionally enable
notarization. If your Developer ID identity differs from the upstream
default, also set `SIGN_IDENTITY` (e.g.
`Developer ID Application: <Your Name> (<YOUR_TEAM_ID>)`). With no
Apple secrets configured the build job falls through to
`OUROBOROS_SIGN=0 bash build.sh` and ships an unsigned DMG identical to
v5.0.0 behaviour. See `docs/ARCHITECTURE.md` §8.1 and
`docs/DEVELOPMENT.md::"GitHub Actions: secrets in step-level if conditions"`
for the rationale (job-level `env:` mapping so step-level `if:` can read
`env.*`; GHA rejects `secrets.*` in step `if:`).

### Linux (.tar.gz)

```bash
bash scripts/download_python_standalone.sh
bash build_linux.sh
```

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`

> **Linux native libs:** The Chromium browser binary is bundled, but some hosts need
> native system libraries. If browser tools fail, install deps via the bundled Python
> (the bare `playwright` CLI is not on PATH in packaged builds):
> ```bash
> ./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium
> ```

### Windows (.zip)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`

---

## Architecture

Two-process desktop app. The launcher (`launcher.py`) is an immutable
PyWebView shell; it spawns `server.py`, which runs Starlette + uvicorn
plus a supervisor thread that manages worker processes. The agent core
lives in `ouroboros/`, the SPA in `web/`, the queue/process plane in
`supervisor/`, and the system prompts in `prompts/`.

For the full file-by-file structural map, the operational layer
(every API endpoint, log file, env var, state path), and the rationale
layer (the *why* for every non-trivial design decision), see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — that is the canonical
SSOT (Bible P6) and this README only summarizes it.

### Data Layout (`~/Ouroboros/`)

Created on first launch:

| Directory | Contents |
|-----------|----------|
| `repo/` | Self-modifying local Git repository |
| `data/state/` | Runtime state, budget tracking |
| `data/memory/` | Identity, working memory, system profile, knowledge base (including `improvement-backlog.md`), memory registry |
| `data/logs/` | Chat history, events, tool calls |
| `data/uploads/` | Chat file attachments (uploaded via paperclip button) |

---

## Configuration

### API Keys

| Key | Required | Where to get it |
|-----|----------|-----------------|
| OpenRouter API Key | No | [openrouter.ai/keys](https://openrouter.ai/keys) — default multi-model router |
| OpenAI API Key | No | [platform.openai.com/api-keys](https://platform.openai.com/api-keys) — official OpenAI runtime and web search |
| OpenAI Compatible API Key / Base URL | No | Any OpenAI-style endpoint (proxy, self-hosted gateway, third-party compatible API) |
| Cloud.ru Foundation Models API Key | No | Cloud.ru Foundation Models provider |
| Anthropic API Key | No | [console.anthropic.com](https://console.anthropic.com/settings/keys) — direct Anthropic runtime + Claude Agent SDK |
| Telegram Bot Token | No | [@BotFather](https://t.me/BotFather) — used by the optional Telegram bridge skill |
| GitHub Token | No | [github.com/settings/tokens](https://github.com/settings/tokens) — enables remote sync |

All keys are configured through the **Settings** page in the UI or during the first-run wizard.

### Default Models

| Slot | Default | Purpose |
|------|---------|---------|
| Main | `anthropic/claude-opus-4.6` | Primary reasoning |
| Code | `anthropic/claude-opus-4.6` | Code editing |
| Light | `anthropic/claude-sonnet-4.6` | Safety checks, consciousness, fast tasks |
| Fallback | `anthropic/claude-sonnet-4.6` | When primary model fails |
| Claude Agent SDK | `claude-opus-4-6[1m]` | Anthropic model for Claude Agent SDK tools (`claude_code_edit`, `advisory_pre_review`); the `[1m]` suffix is a Claude Code selector that requests the 1M-context extended mode |
| Scope Review | `openai/gpt-5.5` | Blocking scope reviewer (single-model, runs in parallel with triad review) |
| Web Search | `gpt-5.2` | OpenAI Responses API for web search |

Task/chat reasoning defaults to `medium`. Scope review reasoning defaults to `high`.

Models are configurable in the Settings page. Runtime model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, Cloud.ru, or direct Anthropic. When only official OpenAI is configured and the shipped default model values are still untouched, Ouroboros auto-remaps them to official OpenAI defaults. In **OpenAI-only** or **Anthropic-only** direct-provider mode, review-model lists are normalized automatically: the fallback shape is `[main_model, light_model, light_model]` (3 commit-triad slots, 2 unique models) so both the commit triad (which expects 3 reviewers) and `plan_task` (which requires >=2 unique for majority-vote) work out of the box. This fallback additionally requires the normalized main model to already start with the active provider prefix (`openai::` or `anthropic::`); custom main-model values that don't match the prefix leave the configured reviewer list as-is. If a user has overridden both main and light lanes to the same model, the fallback degrades to legacy `[main] * 3` and `plan_task` errors with a recovery hint (the commit triad still works). Both the commit triad and `plan_task` route through the same `ouroboros/config.py::get_review_models` SSOT. (OpenAI-compatible-only and Cloud.ru-only setups do not yet get this fallback — the detector returns empty when those keys are present, so users configure review-model lists manually in that case.)

### File Browser Start Directory

The web UI file browser is rooted at one configurable directory. Users can browse only inside that directory tree.

| Variable | Example | Behavior |
|----------|---------|----------|
| `OUROBOROS_FILE_BROWSER_DEFAULT` | `/home/app` | Sets the root directory of the `Files` tab |

Examples:

```bash
OUROBOROS_FILE_BROWSER_DEFAULT=/home/app python server.py
OUROBOROS_FILE_BROWSER_DEFAULT=/mnt/shared python server.py --port 9000
```

If the variable is not set, Ouroboros uses the current user's home directory. If the configured path does not exist or is not a directory, Ouroboros also falls back to the home directory.

The `Files` tab supports:

- downloading any file inside the configured browser root
- uploading a file into the currently opened directory

Uploads do not overwrite existing files. If a file with the same name already exists, the UI will show an error.

---

## Commands

Available in the chat interface:

| Command | Description |
|---------|-------------|
| `/panic` | Emergency stop. Kills ALL processes, closes the application. |
| `/restart` | Soft restart. Saves state, kills workers, re-launches. |
| `/status` | Shows active workers, task queue, and budget breakdown. |
| `/evolve` | Toggle autonomous evolution mode (on/off). |
| `/review` | Queue a deep self-review: sends all agent code, prompts, docs, and core memory artifacts (identity, scratchpad, registry, WORLD, knowledge index, patterns, improvement-backlog) to a 1M-context model for Constitution-grounded analysis. Excludes vendored libraries and operational logs. Rejected with an explicit error if the assembled prompt (system + pack) exceeds ~850K estimated tokens — on 1M-context models the window is shared between input and output. |
| `/bg` | Toggle background consciousness loop (start/stop/status). |

The same runtime actions are also exposed as compact buttons in the Chat header. All other messages are sent directly to the LLM.

---

## Philosophy

The 13 Constitution principles — Agency, Continuity, Meta-over-Patch,
Immune Integrity, Self-Creation, LLM-First, Authenticity & Reality
Discipline, Minimalism, Becoming, Versioning and Releases, the absorbed
Iterations / Spiral lineage, and Epistemic Stability — are defined in
full in [`BIBLE.md`](BIBLE.md). That file is the constitutional SSOT
(Bible P4 Ship-of-Theseus protection) and this README intentionally does
not paraphrase it.

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 5.10.3 | 2026-05-09 | **fix(skills+ui): harden transport skill UX and repair flows.** Settings refreshes skill-requested secrets without restart, dedupes custom requests, and keeps base keys in Stored Secrets; skill cards gate widget links on real UI tabs, sort by install time, and show busy review/repair state; toasts replace layout-shifting banners; repair-mode payload edits work cleanly in light mode; and shared page icons/header/glass rules are documented for future UI work. A paired OuroborosHub catalog update hardens the official A2A/Telegram bridge payloads for Host Service token, loopback, slash-command, and bounded-update safety checks. |
| 5.10.2 | 2026-05-09 | **fix(ui): restore page isolation after sticky headers.** Skills and Widgets keep their inner scroll regions without overriding the global `.page` visibility contract, and a Cursor visual-verification rule now requires screenshot inspection for future UI changes. |
| 5.10.1 | 2026-05-09 | **fix(skills): finish transport repair CI follow-ups.** Align skill-requested secret grants, `/api/command` task-constraint kwargs, and skill-flow tests with the structured repair contract so the full quick-test lane stays green after the v5.10 transport refactor. |
| 5.10.0 | 2026-05-09 | **feat(skills): generalize transport and repair flows.** Core chat provenance now uses a generic `transport` envelope instead of messenger-specific fields, skill repair tasks use structured `task_constraint` payload confinement instead of prompt markers, and Settings separates base secrets, skill-requested secrets, custom keys, and GitHub repo metadata. Skills and Widgets keep their headers fixed while scrolling content, and the paired OuroborosHub catalog update for `a2a`/`telegram-bridge` adds local slash-command rejection plus inbound backpressure before Host Service injection. |
| 5.9.2 | 2026-05-08 | **test(ci): harden companion and Docker UI smoke assertions.** Windows companion tests now assert environment preservation case-insensitively, and Docker UI smoke tolerates an already-initialized overlay before checking the chat page, matching CI runner behavior without changing runtime semantics. |
| 5.9.0 | 2026-05-08 | **feat(skills): move A2A and Telegram into reviewed transport skills.** A new loopback-only Host Service API, companion-process supervisor, event bus, opaque skill tokens, permission grants, and chat-id policy let reviewed skills inject transport messages without patching the core. Base-runtime A2A modules/deps and Telegram polling code are removed, Settings gains a unified Secrets surface, and this core release is paired with an external OuroborosHub catalog update that publishes the official `a2a` and `telegram-bridge` skills. Host Service, companion, grant, event-bus, shutdown, and transport-extraction tests cover the new flow. |
| 5.8.3-rc.5 | 2026-05-08 | **refactor(ABCDEF): consolidate SSOT across code, prompts, docs, and frontend.** The fifth release-candidate pass removes ~3k LOC of verified duplication without weakening the immune system or regressing governance docs (BIBLE.md, CHECKLISTS.md, contracts/, registry sandbox stay byte-untouched; ARCHITECTURE.md and CREATING_SKILLS.md keep their rationale layer while two narrow historical narratives are rewritten as forward-looking "Why" rationale). Code (group A): four `_emit_live_log` copies fold into `ouroboros/utils.py::emit_log_event`; web helpers `safeExternalHrefAttr` / `renderMarkdownSafe` / `boundedText` / `fetchJson` consolidate into `web/modules/utils.js`; tiny SSOT inlines (`_now_iso`, `_write_json_atomic`, `_CHECKLISTS_PATH`) and verified-dead test/export removals. Doc/prompts (group B): `prompts/SYSTEM.md` loses two duplicate Versioning blocks (and the active `git tag -a` collision bug at line 287), BIBLE-doctrine restatements compress to short pointers, and the `commit/obligation flow` narrative becomes a 6-bullet in-loop reminder; `README.md` ASCII tree, philosophy table, and Layered Safety essay collapse into pointers; `docs/CREATING_SKILLS.md` skill-review enum points back to `docs/CHECKLISTS.md` (SSOT); two narrow `docs/ARCHITECTURE.md` "Prior to v4.36.1" historical narratives become "Why warning-only:" rationale that preserves the *why* under P6. SSOT (group C): `850_000` token budget moves to `tools/review_helpers.py::REVIEW_PROMPT_TOKEN_BUDGET`; `_load_bible` / `_load_dev_guide_text` / `_load_architecture_text` route through `load_governance_doc(..., on_missing="explicit")` so missing core docs surface `[⚠️ OMISSION: …]` markers instead of silent empty strings (fixes a DEVELOPMENT.md "No silent truncation" violation); `web/modules/settings.js` `SETTINGS_FALLBACK_MODELS` gets a guard test against `config.py` defaults (caught real `anthropic/claude-sonnet-4.6` drift); `_record_commit_attempt` (20-arity, violates DEVELOPMENT.md "<8 parameters") accepts a new `@dataclass CommitAttemptRequest` while a backward-compat kwargs adapter keeps all 47 existing call sites intact. Frontend (group D): dead `hostPage='settings'` defaults in `logs/costs/evolution/updates.js` flip to `'dashboard'`; `.dashboard-tabs` / `.skills-tabs` CSS verbatim duplicates of `.app-tab-strip` / `.app-tab` are deleted; 50 hardcoded `rgba(201,53,69,X)` literals collapse into nine `--accent-*` tokens; 30 `border-radius: Npx;` literals adopt the existing `--radius-*` tokens (with new `--radius-md: 10px`). Test dedup (group E): five small redundancy deletions across `test_chat_js_contracts` / `test_tool_capabilities` / `test_docs_sync` / `test_phase7_pipeline` / `test_settings_ui_syntax` / `test_bughunt_fixes` / `test_chat_logs_ui`; the larger E1/E2/E3 parametrizations and group F module splits stay deferred to follow-up RCs. **Note on changelog rolloff**: the v5.8.1 and v5.8.2 patch rows were rolled off to respect the P9 5-patch-row cap; their full bodies remain at git tags `v5.8.1` and `v5.8.2`. |
| 5.7.0 | 2026-05-02 | **feat(skills+ui): expand the skill platform and repair mobile/dashboard UX.** The Skills platform gains core-visible `review_skill`, a heal-safe `skill_preflight` validator, expanded script runtimes (`deno`, `ruby`, `go`), larger `skill_exec` output caps, async extension tool handling, `PluginAPI.get_runtime_info`, extension-provided Settings sections, sandboxed `kind: module` widgets, declarative `map`/`calendar`/`kanban` components, dependency-state enforcement, and broader protection for skill provenance/control-plane files. The UI moves About into Settings, restores horizontal mobile Settings tabs, centers the mobile nav, differentiates Dashboard with a gauge icon, fixes Dashboard panel scrolling and duplicate labels, repairs the chat input fade/glass effect, anchors Skills kebab menus, stabilizes Widgets loading, and fixes ClawHub search/Official filtering/install progress. `video_gen` leaves the bundled seed (weather remains the only built-in example); a companion OuroborosHub catalog update (`joi-lab/OuroborosHub@68b3358`) publishes DuckDuckGo and Perplexity as official installable skills. **Note on changelog rolloff**: the v5.2.3 patch row was retained in the v5.7.0 minor release and rolled off in v5.7.1; its full body remains in git history. |
| 5.6.0 | 2026-05-01 | **feat(ui): add managed updates, settings-hosted observability, smart skill activation, and mobile polish.** Settings becomes the app hub for operational surfaces: Logs, Evolution, Updates, and Costs now live as first-class Settings sub-tabs while the mobile Settings page switches to a native stack-style section list. The chat budget pill opens Costs directly, and the chat composer now uses a flex layout with the iOS `interactive-widget` viewport hint removed so the input stays anchored above mobile keyboards. The new Updates tab exposes the existing launcher-managed `managed` remote as a safe owner-facing Update flow: passive status by default, explicit Check for updates, rescue snapshot before apply, exact target-SHA pinning, and automatic local-keep preservation for divergent committed work. Skills keep the visual toggle but turn it into the activation entry point: enabling can run review, request owner key grants through the desktop launcher confirmation bridge, and then enable without exposing duplicate grant-error copy. Buttons now share one documented design system, widget inline cards preserve state across tab switches, and the change was visually checked across desktop and mobile. **Note on changelog rolloff**: the v5.1.0 minor row was rolled off to respect the P9 5-minor-row cap; its full body remains at git tag `v5.1.0`. |
Older releases are preserved in Git tags and GitHub releases. The 5.2.0, 5.3.0, 5.3.x release-candidate, 5.4.0, 5.5.0, 5.7.5, 5.7.6, 5.8.0-rc.2, 5.8.0-rc.6, 5.8.1, 5.8.2, 5.8.3-rc.1, 5.8.3-rc.2, 5.8.3-rc.3, 5.8.3-rc.4, 5.9.1, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
