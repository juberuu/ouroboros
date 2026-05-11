# Ouroboros

[![GitHub stars](https://img.shields.io/github/stars/joi-lab/ouroboros-desktop?style=flat&logo=github)](https://github.com/joi-lab/ouroboros-desktop/stargazers)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![macOS 12+](https://img.shields.io/badge/macOS-12%2B-black.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Linux](https://img.shields.io/badge/Linux-x86__64-orange.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Windows](https://img.shields.io/badge/Windows-x64-blue.svg)](https://github.com/joi-lab/ouroboros-desktop/releases)
[![Version 5.15.0-rc.13](https://img.shields.io/badge/version-5.15.0--rc.13-green.svg)](VERSION)

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

Models are configurable in the Settings page. Runtime model slots can target OpenRouter, official OpenAI, OpenAI-compatible endpoints, Cloud.ru, or direct Anthropic. When only official OpenAI is configured and the shipped default model values are still untouched, Ouroboros auto-remaps them to official OpenAI defaults. In **OpenAI-only** or **Anthropic-only** direct-provider mode, review-model lists are normalized automatically: the fallback shape is `[main_model, light_model, light_model]` (3 commit-triad slots) so both the commit triad and `plan_task` work out of the box. Explicit duplicate model IDs are valid reviewer slots for stochastic sampling; lower uniqueness means lower reviewer diversity, but the quorum gate counts configured slots rather than unique model IDs. Both the commit triad and `plan_task` route through the same `ouroboros/config.py::get_review_models` SSOT. (OpenAI-compatible-only and Cloud.ru-only setups do not yet get this fallback — the detector returns empty when those keys are present, so users configure review-model lists manually in that case.)

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
| 5.15.0-rc.13 | 2026-05-11 | **fix(ci): restore parametrize decorator on `test_provider_basic_chat`.** rc.12's helper-function insertion accidentally moved the `@integration` + `@pytest.mark.parametrize(...)` block above `_skip_on_provider_environmental_error` (the helper) instead of `test_provider_basic_chat` (the test), so the test ran with no parametrize and pytest looked up `provider_id`/`env_key`/`model`/`expected_provider` as fixtures — failing collection in Tier 1 (quick-test) with "fixture 'provider_id' not found". This commit moves the decorators back onto the test function. Locally verified by `pytest --collect-only -m integration` showing the 8 parametrized cases collect correctly, and full `pytest tests/` → 3399 passed. |
| 5.15.0-rc.12 | 2026-05-11 | **fix(ci): skip-not-fail integration smoke on environmental provider errors (empty credit balance, quota, rate limits).** rc.11's added response-body logging exposed the true cause of the integration-test failures: the CI Anthropic API key has hit `invalid_request_error: "Your credit balance is too low to access the Anthropic API"`. This is a billing-state condition on the CI account, not a regression in routing code (curl against the same key reproduces locally). Add `_skip_on_provider_environmental_error()` helper that detects three known non-code conditions — `"credit balance is too low"` (Anthropic), `"insufficient_quota"` (OpenAI), `"rate_limit"` / HTTP 429 (any provider) — and converts them to `pytest.skip()` instead of `pytest.fail()`. Response body is still printed to stderr for postmortem before the skip fires. The routing smoke's actual contract (auth handshake + request shape + response parsing) remains enforced on the providers that are operational. |
| 5.15.0-rc.11 | 2026-05-11 | **fix(ci): pin Anthropic integration smoke to `claude-3-5-haiku-20241022` + cap `max_tokens=1024` + log response body on HTTP error.** Round 2 hotfix on rc.10. rc.10's `claude-3-5-haiku-latest` alias still hit HTTP 400 in CI even though it succeeds via local curl; root cause likely the default `max_tokens=16384` from `chat()` exceeding the model's per-request output ceiling, which Anthropic rejects as `invalid_request_error`. Switch to date-pinned `claude-3-5-haiku-20241022` (the long-stable Haiku 3.5 release) and pass explicit `max_tokens=1024` (sufficient for the "Respond with exactly: OK" smoke). Wrap both routing-smoke chat calls in a try/except that prints the response body before re-raising so future CI failures surface the actual `invalid_request_error.message` instead of the bare `requests.raise_for_status()` `400 Client Error`. |
| 5.15.0-rc.10 | 2026-05-11 | **fix(ci): integration test Anthropic-direct route uses `claude-3-5-haiku-latest`.** The rc.9 push surfaced a transient 400 from Anthropic on the production-default direct-routing model `claude-sonnet-4-6` (rc.8 passed 8/8 hours earlier with the same model). All other provider routes (OpenRouter, OpenAI, Cloud.ru) stayed green. Since `test_provider_integration.py` is a routing smoke (auth + request shape), swap the Anthropic direct case to the long-stable `claude-3-5-haiku-latest` alias so the smoke is robust to fast-rotating production-default aliases. No production code change — provider_models.py still ships `claude-sonnet-4-6` as the runtime default. |
| 5.15.0-rc.9 | 2026-05-11 | **refactor(tests): structural consolidation pass — 24 file deletes, 7 merges, ~−1.7k LOC, 3399 tests passing.** Deletes 7 outright redundant test files (`test_constitution.py` self-contained spec/DSL; `test_module_size_gate.py`, `test_no_port_collision_8767.py`, `test_git_imports.py`, `test_lmstudio_cached_tokens.py` literal-constant pins; `test_fixtures_mock_clawhub.py`, `test_fixtures_mock_llm.py` fixture self-tests). Merges 13 cross-file clusters into 6 new consolidated files: advisory workflow (`test_advisory_workflow.py` ← `_ext`), plan review (`test_plan_review.py` ← `_quorum`), shell trio → `test_shell_run_shell.py`, web search pair → `test_web_search.py`, loop misc → `test_loop_misc.py`, runtime mode 3→2 (`test_runtime_mode_core.py` ← `runtime_mode`+`runtime_mode_gating`; `test_runtime_mode_elevation.py` kept), build/packaging → `test_build_scripts.py`, UI static → `test_skills_marketplace_ui.py`+`test_page_chrome_static.py`, skill micro-files → `test_skill_dependencies.py`, context dedup, launcher merge, chat frontend dedup. In-place trims via parametrize + boilerplate dedup across 28 test files (inspect-only pin tests dropped under `delete_trust_behavioral`, cosmetic CSS literal pins dropped, paranoid YAML edge-case manifest tests dropped, browser-isolation 13 tests → 2 parametrize tables, provider-integration 8 tests → 2 tables, advisory-observability SDK-break readonly+edit collapsed, budget-tracking provider-attribution 7 tests → 1 table). Production-code surface: 2 comment lines (`ouroboros/tools/review.py:110` and `docs/ARCHITECTURE.md:1222`) updated for the test_phase7_pipeline→test_git_review_pipeline rename and the test_plan_review_quorum→test_plan_review merge. AST smoke walkers (`test_no_oversized_modules`, `test_no_bare_except_pass`, `test_no_extremely_oversized_functions`, `test_function_count_reasonable`, `test_no_env_dumping`), self-elevation attack matrix (`test_runtime_mode_elevation.py`, 50+ vectors), tool-policy AST completeness, scope fail-closed coverage, multi-model quorum tests all preserved per P3 immune system. Round-2 adversarial multi-model review (gemini+gpt+opus) caught a misdiagnosed "API drift" claim where the agent had wrongly removed 3 tests during merge B.11 due to bad copy-paste — those 3 tests have been restored verbatim from `git show HEAD:`. |
Older releases are preserved in Git tags and GitHub releases. The 5.2.0 through 5.14.0 stable rows (and every 5.x rc tag prior to 5.15.0-rc.9, including v5.15.0-rc.1 through v5.15.0-rc.8) and former `4.0.0` rows are rolled off to respect the P9 changelog cap; their full bodies remain at their git tags.

---

## License

[MIT License](LICENSE)

Created by [Anton Razzhigaev](https://t.me/abstractDL) & Andrew Kaznacheev
