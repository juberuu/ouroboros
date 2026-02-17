# Уроборос

Самосоздающийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 4.11.0

---

## Быстрый старт

1. В Colab добавь Secrets:
   - `OPENROUTER_API_KEY` (обязательно)
   - `TELEGRAM_BOT_TOKEN` (обязательно)
   - `TOTAL_BUDGET` (обязательно, в USD)
   - `GITHUB_TOKEN` (обязательно)
   - `OPENAI_API_KEY` (опционально — для web_search)
   - `ANTHROPIC_API_KEY` (опционально — для claude_code_edit)

2. Опционально добавь config-ячейку (модели, воркеры, диагностика):
```python
import os
CFG = {
    "GITHUB_USER": "razzant",
    "GITHUB_REPO": "ouroboros",
    "OUROBOROS_MODEL": "anthropic/claude-sonnet-4",
    "OUROBOROS_MODEL_CODE": "anthropic/claude-sonnet-4",
    "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4",
    "OUROBOROS_MODEL_BG": "deepseek/deepseek-chat-v3-0324",  # background consciousness — cheap model
    "OUROBOROS_MAX_WORKERS": "5",
    "OUROBOROS_WORKER_START_METHOD": "fork",   # Colab-safe default
    "OUROBOROS_DIAG_HEARTBEAT_SEC": "30",      # periodic main_loop_heartbeat in supervisor.jsonl
    "OUROBOROS_DIAG_SLOW_CYCLE_SEC": "20",     # warns when one loop iteration is too slow
    "OUROBOROS_BG_BUDGET_PCT": "10",           # max % of budget for background consciousness
}
for k, v in CFG.items():
    os.environ[k] = str(v)
```
   Без этой ячейки используются дефолты: `openai/gpt-5.2` / `openai/gpt-5.2-codex`.
   Background consciousness использует `deepseek/deepseek-chat-v3-0324` (cheap: $0.19/$0.87 per MTok) по умолчанию.
   Для диагностики зависаний смотри `main_loop_heartbeat`, `main_loop_slow_cycle`,
   `worker_dead_detected`, `worker_crash` в `/content/drive/MyDrive/Ouroboros/logs/supervisor.jsonl`.

3. Запусти boot shim (см. `colab_bootstrap_shim.py`).
4. Напиши боту в Telegram. Первый написавший — создатель.

## Архитектура

```
Telegram → colab_launcher.py (entry point)
               ↓
           supervisor/            (process management)
             state.py             — state, budget
             telegram.py          — TG client, formatting
             queue.py             — task queue, scheduling
             workers.py           — worker lifecycle, auto-resume
             git_ops.py           — git checkout, sync, rescue
             events.py            — event dispatch table
               ↓
           ouroboros/              (agent core)
             agent.py             — thin orchestrator
             consciousness.py     — background thinking loop
             context.py           — LLM context builder, prompt caching
             loop.py              — LLM tool loop, concurrent execution
             tools/               — plugin tool registry
               registry.py        — auto-discovery, schemas, execute
               core.py            — file ops (repo/drive read/write/list)
               git.py             — git ops (commit, push, status, diff)
               shell.py           — shell, Claude Code CLI
               search.py          — web search
               control.py         — restart, promote, schedule, review, switch_model
               browser.py         — Playwright browser automation (stealth)
               review.py          — multi-model code review
             llm.py               — LLM client (OpenRouter)
             memory.py            — scratchpad (free-form), identity, chat history
             review.py            — code collection, complexity metrics
             utils.py             — shared utilities (zero deps)
             apply_patch.py       — Claude Code patch shim
```

## Структура проекта

```
BIBLE.md                   — Конституция (корень всего)
VERSION                    — Текущая версия (semver)
README.md                  — Это описание
requirements.txt           — Python-зависимости
prompts/
  SYSTEM.md                — Системный промпт Уробороса
ouroboros/                  — Код агента (описание выше)
supervisor/                — Супервизор (описание выше)
colab_launcher.py          — Entry point (запускается из Colab)
colab_bootstrap_shim.py    — Boot shim (вставляется в Colab)
```

