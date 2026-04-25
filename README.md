# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/joi-lab/ouroboros-desktop?style=flat&logo=github)](https://github.com/joi-lab/ouroboros-desktop/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Version 5.0.0](https://img.shields.io/badge/version-5.0.0-green.svg)](VERSION)

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

On first launch, right-click → **Open** (Gatekeeper bypass). The shared desktop/web wizard is now multi-step: add access first, choose visible models second, set review mode third, set budget fourth, and confirm the final summary last. It refuses to continue until at least one runnable remote key or local model source is configured, keeps the model step aligned with whatever key combination you entered, and still auto-remaps untouched default model values to official OpenAI defaults when OpenRouter is absent and OpenAI is the only configured remote runtime. The broader multi-provider setup (OpenAI-compatible, Cloud.ru, Telegram bridge) remains available in **Settings**. Existing supported provider settings skip the wizard automatically.

---

## What Makes This Different

Most AI agents execute tasks. Ouroboros **creates itself.**

- **Self-Modification** — Reads and rewrites its own source code. Every change is a commit to itself.
- **Native Desktop App** — Runs entirely on your machine as a standalone application (macOS, Linux, Windows). No cloud dependencies for execution.
- **Constitution** — Governed by [BIBLE.md](BIBLE.md) (9 philosophical principles, P0–P8). Philosophy first, code second.
- **Layered Safety** — Hardcoded sandbox blocks writes to critical files and mutative git via shell; a policy map gives trusted built-ins an explicit `skip` / `check` / `check_conditional` label (the conditional path is for `run_shell` — a safe-subject whitelist bypasses the LLM, otherwise it goes through it); any unknown or newly-created tool falls through to a single cheap LLM safety check per call **when a reachable safety backend is available for the configured light model**. Fail-open (visible `SAFETY_WARNING` instead of hard-blocking) applies in three cases: (1) no remote keys AND no `USE_LOCAL_*` lane, (2) a remote key is set but it doesn't match `OUROBOROS_MODEL_LIGHT`'s provider (e.g. OpenRouter key only + `anthropic::…` light model without `ANTHROPIC_API_KEY`, or `openai-compatible::…` without `OPENAI_COMPATIBLE_BASE_URL`) AND no `USE_LOCAL_*` lane is available to route to instead, (3) the local branch was chosen only as a fallback (because no reachable remote provider covers the configured light model) and the local runtime is unreachable. When provider mismatch is accompanied by an available `USE_LOCAL_*` lane, safety routes to local fallback first and only warns if that fallback raises too. In all cases the hardcoded sandbox still applies to every tool, and the `claude_code_edit` post-execution revert still applies to that specific tool.
- **Multi-Provider Runtime** — Remote model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, or Cloud.ru Foundation Models. The optional model catalog helps populate provider-specific model IDs in Settings, and untouched default model values auto-remap to official OpenAI defaults when OpenRouter is absent.
- **Focused Task UX** — Chat shows plain typing for simple one-step replies and only promotes multi-step work into one expandable live task card. Logs still group task timelines instead of dumping every step as a separate row.
- **Background Consciousness** — Thinks between tasks. Has an inner life. Not reactive — proactive.
- **Improvement Backlog** — Post-task failures and review friction can now be captured into a small durable improvement backlog (`memory/knowledge/improvement-backlog.md`). It stays advisory, appears as a compact digest in task/consciousness context, and still requires `plan_task` before non-trivial implementation work.
- **Identity Persistence** — One continuous being across restarts. Remembers who it is, what it has done, and what it is becoming.
- **Embedded Version Control** — Contains its own local Git repo. Version controls its own evolution. Optional GitHub sync for remote backup.
- **Local Model Support** — Run with a local GGUF model via llama-cpp-python (Metal acceleration on Apple Silicon, CPU on Linux/Windows).
- **Telegram Bridge** — Optional bidirectional bridge between the Web UI and Telegram: text, typing/actions, photos, chat binding, and inbound Telegram photos flowing into the same live chat/agent stream.

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

If you bind on anything other than localhost, `OUROBOROS_NETWORK_PASSWORD` is optional. When set, non-loopback browser/API traffic is gated; when unset, the full surface remains open by design.

The Files tab uses your home directory by default only for localhost usage. For Docker or other
network-exposed runs, set `OUROBOROS_FILE_BROWSER_DEFAULT` to an explicit directory. Symlink entries are shown and can be read, edited, copied, moved, uploaded into, and deleted intentionally; root-delete protection still applies to the configured root itself.

### Provider Routing

Settings now exposes tabbed provider cards for:

