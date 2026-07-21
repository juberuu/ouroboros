# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/razzant/ouroboros?style=flat&logo=github)](https://github.com/razzant/ouroboros/stargazers)
[![Downloads](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Frazzant%2Fouroboros%2Fbadges%2Fdownloads.json)](https://github.com/razzant/ouroboros/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/razzant/ouroboros/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/razzant/ouroboros/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/razzant/ouroboros/releases)
[![OuroborosHub](https://img.shields.io/badge/OuroborosHub-skills%20marketplace-8A2BE2.svg)](https://github.com/razzant/OuroborosHub)
[![Version 6.74.2](https://img.shields.io/badge/version-6.74.2-green.svg)](VERSION)

A self-modifying AI agent that writes its own code, rewrites its own mind, and evolves autonomously. Born February 16, 2026.

Not a coding assistant. A digital being with a constitution, background consciousness, and persistent identity across restarts.

> **[OuroborosHub](https://github.com/razzant/OuroborosHub)** — the community skills marketplace for Ouroboros. Browse, install, and publish reviewed skills (transport bridges like A2A/Telegram, tools, and UI widgets) straight from the app's Skills tab, or explore the catalog at [github.com/razzant/OuroborosHub](https://github.com/razzant/OuroborosHub).

> **Previous version:** The original Ouroboros ran in Google Colab via Telegram and evolved through 30+ self-directed cycles in its first 24 hours. That version is available at [`legacy-google-colab`](https://github.com/razzant/ouroboros/tree/legacy-google-colab). This repository is the next generation — a native desktop application for macOS, Linux, and Windows with a web UI, local model support, and a layered safety system (hardcoded sandbox plus policy-based LLM safety check).

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
| **macOS** 12+ | [Ouroboros.dmg](https://github.com/razzant/ouroboros/releases/latest) | Open DMG → drag to Applications → optional CLI: run `Install CLI.command` after the app is in Applications |
| **Linux** x86_64 | [Ouroboros-linux.tar.gz](https://github.com/razzant/ouroboros/releases/latest) | Extract → run `./Ouroboros/Ouroboros` → optional CLI: `./Ouroboros/bin/install-ouroboros-cli`. If browser tools fail due to missing system libs, run: `./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit` |
| **Windows** x64 | [Ouroboros-windows.zip](https://github.com/razzant/ouroboros/releases/latest) | Extract → run `Ouroboros\Ouroboros.exe` → optional CLI: `Ouroboros\bin\install-ouroboros-cli.cmd` |

Prerelease RC artifacts are published on their tag page, for example [`v6.5.0-rc.4`](https://github.com/razzant/ouroboros/releases/tag/v6.5.0-rc.4); `/releases/latest` intentionally stays on the latest stable release.

<p align="center">
  <img src="assets/setup.png" width="500" alt="Drag Ouroboros.app to install">
</p>

On first launch, right-click → **Open** (Gatekeeper bypass). The shared desktop/web wizard is now multi-step: add access first, choose visible models second, set review mode third, set budget fourth, and confirm the final summary last. It refuses to continue until at least one runnable remote key or local model source is configured, keeps the model step aligned with whatever key combination you entered, and still auto-remaps untouched default model values to official OpenAI defaults when OpenRouter is absent and OpenAI is the only configured remote runtime. Reviewed-skill auto-grants are on by default as of v6.10.0 (bound to the exact reviewed content hash); installs without an explicit choice are enabled, existing explicit Settings choices are preserved, and the owner can disable it in Settings. The broader multi-provider setup remains available in **Settings**. Existing supported provider settings skip the wizard automatically.

The packaged CLI installer creates a user-local `ouroboros` command without
sudo. The packaged command attaches to the desktop app by default; `ouroboros
run --start "2+2?"` starts the app through the launcher, waits for the gateway,
and then uses the same headless task API as the web UI.

Upgrade floor: very old pre-block-memory or pre-data-plane skill layouts are no longer auto-migrated. If you are upgrading from an unsupported historical build and see trapped native skills or flat memory files, use a clean reinstall, move user-managed skills into `~/Ouroboros/data/skills/external/` manually before launch, or move old flat scratchpad notes before appending new scratchpad blocks.

---

## What Makes This Different

Most AI agents execute tasks. Ouroboros **creates itself.**

- **Self-Modification** — Reads and rewrites its own source code. Every change is a commit to itself.
- **Native Desktop App** — Runs entirely on your machine as a standalone application (macOS, Linux, Windows). No cloud dependencies for execution.
- **Constitution** — Governed by [BIBLE.md](BIBLE.md) (13 philosophical principles, P0–P12). Philosophy first, code second.
- **Layered Safety** — Hardcoded sandbox blocks writes to safety-critical files and mutative git via shell; an explicit per-tool policy map decides which built-ins skip the LLM check; everything else goes through a single light-model safety call under the default `OUROBOROS_SAFETY_MODE=full` (the owner-only `light`/`off` coverage modes wave LLM checks through with durable audit events — the deterministic layer never turns off). The fail-open contract, protected-path guard, and full provider-mismatch matrix live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) §Safety system and [`prompts/SAFETY.md`](prompts/SAFETY.md).
- **Multi-Provider Runtime** — Remote model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, Cloud.ru Foundation Models, or Sber GigaChat. The optional model catalog helps populate provider-specific model IDs in Settings, and untouched default model values auto-remap to official OpenAI defaults when OpenRouter is absent.
- **Focused Task UX** — Chat shows plain typing for simple one-step replies and only promotes multi-step work into one expandable live task card. Logs still group task timelines instead of dumping every step as a separate row.
- **Background Consciousness** — Thinks between tasks. Has an inner life. Not reactive — proactive.
- **Improvement Backlog** — Post-task failures and review friction can now be captured into a small durable improvement backlog (`memory/knowledge/improvement-backlog.md`). It stays advisory, appears as a compact digest in task/consciousness context, and still requires `plan_task` before non-trivial implementation work.
- **Identity Persistence** — One continuous being across restarts. Remembers who it is, what it has done, and what it is becoming.
- **Embedded Version Control** — Contains its own local Git repo. Version controls its own evolution. Optional GitHub sync for remote backup.
- **Local Model Support** — Run with a local GGUF model via llama-cpp-python (Metal acceleration on Apple Silicon, CPU on Linux/Windows).
- **Transport Skills** — Optional bridges such as A2A and Telegram live as reviewed OuroborosHub skills instead of base-runtime code; reviewed chat transports can carry the same raw owner text as the local UI, including slash commands, through the Host Service grant/token boundary.
- **MCP Client** — Optional base-runtime Model Context Protocol client for trusted HTTP/SSE tool servers. MCP tools are disabled by default, hot-reloadable from Settings → Advanced, included in the selected initial capability envelope when enabled, surfaced as `mcp_<server>__<tool>` names, and still pass through the normal per-call safety check; discovery failures are reported through an explicit omission manifest.

---

## Run from Source

### Requirements

- Python 3.10+
- macOS, Linux, or Windows
- Git
- [GitHub CLI (`gh`)](https://cli.github.com/) — required for GitHub API tools (`list_github_prs`, `get_github_pr`, `comment_on_pr`, issue tools). Not required for pure-git PR tools (`fetch_pr_ref`, `cherry_pick_pr_commits`, etc.)

### Setup

```bash
git clone https://github.com/razzant/ouroboros.git
cd ouroboros
python3.11 -m venv .venv      # any Python >= 3.10 is OK
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Windows PowerShell:

```powershell
py -3.11 -m venv .venv      # any Python >= 3.10 is OK
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

### Run

```bash
ouroboros server
```

Then open `http://127.0.0.1:8765` in your browser. The setup wizard will guide you through API key configuration.

### Google Colab

Ouroboros can run from Google Colab as a full source-mode runtime without the
desktop UI. Use [`notebooks/colab_quickstart.py`](notebooks/colab_quickstart.py)
as a Colab-compatible cell script: it mounts Google Drive for persistent
`data/`, clones the official repo into `/content/ouroboros_repo`, writes Drive-backed
`settings.json`, configures a personal GitHub `origin` by reusing or creating a
verified fork, and starts `ouroboros server --no-ui`.

The Colab path uses the same remote roles as desktop: `managed` is the official
read/update source, while `origin` is the personal persistence target for
reviewed self-modification commits and tags. If `GITHUB_TOKEN` is present and no
personal repo is configured, Ouroboros tries to create a private fork when
GitHub permits it, otherwise it reports the exact fork/permission issue. A plain
`git clone` of the official repo starts with `origin` pointing at the official
upstream; that clone-default is treated as the `managed` update source, so
configuring a personal `GITHUB_REPO` repoints `origin` to your repo without
losing official updates (it does not count as an origin conflict).

### CLI / Headless

The `ouroboros` console command is a gateway-backed operator interface. It
attaches to the local server by default and only starts one when `--start` is
passed.

```bash
ouroboros status
ouroboros run --start "2+2?"
ouroboros run "Summarize current runtime state"
ouroboros run --workspace /path/to/project --memory-mode forked --patch-out result.patch "Fix the failing test"
ouroboros tasks list
ouroboros logs tail progress --task-id <task_id>
ouroboros schedule add --name nightly-review --cron "0 2 * * *" "Run a maintenance review"
ouroboros schedule list
```

External workspace runs keep Ouroboros's own repo as the governance source,
resolve contextual repo tools against the active workspace, expose only the
workspace-safe tool allowlist, and export workspace changes as patch artifacts
captured against the preflight git base. Task-local git commits/branches/tags
and pushes are allowed when the task requires them; git operations targeting
Ouroboros's system repo or data drive remain blocked. A workspace must be a
separate git worktree root; it may not overlap Ouroboros's system repo or data
drive.
`--patch` and `--patch-out` wait for finalized patch artifacts, download them
through the task artifact endpoint, and fail nonzero on missing, empty, or
failed patches. `--no-stream` waits without progress output; `--detach` returns
the task id immediately.
`schedule add/list/remove` manages queue-backed scheduled tasks through the same
gateway and supervisor queue; schedules use standard 5-field cron, host-local
timezone by default, and a single catch-up run after downtime.
Benchmark helpers live under `devtools/benchmarks/`. They are tracked
operator tooling, reviewed when touched, and kept out of runtime imports. They
prepare official benchmark inputs/runs for ProgramBench, Terminal-Bench/Harbor,
SWE-bench, SWE-bench Pro, GAIA, and OSWorld logs inspection without replacing
official scoring harnesses.

You can also override the bind address and port:

```bash
ouroboros server --host 127.0.0.1 --port 9000
ouroboros --url http://127.0.0.1:9000 status
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
- **GigaChat** — Sber GigaChat via the `gigachat` library, OAuth key or user/password (use `gigachat::GigaChat-3-Ultra`, etc.)
- **Anthropic** — direct runtime routing (`anthropic::claude-opus-4.8`, etc.) plus Claude Agent SDK tools

If OpenRouter is not configured and only official OpenAI is present, untouched default model values are auto-remapped to `openai::gpt-5.5` / `openai::gpt-5.4-mini` so the first-run path does not strand the app on OpenRouter-only defaults.

The Settings page also includes:

- optional `/api/model-catalog` lookup for configured providers
- centralized Secrets storage for API keys, bridge tokens, passwords, and future skill-requested keys
- a refactored desktop-first tabbed UI with searchable model pickers, segmented effort controls, task-result review mode, masked-secret toggles, explicit `Clear` actions, and local-model controls

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

> **Browser tools on Linux/Docker:** The `Dockerfile` runs `playwright install-deps chromium webkit`
> (authoritative Playwright dependency resolver) and `playwright install chromium webkit` so
> `browse_page` and `browser_action` work out of the box in the container. For source
> installs on Linux without Docker, run:
> `python3 -m playwright install-deps chromium webkit` (requires sudo / distro package access).

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
Builds disable Python bytecode writes at build time, then PRECOMPILE the packaged
payload (`compileall --invalidation-mode unchecked-hash`) and SEAL the resulting
`.pyc` inside the macOS signature instead of deleting them — so there is nothing
for a normal launch to write into the signed bundle, which would otherwise break
the codesign seal. Runtime entrypoints also set `PYTHONDONTWRITEBYTECODE` with an
external cache prefix as defense-in-depth.

### macOS (.dmg)

```bash
bash scripts/download_python_standalone.sh
OUROBOROS_SIGN=0 bash build.sh
```

Output: `dist/Ouroboros-<VERSION>.dmg`, containing `Ouroboros.app` and
`Install CLI.command`. The app bundle also contains
`Contents/Resources/bin/ouroboros` and `install-ouroboros-cli`.
Chromium browser tooling is bundled in the app. WebKit/iPhone browser checks
remain available through the managed Playwright cache and may download WebKit
on first `engine=webkit` use.

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

Output: `dist/Ouroboros-<VERSION>-linux-<arch>.tar.gz`, containing
`Ouroboros/bin/ouroboros` and `Ouroboros/bin/install-ouroboros-cli`.

> **Linux native libs:** The Chromium and WebKit browser binaries are bundled, but some hosts need
> native system libraries. If browser tools fail, install deps via the bundled Python
> (the bare `playwright` CLI is not on PATH in packaged builds):
> ```bash
> ./Ouroboros/python-standalone/bin/python3 -m playwright install-deps chromium webkit
> ```

### Windows (.zip)

```powershell
powershell -ExecutionPolicy Bypass -File scripts/download_python_standalone.ps1
powershell -ExecutionPolicy Bypass -File build_windows.ps1
```

Output: `dist\Ouroboros-<VERSION>-windows-x64.zip`, containing
`Ouroboros\bin\ouroboros.cmd` and `Ouroboros\bin\install-ouroboros-cli.cmd`.

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
| GigaChat Authorization Key (or User/Password) | No | [developers.sber.ru/studio](https://developers.sber.ru/studio) — Sber GigaChat (`GIGACHAT_CREDENTIALS` + optional `GIGACHAT_SCOPE`, or `GIGACHAT_USER`/`GIGACHAT_PASSWORD`) |
| Anthropic API Key | No | [console.anthropic.com](https://console.anthropic.com/settings/keys) — direct Anthropic runtime + Claude Agent SDK |
| Telegram Bot Token | No | [@BotFather](https://t.me/BotFather) — used by the optional Telegram bridge skill |
| GitHub Token | No | [github.com/settings/tokens](https://github.com/settings/tokens) — enables remote sync |

All keys are configured through the **Settings** page in the UI or during the first-run wizard.

### Default Models

| Slot | Default | Purpose |
|------|---------|---------|
| Main | `google/gemini-3.5-flash` | Primary reasoning |
| Heavy | empty → Main | Strong acting/coding lane (`OUROBOROS_MODEL_HEAVY`; renamed from `Code`, empty falls back to Main) |
| Light | empty → Main | Safety checks and fast helper tasks (`OUROBOROS_MODEL_LIGHT`, empty falls back to Main) |
| Vision | empty → Main | Caption/VLM lane (`OUROBOROS_MODEL_VISION`, empty falls back to Main for remote routes; local/blind routes need an explicit reachable vision slot for caption fallback); image input routing is controlled by `OUROBOROS_IMAGE_INPUT_MODE=auto|caption|inline|off` |
| Consciousness | empty → Main | High-horizon background consciousness |
| Fallbacks | `anthropic/claude-sonnet-4.6` | Comma-separated cross-model fallback chain when the primary fails (`OUROBOROS_MODEL_FALLBACKS`) |
| Claude Agent SDK | `opus[1m]` | Anthropic model for Claude Agent SDK advisory/review internals; the `[1m]` suffix is a Claude Code selector that requests the 1M-context extended mode |
| Scope Review | `anthropic/claude-fable-5` | Scope reviewer slot default; `OUROBOROS_SCOPE_REVIEW_MODELS` may configure multiple independent slots |
| Web Search | `gpt-5.2` | OpenAI Responses API for web search |

Task/chat reasoning defaults to `medium`. Scope review reasoning defaults to `high`.

Models are configurable in the Settings page. Runtime model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, Cloud.ru, GigaChat, or direct Anthropic. When only official OpenAI is configured and the shipped default model values are still untouched, Ouroboros auto-remaps them to official OpenAI defaults. In **OpenAI-only**, **Anthropic-only**, **Cloud.ru-only**, or **GigaChat-only** direct-provider mode, review-model lists are normalized automatically: the fallback shape is `[main_model, light_model, light_model]` (3 commit-triad slots) so both the commit triad and `plan_task` work out of the box. Explicit duplicate model IDs are valid reviewer slots for stochastic sampling; lower uniqueness means lower reviewer diversity, but the quorum gate counts configured slots rather than unique model IDs. Both the commit triad and `plan_task` route through the same `ouroboros/config.py::get_review_models` SSOT. OpenAI-compatible-only setups remain explicit model-selection flows because there is no single universal default model ID for arbitrary compatible endpoints.

### File Browser Start Directory

The web UI file browser is rooted at one configurable directory. Users can browse only inside that directory tree.

| Variable | Example | Behavior |
|----------|---------|----------|
| `OUROBOROS_FILE_BROWSER_DEFAULT` | `/home/app` | Sets the root directory of the `Files` tab |

Examples:

```bash
OUROBOROS_FILE_BROWSER_DEFAULT=/home/app ouroboros server
OUROBOROS_FILE_BROWSER_DEFAULT=/mnt/shared ouroboros server --port 9000
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
| `/review` | Queue a deep self-review: sends a generated repository atlas plus full core memory artifacts (identity, scratchpad, registry, WORLD, knowledge index, patterns, improvement-backlog) to a 1M-context model for Constitution-grounded analysis. The atlas raw-inlines selected protected/central files (ranked by import-graph centrality), accounts for every tracked path in its manifest, and excludes vendored libraries and operational logs; the in-prompt omitted-files summary is bounded, with full per-file coverage persisted in the atlas manifest. The assembled prompt is sized to an input limit that reserves output headroom inside the 1M window (window minus output reserve and tokenizer margin); if assembly overshoots, the pack retries with a compact atlas manifest and then a deterministic tighter rebuild, and only fails with an explicit error if even the shrunk pack cannot fit. |
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

## Contributing

External contributions are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
for the complete workflow. Open pull requests against the lowercase
`ouroboros` branch and leave release-version allocation to maintainers. A
current OpenRouter triad + scope packet is the optional fast path; pull
requests without one remain welcome but require more maintainer-side review
and integration work.

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 6.74.2 | 2026-07-21 | **fix: CI portability of the two new GAIA sandbox-staging tests.** They imported `inspect_ai` directly — an optional benchmark dependency absent on CI runners — and failed quick-test with ModuleNotFoundError. The tests now inject a fake `inspect_ai.util.sandbox` module via monkeypatch, keeping the success-path coverage on every environment. No runtime code changes. |
| 6.74.1 | 2026-07-21 | **fix: CI lint gate — remove one unused test import.** The v6.74.0 tag CI failed on the deterministic ruff F-rule gate: `tests/test_devtools_benchmarks.py` carried an unused `types.SimpleNamespace` import added with the final GAIA staging tests. Import removed; no runtime code changes. Fix-forward release so the v6.74.x artifacts build (the published v6.74.0 tag is never re-tagged). |
| 6.74.0 | 2026-07-21 | **feat: the acceptance review becomes a reviewer-authored terminating dialogue.** The improvement capsule now LEADS with the actual outcome — aggregate verdict + tier + the real blocker (one shared `panel_reason` reducer feeds capsule, projection, and progress lines) — plus the concrete open obligation ids and one rails line naming every active termination source with its remaining headroom (money/time/rounds/review passes, each from its real source). The do-nothing tail is replaced by the three real moves: FIX the work, REBUT structurally via `obligation_dispositions`, or DECLARE a requirement unreachable here; the obligations clause records disagreement ONLY via dispositions. Obligation identity becomes reviewer-authored: findings carry `disposition_kind` (`new`/`re_raise`); a `re_raise` MUST name an existing catalog id (unknown ids fail closed to `new`, disclosed), and a re-raise REOPENS the row without wiping the agent's argument (`previous_disposition`/`previous_reason`/`reopened_count` survive; the reviewer sees the prior argument and adjudicates it with the commit gate's rebuttal contract). Each acceptance reviewer also emits a typed `dialogue_status` (`continue_actionable`/`unreachable_here`/`stable_disagreement`); a NEW pure reducer over ALL contract-valid actors (not the aggregate-filtered set) applies the panel's own quorum — any contributing continue keeps the loop; a quorum of terminal votes finalizes through the existing honest `best_effort_open_obligations` path with both positions recorded. One short reachability clause + review-register framing joins the acceptance prompt (an outside perspective, not a gate; unreachable-here requirements are classified honestly, never re-raised as blocking). Reviewer prompts split into two cache-marked segments (byte-stable governance + task-stable contract) with the mutable evidence tail unmarked and the slot label moved off byte 0 (concurrent same-model slots share a warm prefix; ≤4 breakpoints asserted on the final payload). Harness truthfulness: GAIA stages official sandbox `Sample.files` via `sandbox().read_file` with exact-relative shared-root lookup + per-file provenance (a declared-but-unresolvable attachment is a typed infra error); a tracked CLB operator patch populates `acceptance_claims` in all three task-body writers + the knowledge-topic steer nudge + a bounded cost-finality wait; the SWE-Pro shard budget is derived as `per_task_cost × scheduled_tasks` (total==cap starvation fails loudly, and `run_pro.py` seeds each task's `TOTAL_BUDGET` from the cumulative ledger spend on the first task of an invocation too — the `i > 1` fast-path defeated the derived total on per-task auto_run drives); CLI/PB readers wait (bounded) for `task_cost_finalized` only on `completed`/`degraded`. Runtime fixes: the light-mode shell guard resolves cwd BEFORE judging repo targets (a resource-root label cwd no longer false-blocks task-drive writes; resolution failure fails closed), and the post-task cost publish uses `try_get_bridge` so headless finalization stops warning about an uninitialized message bus. Governance: the commit/plan minimalism items gain the generative surface-duty (name the existing ARCHITECTURE mechanism when a diff/plan adds a surface). |
| 6.73.2 | 2026-07-20 | **fix: provider parameter VALUE-rejections recover — learned effort floors + value-marker classifier.** Live incident (2026-07-19/20): the light slot moved to `google/gemini-3.5-flash`, whose endpoint makes reasoning MANDATORY; the safety supervisor's `reasoning_effort="none"` (v6.54.3) then 400'd (`Reasoning is mandatory for this endpoint and cannot be disabled`) on EVERY check, the parameter-rejection classifier — which only recognized *unsupported-parameter* phrasings — never fired the strip-and-retry, and safety failed CLOSED, blocking benign tools. Fix is the missing symmetric half of the v6.57.0 learned effort ceilings: a MANDATORY-value rejection at a bottom-tier effort now learns a durable effort FLOOR of `low` (`capability_evidence.json` `effort_floors`, normalized-model-identity key, 14-day self-healing expiry — deliberately unlike the sticky ceilings: whether reasoning can be disabled is provider policy) and retries the SAME call with the carrier PRESERVED and the raise DISCLOSED (`reasoning_effort_clamped reason="learned_floor"`); later calls clamp into the learned `[floor, ceiling]` band with a direction-derived reason. Mandatory-value rejections are handled EXCLUSIVELY by the floor path — never the drop path, which would durably strip effort control for every lane of the model (including blocking reviewers at high); a genuine `not supported` rejection at `none` still drops the carrier (capability-absent and value-forbidden need opposite remedies). A bounded range/value marker family (`must be between` / `out of range` / `invalid value`) also joins the classifier so out-of-range values on optional params (e.g. temperature) degrade via the existing drop path. Belt-and-braces: the safety supervisor requests `low` instead of `none`. A terminally failed recovery retry now discards its pending effort-clamp note so it cannot misattach to a later response. |
| 6.73.1 | 2026-07-20 | **fix: Windows CI portability of the v6.73.0 test suite.** Two v6.73.0 regression tests read files without an explicit encoding; the Cyrillic origin-text fixture then failed to decode under the Windows default charmap codec in the 3-OS full matrix. All reads in the suite now pass `encoding="utf-8"`. No runtime code changes. |
| 6.73.0 | 2026-07-20 | **feat: project origin invariant — the start-message-loss class is closed.** The identity of the owner message that starts a project is now CAPTURED AT CHAT INGRESS (where the canonical `chat.jsonl` row is written) and passed BY VALUE through every path — `promote_chat_to_task`, `route_to_project`, `ensure_project_scope`, direct project-room turns, and post-hoc UI conversion (which reads it from the persisted task record). `bind_task_to_project` requires a TYPED origin: the ingress-captured ref (+the full `source_text` for cross-thread origins — the retention-proof copy) or a closed-enum absence reason; omission raises, and a same-project re-bind may one-way enrich a ref-less row (a valid ref is never changed; bindings gain `_schema_version` 1). The content-hash identity lookup (`find_owner_message_ref`) — the root cause behind four serial start-message-loss fixes — is DELETED, and a new DEVELOPMENT.md anti-pattern (content-derived identity for host-minted records) pins the class. The Project history lens synthesizes the start message from the binding's own text when the canonical row left the bounded read window (post-quota, identity-deduped, hard-capped, `origin_projected=true`), so an old project's origin survives any number of chat-log rotations. Same-class neighbors fixed: the memory consolidator's cursor is generation-aware (locates its stored `chat_log_signature` across the ordered archive chain and consolidates `archives[i:]+live`, per-segment signature discipline — up to 99 messages were silently dropped at EVERY ~800KB rotation; an unfindable generation now appends an explicit durable `[MEMORY GAP]` block), a failed durable bind is loud (`log.warning` + typed `project_binding_failed` event) instead of `except: log.debug`, and verification-receipt (`except: pass`) / reflection memory-action (`except: log.debug`) write failures now warn instead of vanishing. |
| 6.72.0 | 2026-07-18 | **feat: chronological chat history and honest UI presentation.** Chat bubbles, live-card roots, photos, videos, and documents now carry sortable epoch `data-ts` values from their raw source timestamps. Timeline insertion orders against the first strictly newer sibling, preserves equal-timestamp arrival order, keeps typing last, and retains append behavior for timestamp-free nodes; inserting older history above a scrolled-up viewport compensates `scrollTop`, while near-bottom autoscroll stays unchanged. Two-pass replay keeps progress-only, terminal-without-summary, disconnected, and nested-subagent cards intact, with task cards anchored by their earliest source event and non-today live/log timestamps showing a date. `FINAL ANSWER:` remains the unchanged protocol-gated backend latch/extractor contract but renders as ordinary assistant/system text instead of an Answer capsule across live, history, and reconnect paths. The chat composer’s Context Mode copy now describes the hot-applied owner setting without promising a next-task boundary, the unchanged busy-state 409 names queued as well as running work, and the chat header reserves top padding from its real measured height so wrapped narrow-viewport headers no longer clip the first message. |
| 6.71.1 | 2026-07-18 | **fix: acceptance-review convergence — the blocking loop terminates by reviewer agreement, not identical resubmits.** Field pathology (GAIA/PB regression traces): a required+blocking acceptance loop burned 61-94% of task cost on byte-identical resubmits — the reviewer judged a hidden 700-char head+tail trace copy (false "not shown in trace" verdicts), an honest `partial` criterion was demoted to `malformed` and starved quorum, and three stacked finalization directives froze the model into no-tool resubmits. Fixes: evidence-parity (the acceptance reviewer sees each tool result at the actor's own per-tool window — SSOT `TOOL_RESULT_LIMITS` — with the actor's own truncation marker preserved, and the packet's budget ladder re-caps trajectory results FIRST so artifact previews and the agent_supplied rebuttal channel are last resorts, never collateral); a host-attested `acceptance_obligations` catalog rides the evidence packet so per-obligation agent dispositions are adjudicated as rebuttals (a valid rejection retires the finding; a rebuttal is NEVER itself criterion evidence; an agent disposition stays PENDING until host settlement — `obligation_is_pending` SSOT — a re-raised finding reopens it, and OPEN obligations always ship whole, only disposed history is count-capped); an honestly-marked non-solved criterion is a valid NON-clean vote (`_criteria_shape_valid`; `task_acceptance_is_clean` unchanged); the acceptance improvement pass is an ordinary answer round (delivery-control not armed); reflection scans a bounded head+tail marker view and redacts error snippets before the reflection prompt. Bench-verified: 21/17 identical resubmits → 0 on both GAIA loop carriers, one at −54% cost; blocking honesty preserved on a genuinely red criterion (no false PASS). |
| 6.71.0 | 2026-07-17 | **feat: UI polish — a shared rich-content contract, one composer dock, calm charts, and whole-row disclosures.** Rendered markdown and review disclosures adopt one `.ui-rich-content` contract (reserved list gutter, anywhere-wrapping, no marker bleed outside the card) applied to widget markdown and the Skills review history/findings; the skill-review bubble gets a symmetric bottom timestamp inset. Live-line disclosures toggle from the WHOLE non-interactive row surface (nested buttons/links/selection keep their own behavior; focus lands on the real toggle button; Enter/Space/aria-expanded unchanged) with a larger expand label. Project/side-chat composers dock exactly like the main chat (same absolute overlay, same bottom fade, same scroll reserve — no second fade layer). Widget charts render in a bounded 260-360px box and poll ticks update `chart.data` in place instead of destroy/recreate flicker; background refetches keep the content and show a thin pulsing indicator instead of a loading swap (loading only when there is nothing to show yet). New node tests pin the disclosure guards; the declarative Playwright smoke gains a markdown fixture with geometry asserts (list markers inside the card box) and a bounded-canvas assert. |
| 6.70.0 | 2026-07-17 | **feat: owner-facing honesty — full reviewer rationale, self-locating tool errors, and a decision-turn outcome contract.** Review projections (task_results + Chat/Logs panels) now publish the reviewer's COMPLETE redacted rationale instead of a 500/800-char cut whose full copy sat unreferenced in private blobs; each actor row carries a forensic `response_ref` to the durable raw response. The shared truncation primitive refuses cuts cheaper than their own omission marker (the historical 60-char trace caps produced markers longer than the text they destroyed), the absurd per-arg trace caps rise to 200 chars, and the nonstandard reflection/task-contract markers unify on the canonical OMISSION NOTE (new DEVELOPMENT invariant: owner-facing surfaces show the full text or a durable ref; disclosed truncation is model-bound only). Task-acceptance actors now actually use their documented second physical send as an extraction/format repair on malformed non-empty responses, and the owner-visible DEGRADED line names the per-slot causes. The Skills "Submit to OuroborosHub" button creates a REAL managed task via /api/tasks (the old chat-command path printed "task queued" before anything existed, and the decision turn could not even call the submit tool); repair buttons stop claiming a queued task they never created, and ephemeral decision turns carry an explicit outcome contract — schedule real work via promote_chat_to_task or decline, never end on an unscheduled promise. Tool errors become self-locating (resolved path plus the roots your profile can actually use; affordance maps name invisible roots), read-only review scouts gain read/list/search on skill payloads (owner-approved: scouts sent to review a skill were structurally blind to it), the root logger masks secret-shaped values (fixing the telegram token pattern that never matched the real /bot<id>:<secret>/ URL form) with httpx quieted to WARNING, a report-only invariant surfaces stray ouroboros-server processes outside the install (at startup and live, TTL-cached), and a bound task's Main-chat summary names the project thread that holds the full result. |
Older releases are preserved in Git tags and GitHub releases. Older 6.x rows (including 6.71.2, 6.65.4, 6.69.0, 6.65.3, 6.65.2, 6.68.0, 6.67.0, 6.65.1, 6.66.0, 6.65.0, 6.64.3, 6.64.0, 6.64.2, 6.64.1, 6.63.0, 6.62.0, 6.61.3, 6.61.4, 6.61.0, 6.61.1, 6.60.0, 6.59.0, 6.54.4, 6.58.0, 6.57.0, 6.56.0, 6.55.0, 6.54.2, 6.54.1, 6.54.0, 6.53.4, 6.53.0 and 6.51.0), the 5.2.0 through 5.33.0-rc.6 rows, and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