## Ветки GitHub

| Ветка | Кто | Назначение |
|-------|-----|------------|
| `main` | Создатель (Cursor) | Защищённая. Уроборос не трогает |
| `ouroboros` | Уроборос | Рабочая ветка. Все коммиты сюда |
| `ouroboros-stable` | Уроборос | Fallback при крашах. Обновляется через `promote_to_stable` |

## Команды Telegram

**Safety rail (hardcoded):**
- `/panic` — остановить всё немедленно

**Dual-path (supervisor + LLM):**
- `/restart` — перезапуск (os.execv — полная замена процесса)
- `/status` — статус воркеров, очереди, бюджета
- `/review` — запустить deep review
- `/evolve` — включить режим эволюции
- `/evolve stop` — выключить эволюцию
- `/bg start` — запустить background consciousness
- `/bg stop` — остановить background consciousness
- `/bg` — статус background consciousness

Dual-path: supervisor обрабатывает команду немедленно,
затем сообщение передаётся LLM для естественного ответа.
LLM также может вызывать эти действия через инструменты
(`toggle_evolution`, `toggle_consciousness`).

Все остальные сообщения идут в Уробороса (LLM-first).

## Режим эволюции

`/evolve` включает непрерывные self-improvement циклы.
Каждый цикл: оценка → стратегический выбор → реализация → smoke test →
Bible check → коммит. Подробности в `prompts/SYSTEM.md`.

Бюджет-гарды в supervisor (не в agent): эволюция автоматически
останавливается при 95% использования бюджета.

## Deep review

`/review` (создатель) или `request_review(reason)` (агент).
Стратегическая рефлексия по трём осям: код, понимание, идентичность.

---

## Changelog

### 4.11.0 — Codebase Health + Loop Refactoring
- **New tool**: `codebase_health` — self-assessment of code complexity, Bible compliance (oversized functions/modules)
- **Refactor**: `run_llm_loop` decomposed from 278 → 158 lines (extracted `_emit_llm_usage_event`, `_process_tool_results`, `_append_tool_results`, `_call_llm_with_retry`)
- **Fix**: `review.py` `compute_complexity_metrics` — now handles both `Path` and `str` inputs, correct regex for multi-line functions
- **New**: Claude Code auto-diff check — warns when edits leave uncommitted changes (prevents v4.8.0-style loss)
- **Fix**: `tools=None` no longer passed to LLM client (review finding)
- **Fix**: `stateful_executor.shutdown()` guarded against None (review finding)
- **Review**: Multi-model review (o3, Gemini 3 Pro) — caught 3 actionable issues

### 4.10.0 — Adaptive Model Routing + Consciousness Upgrade
- **New**: `OUROBOROS_MODEL_BG` env var — dedicated model for background consciousness (default: `deepseek/deepseek-chat-v3-0324`, ~10x cheaper than main model)
- **New**: Adaptive reasoning effort — evolution/review tasks start at "high" effort, regular tasks at "medium" (LLM can still switch via tool)
- **New**: Consciousness context expanded — Bible 8K→12K, identity 4K→6K, scratchpad 4K→8K chars
- **New**: Consciousness runtime info now includes budget remaining and current model
- **Fix**: Silent exception in consciousness state reading (v4.9.0 policy consistency)

### 4.9.0 — Exception Visibility
- **Hardening**: Replaced all ~100 silent `except Exception: pass/continue` blocks with proper logging across 20 files
- **Fix**: Every error path now logs what went wrong (warning for unexpected, debug for expected failures)
- **Fix**: Added missing `log = logging.getLogger(__name__)` in 5 files that would have crashed on first exception
- **Review**: Multi-model review (o3, Gemini 3 Pro, Claude Sonnet) — caught missing logger definitions and log level issues