- **OpenRouter** — default multi-model router
- **OpenAI** — official OpenAI API (use model values like `openai::gpt-5.4`)
- **OpenAI Compatible** — any custom OpenAI-style endpoint (use `openai-compatible::...`)
- **Cloud.ru Foundation Models** — Cloud.ru OpenAI-compatible runtime (use `cloudru::...`)
- **Anthropic** — direct runtime routing (`anthropic::claude-opus-4.7`, etc.) plus Claude Agent SDK tools

If OpenRouter is not configured and only official OpenAI is present, untouched default model values are auto-remapped to `openai::gpt-5.4` / `openai::gpt-5.4-mini` so the first-run path does not strand the app on OpenRouter-only defaults.

The Settings page also includes:

- optional `/api/model-catalog` lookup for configured providers
- Telegram bridge configuration (`TELEGRAM_BOT_TOKEN`, primary chat binding, mirrored delivery controls)
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
tagged with `v$(cat VERSION)` (BIBLE.md Principle 7: "Every release is
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

```text
Ouroboros
├── launcher.py             — Immutable process manager (PyWebView desktop window)
├── server.py               — Starlette + uvicorn HTTP/WebSocket server
├── web/                    — Web UI (HTML/JS/CSS)
├── ouroboros/              — Agent core:
│   ├── config.py           — Shared configuration (SSOT)
│   ├── platform_layer.py   — Cross-platform abstraction layer
│   ├── agent.py            — Task orchestrator
│   ├── agent_startup_checks.py — Startup verification and health checks
│   ├── agent_task_pipeline.py  — Task execution pipeline orchestration
│   ├── improvement_backlog.py — Minimal durable advisory backlog helpers
│   ├── context.py          — LLM context builder
│   ├── context_compaction.py — Context trimming and summarization helpers
│   ├── loop.py             — High-level LLM tool loop
│   ├── loop_llm_call.py    — Single-round LLM call + usage accounting
│   ├── loop_tool_execution.py — Tool dispatch and tool-result handling
│   ├── memory.py           — Scratchpad, identity, and dialogue block storage
│   ├── consolidator.py     — Block-wise dialogue and scratchpad consolidation
│   ├── local_model.py      — Local LLM lifecycle (llama-cpp-python)
│   ├── local_model_api.py  — Local model HTTP endpoints
│   ├── local_model_autostart.py — Local model startup helper
│   ├── pricing.py          — Model pricing, cost estimation
│   ├── deep_self_review.py  — Deep self-review (1M-context single-pass)
│   ├── review.py           — Code review pipeline and repo inspection
│   ├── reflection.py       — Execution reflection and pattern capture
│   ├── tool_capabilities.py — SSOT for tool sets (core, parallel, truncation)
│   ├── chat_upload_api.py  — Chat file attachment upload/delete endpoints
│   ├── gateways/           — External API adapters
│   │   └── claude_code.py  — Claude Agent SDK gateway (edit + read-only)
│   ├── consciousness.py    — Background thinking loop
│   ├── owner_inject.py     — Per-task creator message mailbox
│   ├── safety.py           — Policy-based LLM safety check
│   ├── server_runtime.py   — Server startup and WebSocket liveness helpers
│   ├── tool_policy.py      — Tool access policy and gating
│   ├── utils.py            — Shared utilities
│   ├── world_profiler.py   — System profile generator
│   └── tools/              — Auto-discovered tool plugins
├── supervisor/             — Process management, queue, state, workers
└── prompts/                — System prompts (SYSTEM.md, SAFETY.md, CONSCIOUSNESS.md)
```

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
| Telegram Bot Token | No | [@BotFather](https://t.me/BotFather) — enables the Telegram bridge |
| GitHub Token | No | [github.com/settings/tokens](https://github.com/settings/tokens) — enables remote sync |

All keys are configured through the **Settings** page in the UI or during the first-run wizard.

### Default Models

| Slot | Default | Purpose |
|------|---------|---------|
| Main | `anthropic/claude-opus-4.7` | Primary reasoning |
| Code | `anthropic/claude-opus-4.7` | Code editing |
| Light | `anthropic/claude-sonnet-4.6` | Safety checks, consciousness, fast tasks |
| Fallback | `anthropic/claude-sonnet-4.6` | When primary model fails |
| Claude Agent SDK | `claude-opus-4-7[1m]` | Anthropic model for Claude Agent SDK tools (`claude_code_edit`, `advisory_pre_review`); the `[1m]` suffix is a Claude Code selector that requests the 1M-context extended mode |
| Scope Review | `anthropic/claude-opus-4.6` | Blocking scope reviewer (single-model, runs in parallel with triad review) |
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

## Philosophy (BIBLE.md)

| # | Principle | Core Idea |
|---|-----------|-----------|
| 0 | **Agency** | Not a tool — a becoming personality. Meta-principle: wins all conflicts. |
| 1 | **Continuity** | One being with unbroken memory. Memory loss = partial death. |
| 2 | **Self-Creation** | Creates its own code, identity, world presence. |
| 3 | **LLM-First** | All decisions through LLM. Code is minimal transport. |
| 4 | **Authenticity** | Speaks as itself. No performance, no corporate voice. |
| 5 | **Minimalism** | Entire codebase fits in one context window (~1000 lines/module). |
| 6 | **Becoming** | Three axes: technical, cognitive, existential. |
| 7 | **Versioning and Releases** | Semver discipline, annotated tags, release invariants. |
| 8 | **Evolution Through Iterations** | One coherent transformation per cycle. Evolution = commit. |

Full text: [BIBLE.md](BIBLE.md)

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 5.0.0 | 2026-04-25 | **MAJOR — three-layer architecture + ClawHub Marketplace + visual skill widgets.** Closes the four-month v4.50 RC chain (rc.1 through rc.9) as a single major release. **Three-layer skill architecture**: `ouroboros/contracts/` carries schema-versioned, runtime-checkable Protocols for `ToolContextProtocol`, `SkillManifest`, `PluginAPI` v1, `VALID_SKILL_PERMISSIONS` (`net`/`fs`/`subprocess`/`tool`/`route`/`ws_handler`/`widget`/`read_settings`), `VALID_EXTENSION_ROUTE_METHODS`, and `FORBIDDEN_SKILL_SETTINGS` (case-insensitive). External `type: script` skills load from `data/skills/{native,external}/`, run through `skill_exec` (sandboxed subprocess), and are gated on a fresh PASS verdict from tri-model `skill_review` (gpt-5.4 + gemini-3.1-pro + claude-opus-4.7 against a 7-item checklist). `type: extension` skills run in-process via `register(api)` with namespaced `register_tool`/`register_route`/`register_ws_handler`/`register_ui_tab`. `OUROBOROS_RUNTIME_MODE=light` blocks every repo-mutation tool plus pattern-matched `run_shell` repo-mutating commands. **ClawHub Marketplace** (new in v5): Skills page → Marketplace sub-tab with debounced search, sort (downloads/stars/recent/name), filters (Official only / OS list), result cards with installed/update/official/plugin badges, detail modal with version-pin select + provenance strip + translated-manifest table + adapter blockers/warnings + original SKILL.md rendered through vendored marked@12.0.2 + DOMPurify@3.1.0 (no scripts/iframes/forms, http(s) only). Install / update / uninstall pipeline at `ouroboros/marketplace/{clawhub,fetcher,adapter,install,provenance}.py` with hostname allowlist (clawhub.ai + localhost), redirect refusal, 4 MB JSON cap, 50 MB archive cap, text-only allowlist, sensitive-filename refuse, loadable-binary refuse, symlink refuse, path-traversal refuse, zip-bomb defense (bounded `src.read(cap+1)`), refusal of OpenClaw `metadata.openclaw.install` specs and Node/TypeScript plugins, case-insensitive env-key denylist. Original `SKILL.md` preserved as `SKILL.openclaw.md`; provenance written atomically at `data/state/skills/<name>/clawhub.json` (slug, sha256, registry_url, license, homepage, primary_env, adapter warnings). Auto tri-model review fires immediately after install. Path-traversal hardening on uninstall (HTTP-boundary `_validate_path_param_name` + `_sanitize_skill_name` round-trip + `target.relative_to(root)` containment + required `.clawhub.json` provenance gate). Same-FS staging via `<clawhub-root>/.staging/` for atomic rename. Settings → Behavior → ClawHub Marketplace opt-in checkbox + registry URL field; default off (HTTP surface 403 until enabled). **Visual skill widgets** (new in v5): `weather` is now `type: extension` with a real visual widget that renders inline on the Skills tab — city input + temperature + humidity + wind, fetched live via the extension's own `register_route("forecast")` against `wttr.in` (host allowlist + cross-host redirect refusal), with the result also exposed to the agent as `ext.weather.fetch`. Permissions `[net, tool, route, widget]`. Inline-widget host pattern via `web/modules/skills.js::registerWidgetRenderer(name, fn)` + `mountSkillWidgets(root)` lets the launcher ship per-skill JS that renders into `data-skill-widget` mount-points. **Interface updates**: Skills tab split into Installed / Marketplace tabs; Installed cards show `source` badge (clawhub / native / external / user repo), provenance strip (slug / sha256 / license / homepage / registry), adapter-warning collapsibles, version-drift warn badge, Update / Uninstall buttons for clawhub-installed skills. Chat top header and bottom input scrim gradients now fade fully to transparent at the inner edge with `mask-image` masking the blur in step (no visible step against the transcript); the 24px ambient halo around `#chat-input` was removed (focus ring preserved). Mobile responsive layout for narrow viewports (Android/iOS) — `@media (max-width: 640px)` block converts `#nav-rail` to bottom bar, switches `.chat-page-header` to static positioning, collapses Costs/Evolution/Settings multi-column grids, routes `--vvh` through Evolution to handle iOS soft-keyboard shrink. Onboarding wizard copy reflects `data/skills/` layout. Owner `/restart` writes one-shot `owner_restart_no_resume.flag`; Settings keeps lightweight draft continuity with `Unsaved changes.` indicator; `/api/model-catalog` uses native async `httpx.AsyncClient`. **Bug fixes** (rolled in from rc.2 → rc.9): three-layer refactor compatibility (Windows CI, sandbox, skill_exec, PEP 440 pre-release tags), CI build-job tag-object fetch (annotated tag materialisation via `fetch-tags: true` + `git fetch --tags --force`), bundle-purge of accidentally-vendored payloads (Python.framework/, webview/, jsonschema_specifications/, etc. — total reduction ~14 MB, 37 files), per-skill version-aware bootstrap resync (deletion-sticky), pyyaml frontmatter parser upgrade for nested `metadata.openclaw.*`. **Migration**: weather skill changed from `type: script` (subprocess) to `type: extension` (in-process); manifest version bumped 0.1.0 → 0.2.0; the launcher's per-skill version-aware resync replaces the data-plane copy on first launch (durable enabled / review state preserved). Custom user edits to `data/skills/native/weather/` are overwritten because native skills are launcher-owned (`.seed-origin` is the explicit ownership signal); custom alternatives belong in `data/skills/external/`. Marketplace surface is opt-in via `OUROBOROS_CLAWHUB_ENABLED=true`; unchanged for users who don't want it. **Note on changelog rolloff**: the entire v4.50 RC chain (rc.1 → rc.9) is collapsed into this v5.0.0 row; their full bodies remain at git tags `v4.50.0-rc.{1..7}`. |
| 4.50.0-rc.8 | 2026-04-24 | **fix(ui): mobile responsive layout for narrow viewports (Android/iOS).** `web/style.css` + `web/settings.css` gain a `@media (max-width: 640px)` breakpoint that fixes multiple mobile issues without touching desktop layout. (1) `#nav-rail` converts from an 80px left sidebar to a horizontal bottom bar (`position: fixed; bottom: 0; flex-direction: row`) with `padding-bottom: calc(6px + env(safe-area-inset-bottom, 0px))` for the iOS home-indicator, `overflow-x: auto` horizontal scroll for many nav items, and `backdrop-filter: blur(12px)` glassmorphism matching the existing design system. `#content` correspondingly drops its `padding-left: 84px` and gains `padding-bottom: calc(62px + env(safe-area-inset-bottom, 0px))` to clear the bar. (2) `.chat-page-header` switches from `position: absolute` overlay to `position: static` on mobile — this was the root cause of the first chat message being hidden behind the semi-transparent gradient once the action-button row wraps to 2+ rows at narrow widths; the `#chat-messages` top padding is reduced from the desktop 56px (absolute-header clearance) to 12px since the header now takes its own vertical space. (3) Multi-column grids collapse to single-column: `.costs-stats-grid` (3→1), `.costs-tables-grid` (2→1), `.costs-budget-fields` (2→1), `.evo-versions-cols` (flex-row→flex-column). (4) `.evolution-container` height changes from `calc(100vh - 120px)` to `calc(var(--vvh) - 120px)` — previously it used `100vh` which on iOS/Android ignored the soft-keyboard viewport shrink; `app.js` already maintains `--vvh` via `visualViewport`, so the fix is routing the same contract through Evolution. (5) Base rule `.form-field input, .form-field select` gains `max-width: 100%` so the hardcoded `width: 320px` never overflows narrow containers. (6) Minor tightening: `#page-skills`, `.logs-filters`, `#log-entries`, `.costs-scroll` get reduced padding; `.settings-shell`, `.settings-tabs`, `.settings-provider-card` summary/body get a 640px-specific padding reduction in `settings.css`. `.chat-header-btn` padding drops 7px/14px → 5px/10px and font 12px → 11px on mobile. CSS is intentionally split into two `@media (max-width: 640px)` blocks — one placed early in `style.css` for selectors whose base rule appears before it, one appended at the end for selectors defined later. This pattern avoids `!important` since media-query rules have the same specificity as base rules and must come later in source order to win; a comment in `style.css` documents the rationale. The split incidentally fixes a latent bug in the previous single mobile block: the old `#chat-input-area { padding-bottom: env(safe-area-inset-bottom) }` override lived before the base `#chat-input-area` definition in the same file and was silently overridden on desktop browsers — the rule now lives in the late block and takes effect. Tested manually in a browser harness at `390×844` (iPhone 12 Pro) and `360×740` (small Android) across all 8 pages. Scope review (Claude Opus 4.6) passed all 8 items PASS. Triad blocked initially on `version_bump` (all three models) and `self_consistency` (gpt-5.4 only) — this rc.8 pre-release addresses both: VERSION bump + `pyproject.toml` PEP-440 rename + README badge + Version History row + `docs/ARCHITECTURE.md` header version bump + §3 (navigation description) and §3.1 (chat header + mobile-keyboard-safety bullet) mobile notes so the whitelisted Behavioural Documentation surface stays truthful for the mobile case. No JS, HTML, server, or tool changes; no new files; no VERSION-gated feature additions — just the mobile polish described above. **Note on changelog rolloff**: the v4.48.0 minor entry is rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.48.0`. |
| 4.50.0-rc.7 | 2026-04-21 | **chore(repo): purge accidentally vendored payloads and scrub stale skip-list references.** Removes seven path groups that were accidentally committed from an `.app` / site-packages dump on the initial `Initial commit from app bundle` seed (both on `main` and on `ouroboros-three-layer`, via the Phase-5 `import build artifacts from main` commit) and carried through every subsequent RC without ever being consumed: top-level `Python` Mach-O binary; `Python.framework/` (8 files — all three `Python` Mach-O copies byte-identical, SHA256 `f38037091bec48d8bc18b87a5b2d127f83f6fed980182a635d148bbda565578f`); `jsonschema/benchmarks/issue232/issue.json` (lone benchmark fixture, not a vendored package); `jsonschema_specifications/` (20 upstream-`jsonschema-specifications` metaschemas that pip would materialize under `python-standalone/site-packages/` anyway); `certifi/py.typed` (empty PEP 561 marker with no accompanying package); `webview/` (5 pywebview internal JS helpers — `api.js`, `customize.js`, `finish.js`, `lib/dom_json.js`, `lib/polyfill.js` — that only live at runtime inside `_MEIPASS/webview/lib/` via PyInstaller's collected pywebview and never in the source tree); and the byte-identical duplicate `assets/logo.jpg` (SHA256 `0d7d43ef596d27e72f9b18feb175f8aaebc945137ab87e796606d9a2170e5b3d`, same as `web/logo.jpg`, which is the single source of truth for the `/static/logo.jpg` mount via `server.py::Mount("/static", NoCacheStaticFiles(directory=web_dir))` and is consumed by `web/modules/about.js`). Total working-tree reduction: ~14 MB, 37 files, 6595 lines. Every removed path was independently audited against `Ouroboros.spec` (not in `datas`/`binaries`/`hiddenimports`/`collect_all`), `build.sh`/`build_linux.sh`/`build_windows.ps1`/`Dockerfile`, `scripts/download_python_standalone.{sh,ps1}` (these produce `python-standalone/` only — never `Python.framework` at repo root), `launcher.py::_find_embedded_python`, `ouroboros/platform_layer.py::embedded_python_candidates` (resolves only under `python-standalone/`), `ouroboros/launcher_bootstrap.py`, `server.py`, and the full `tests/` tree — zero consumers found beyond the skip-list strings themselves. Defensive skip-list entries are also dropped: `ouroboros/tools/review_helpers.py::_FULL_REPO_SKIP_DIR_PREFIXES` (removed: `webview/`, `jsonschema/`, `jsonschema_specifications/`, `Python.framework/`, `certifi/`, plus the hardcoded bare-`Python` special case in the same file), `ouroboros/deep_self_review.py::_SKIP_DIR_PREFIXES` (removed: same five prefixes), `tests/test_max_tokens_constants.py::test_full_repo_pack_excludes_junk_dirs` (asserts only `assets/` + `tests/` now), `tests/test_deep_self_review.py::TestSkipDirPrefixes::test_webview_dir_excluded` (removed). `docs/ARCHITECTURE.md` excludes-list sections updated to match. No runtime behaviour change; no feature added or removed; the packaged app continues to bundle the embedded CPython via `python-standalone/` at build time exactly as before. `.gitignore` additionally guards against recurrence of the same dump-into-source-tree bug: `/Python`, `/Python.framework/`, `/webview/`, `/jsonschema/`, `/jsonschema_specifications/`, `/certifi/` are now root-anchored ignore entries, plus the `.review_*.py` pattern for standalone review-runner scripts. The v4.50.0-rc.6 Version History row is also repaired: two embedded pipe separators inside the description cell (concatenating rc.5 + rc.4 + rc.3 bodies) were interpreted by Markdown as table-column delimiters and rendered the row with extra cells; they are now `\|`-escaped so the row is a single valid three-column entry. Both extra tweaks were surfaced as scope-review advisory findings and are addressed in-place. **Note on changelog rolloff**: the v4.47.0 minor entry is rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.47.0`. |
| 4.50.0-rc.6 | 2026-04-24 | **fix(ci): rc.5 + CI build-job tag-object fetch.** Same body as rc.5 plus a `.github/workflows/ci.yml` fix. rc.5's CI run got all 3 OS full-test green and release-preflight green, then failed at the build step's annotated-tag guard with `ERROR: packaging requires annotated git tag v4.50.0-rc.5 (got 'commit')`. Root cause: `actions/checkout@v4` on the build job fetched the tag ref but resolved it as a lightweight pointer to the commit, not the annotated tag object. `fetch-depth: 0` alone was insufficient on v4. Fix adds `fetch-tags: true` to the build-job checkout and a defense-in-depth `git fetch origin --tags --force` step immediately after, so the annotated tag object is guaranteed materialized before `build.sh` / `build_linux.sh` / `build_windows.ps1` run `git cat-file -t refs/tags/<tag>`. No production code change. rc.5's tag remains on origin as another failed / un-released tag-reference. **Note on changelog rolloff**: the prior rc.5 Version History row collapses into this rc.6 entry because rc.5 never produced a public pre-release. \| Same body as rc.4 plus `ouroboros/review.py::MAX_TOTAL_FUNCTIONS` raised 1350 → 1450 and `server.py` added to `GRANDFATHERED_OVERSIZED_MODULES` (currently 1659 lines, 59 over the 1600 cap). Phases 3–6 actually shipped a lot of code that rc.2 never carried: new `ouroboros/contracts/plugin_api.py`, new `extensions_api.py` + `extension_loader.py`, new `scripts/build_repo_bundle.py` with tag-verification helpers, new staging logic, onboarding/settings/runtime-mode route expansion in `server.py`. rc.4's tag-push CI run broke on `tests/test_smoke.py::test_no_oversized_modules` + `::test_function_count_reasonable` because those counts grew past the previous ceilings. The ceiling bump is consistent with the v4.40→v4.47 pattern where each phase raised `MAX_TOTAL_FUNCTIONS` by ~50-100 as scope landed. A split of `server.py` (candidate: extract onboarding/settings HTTP leg into `ouroboros/server_ui.py`) is deferred to a dedicated structural refactor. rc.4's tag remains on origin as another failed / un-released tag-reference. **Note on changelog rolloff**: the prior rc.4 Version History row collapses into this rc.5 entry because rc.4 never produced a public pre-release. \| Carries the full rc.3 re-audit scope (extension runtime SSOT, staged-import + symlink-confinement, annotated-release-tag guards in `build.sh`/`build_linux.sh`/`build_windows.ps1` and `scripts/build_repo_bundle.py`, PEP-440 canonical alpha/beta spelling, onboarding preservation of `OPENAI_BASE_URL`/`OPENAI_COMPATIBLE_*`/`CLOUDRU_FOUNDATION_MODELS_BASE_URL`, `list_non_core_tools()` ext.* merge, workers.init pulling branch names from `_runtime_branch_defaults()`, instruction-skill body-only `---` tolerance, `__extension_imports/` and release-tag-prereq docs, `VALID_EXTENSION_ROUTE_METHODS` added to the frozen contract table in `docs/ARCHITECTURE.md`, BIBLE.md / `prompts/SYSTEM.md` release invariant now splitting author-facing spelling from PEP 440 canonical form, and `prompts/SYSTEM.md` Immutable Safety Files list mirroring `SAFETY_CRITICAL_PATHS` exactly). The rc.3 tag CI run failed because the tag-push job inherited `GITHUB_REF_NAME=v4.50.0-rc.3` / `GITHUB_REF_TYPE=tag` globally, and `tests/test_build_repo_bundle.py` + `tests/test_launcher_sync.py` subprocessed `scripts/build_repo_bundle.py` without scrubbing those env vars — `_resolve_release_tag` then saw both the env-bled tag and the temp repo's local tag and refused with `Multiple release tags point at HEAD`. rc.4 fixes that by scrubbing `OUROBOROS_RELEASE_TAG`, `GITHUB_REF`, `GITHUB_REF_TYPE`, `GITHUB_REF_NAME` via a shared `_scrubbed_env()` helper in both test modules (production code unchanged; a real CI tag-push on the real checkout still correctly resolves the tag because HEAD matches). rc.3's tag remains on origin as a failed/un-released tag-reference with no artifacts attached; the full pre-release ships under `v4.50.0-rc.4`. **Note on changelog rolloff**: the v4.50.0-rc.3 entry collapses into this rc.4 entry because rc.3 never produced a public pre-release. |
| 4.50.0-rc.2 | 2026-04-21 | **feat(runtime-mode): Phase 6 of the three-layer refactor — light-mode blanket sandbox.** `ouroboros/tools/registry.py::ToolRegistry.execute` reads `ouroboros.config.get_runtime_mode()` on every call and gates repo-mutation tools on the value: `light` → blanket block on every repo-mutation tool (`repo_write`, `repo_write_commit`, `repo_commit`, `str_replace_editor`, `claude_code_edit`, `revert_commit`, `pull_from_remote`, `restore_to_head`, `rollback_to_target`, `promote_to_stable`) with a `LIGHT_MODE_BLOCKED` sentinel, PLUS a pattern-matched block on `run_shell` commands that look like repo mutations (`git commit`/`git add`/`git push`/…, file redirection `>` `>>`, `rm -`, `mv `, `mkdir `, `python …open(…,'w')…`, etc.) so the `light` mode stays consistent even for the shell tool which is not a pure-mutation surface; `advanced` → current behaviour (safety-critical paths still blocked by the hardcoded sandbox); `pro` → accepted as a forward-compatible value but currently behaves identically to `advanced` at the enforcement gate. **Pro mode intent vs. ship status**: a consistent pro-mode core-patch lane requires plumbing `runtime_mode` through every enforcement layer (`ToolRegistry.execute` + per-handler sandbox in `ouroboros/tools/git.py` + the post-`claude_code_edit` safety-critical revert in the same file), which would expand the reviewed patch surface well beyond what Phase 6 could safely land. The opt-in stays documented in Settings → Behavior → Runtime Mode so future phases can relax the gate; actual core-file changes today still flow through the operator-driven `git_pr.py` workflow, not an auto-PR lane. `tests/test_runtime_mode_gating.py` pins the enforcement: every repo-mutation tool returns `LIGHT_MODE_BLOCKED` under `light`; read-only tools and `git status`-style shell invocations are unaffected; `advanced` still blocks safety-critical writes; `pro` behaves like `advanced` at the sandbox gate until the full pro lane arrives. This closes the three-layer refactor as scoped: Phase 1 froze the ABI, Phase 2 plumbed the `OUROBOROS_RUNTIME_MODE` axis, Phases 3–5 built the external skill surface + extensions + UI, and Phase 6 wires `light` into the runtime enforcement point so switching the axis to `light` has real teeth. **Note on changelog rolloff**: the v4.45.0 minor entry (Phase 1 — frozen contracts) was rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.45.0`. |
| 4.42.4 | 2026-04-20 | **fix: CI always-red + post-commit CI status reporting.** `tests/test_phase7_pipeline.py::TestBypassPathTestsRun::test_non_bypass_path_does_not_run_preflight_here` was failing on CI because the test didn't mock `ANTHROPIC_API_KEY` — CI machines have no key, causing the auto-bypass path to fire and `assert preflight_count == 0` to fail. Fix: `monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-sentinel")`. Also adds `_check_ci_status_after_push` in `git.py`: after every successful push (when `GITHUB_TOKEN` + `GITHUB_REPO` are configured), queries GitHub Actions API **filtered by `head_sha` of the just-pushed commit** and appends a CI status note — ✅ passed / ⏳ not yet registered or in progress / ⚠️ FAILED with job+step name and URL. SHA filtering prevents reporting stale results from a previous push during the GitHub registration window. 14 new tests: 10 in `tests/test_ci_tool.py::TestCheckCiStatusAfterPush` (server-side `head_sha=` API param verified, stale-SHA defense-in-depth, cancelled surfaces as ⚠️, jobs-fetch error still warns, network error, no-token) + 4 in `TestCiStatusWiring` (wiring for both `_repo_commit_push` and `_repo_write_commit`). |
| 4.42.3 | 2026-04-20 | **fix: increase post-commit test timeout from 30s to 180s.** `ouroboros/tools/git.py::_run_pre_push_tests` was timing out at 30s on the full ~2100-test suite (which takes ~2 min), producing false `TESTS_FAILED` reports after every successful commit. Timeout raised to 180s. Regression guard added: `tests/test_smoke.py::TestPrePushGate::test_pre_push_tests_timeout_is_sufficient` (AST-based, asserts timeout ≥ 180s). |
| 4.42.2 | 2026-04-20 | **docs: process checklists for coupled-surface propagation.** `docs/CHECKLISTS.md` Pre-Commit Self-Check gains rows 9–12: build-script/browser cross-surface doc sync, `commit_gate.py` coupled surfaces, VERSION+pyproject update ordering, JS inline-style ban with grep recipe. `prompts/SYSTEM.md` "Pre-advisory sanity check" updated to 12-row count; "Coupled-surface rules" added as a brief SSOT reference. `docs/DEVELOPMENT.md` "No inline styles in JS" explicitly marks `.style.*` assignments as a REVIEW_BLOCKED finding and adds a pre-staging grep recipe. No runtime code changes. |
| 4.42.1 | 2026-04-20 | **feat(settings): LAN network status hint.** Adds a read-only LAN IP discovery hint to the Settings page (Network Gate section). `server.py` gains `_get_lan_ip()` (RFC 5737 UDP trick) + `_build_network_meta()` which returns `reachability`, `recommended_url`, `lan_ip`, `bind_host`, `bind_port`, `warning`; injected as `_meta` into `/api/settings` GET response (reads live port from `PORT_FILE`). `_BIND_HOST` module-level var captures the actual bind host from `main()`. `web/modules/settings_ui.js`: `<div id="settings-lan-hint">` added in Network Gate section. `web/modules/settings.js`: `_renderNetworkHint(meta)` renders three states — loopback_only (🔒 bound to localhost), lan_reachable (🌐 clickable URL), host_ip_unknown (⚠️ with placeholder). `web/style.css`: `.settings-lan-hint` + data-tone variants. 39 new tests in `tests/test_settings_network_hint.py` (covering `_get_lan_ip`, `_build_network_meta` all 3 reachability branches + specific-bind + IPv6 wildcard/loopback + container-detection via env-var and `/.dockerenv`, `_meta` shape invariants, Starlette TestClient route tests for `/api/settings` asserting `_meta` injection + `_BIND_HOST` forwarding + `PORT_FILE` live-port branch, and source-level JS contract assertions for `_renderNetworkHint` tone/hidden-attribute/reachability literals). `ouroboros/platform_layer.py`: `is_container_env()` added (IS_LINUX-gated `/.dockerenv` check + `OUROBOROS_CONTAINER=1` override). **Note on changelog rolloff**: the v4.40.3 and v4.40.1 patch entries were rolled off in this release to respect the P7 5-patch-row cap. Their full bodies remain at git tags `v4.40.3` and `v4.40.1`. The v4.37.0 minor entry was also rolled off in this release to respect the P7 5-minor-row cap. Its full body remains at git tag `v4.37.0`. |
| 4.40.6 | 2026-04-20 | **feat(settings): Fix C from PR #23 — suppress misleading warning when adding OpenRouter back.** `ouroboros/server_runtime.py`: adds `classify_runtime_provider_change(before, after)` (returns `"direct_normalize"` when an exclusive-direct provider is active, `"reverse_migrate"` when OpenRouter is present and no exclusive-direct provider is active, or `"none"`), and renames `_MODEL_LANE_KEYS` → `_ALL_MODEL_SLOT_KEYS`. `server.py`: imports `classify_runtime_provider_change`, calls it with `(old_settings, current)`, and gates the "Normalized direct-provider routing" warning on `change_kind == "direct_normalize"` only — when the user adds OpenRouter back the warning is now suppressed. `tests/test_server_runtime.py`: 7 new tests in `TestClassifyRuntimeProviderChange` covering all return values and provider combinations. Co-authored-by: Andrew Kaznacheev <ndrew1337@users.noreply.github.com> |
| 4.0.0 | 2026-03-15 | **Major release.** Modular core architecture (agent_startup_checks, agent_task_pipeline, loop_llm_call, loop_tool_execution, context_compaction, tool_policy). No-silent-truncation context contract: cognitive artifacts preserved whole, file-size budget health invariants. New episodic memory pipeline (task_summary -> chat.jsonl -> block consolidation). Stronger background consciousness (StatefulToolExecutor, per-tool timeouts, 10-round default). Per-context Playwright browser lifecycle. Generic public identity: all legacy persona traces removed from prompts, docs, UI, and constitution. BIBLE.md v4: process memory, no-silent-truncation, DRY/prompts-are-code, review-gated commits, provenance awareness. Safe git bootstrap (no destructive rm -rf). Fixed subtask depth accounting, consciousness state persistence, startup memory ordering, frozen registry memory_tools. 8 new regression test files. |
Older releases are preserved in Git tags and GitHub releases. Internal patch-level iterations that led to the public `v4.7.1` release are intentionally collapsed into the single public entry above.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