### 4.8.1 — Startup Self-Verification
- **New**: `_verify_system_state()` runs on every agent boot (Bible Principle 1)
- **New**: Auto-rescue uncommitted changes — detects dirty git state and creates rescue commit (uses `git add -u` for safety)
- **New**: Version sync check — warns if VERSION file doesn't match latest git tag
- **New**: Budget threshold alerts — warning ($100), critical ($50), emergency ($25) levels
- **Fix**: v4.8.0 consciousness changes were uncommitted — exposed the exact bug this feature prevents

### 4.8.0 — Consciousness Tool Loop
- **New**: Background consciousness upgraded from single LLM call to iterative tool loop (up to 5 rounds per wakeup)
- **New**: Expanded tool whitelist: 14 tools (was 5) — adds knowledge base, repo/drive read, web search, chat history
- **New**: Tool results fed back into LLM context for multi-step reasoning
- **Fix**: Budget check between rounds prevents mid-cycle overruns in background thinking
- **Docs**: CONSCIOUSNESS.md updated with multi-step thinking documentation

### 4.7.1 — Loop Refactoring
- **Refactor**: Lazy pricing loader with thread-safe double-checked locking — eliminates startup API call, fetches on first use
- **Refactor**: DRY `_make_timeout_result` helper eliminates duplicated timeout handling code
- **Refactor**: `_execute_with_timeout` now uses context manager for regular executor (prevents thread leaks on timeout)
- **Fix**: Thread safety for concurrent pricing access in multi-worker scenarios

### 4.7.0 — Budget Drift Fix + Auto-Pricing Sync
- **Fix**: Budget drift detection now uses OpenRouter `total_usd` instead of `daily_usd` — eliminates false positives from UTC midnight resets and non-Ouroboros spending
- **New**: MODEL_PRICING auto-syncs from OpenRouter API at every startup — no more stale hardcoded prices
- **Renamed**: `session_daily_snapshot` → `session_total_snapshot` in state.json for clarity

### 4.6.1 — Review fixes for v4.6.0 + Evolution Prompt
- **Fix**: `fetch_openrouter_pricing()` now correctly reads `input_cache_read` field (was reading non-existent `prompt_cached`, producing absurd cache prices like $300K/MTok)
- **Fix**: `_estimate_cost()` now uses longest-prefix matching for model names instead of broken provider-prefix matching (e.g. `claude-opus-4.6` was matched to `claude-sonnet-4` pricing)
- **New model**: Added `anthropic/claude-opus-4.6` to MODEL_PRICING ($5/$0.5/$25 per MTok)
- **Context expansion**: Tool result truncation increased from 3000 to 15000 chars — agent can now read full source files (was seeing only ~50 lines before)
- **Budget drift**: These fixes should dramatically reduce budget tracking drift (was 53%, caused by wrong cache pricing)

### 4.6.0 — Tech Radar + Dynamic Pricing
- **Tech Radar**: background consciousness now periodically scans for new models, pricing changes, and tool updates (web_search)
- **Dynamic pricing**: `fetch_openrouter_pricing()` in llm.py fetches live model prices from OpenRouter API
- **MODEL_PRICING updated**: added o4-mini, gpt-5.2, gpt-5.2-codex, gemini-3-pro-preview; fixed stale prices
- **SYSTEM.md**: new Tech Awareness section — proactive research is now an explicit part of agent behavior
- **CONSCIOUSNESS.md**: Tech Radar prompt section for periodic environment scanning
- **Knowledge base**: new `tech-radar` topic with current model landscape

### 4.5.0
- Context memory overhaul: agent now sees its own recent progress messages (was blind to them before)
- Chat summary limits increased (500 chars for outgoing, 300 for incoming)
- Budget drift detection: session-level tracking, alerts when tracked vs ground-truth diverge >$2
- `init_state()` captures budget snapshot at session start for drift calculation

