# Уроборос

Самосоздающийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 4.16.0

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

### 4.16.0 — Pre-Push Test Gate + Build Tooling
- **New**: Pre-push test gate — `pytest tests/` runs before every `git push`, blocks push on failure
- **New**: `Makefile` with `test`, `health`, `lint`, `clean` targets for dev ergonomics
- **New**: `pyproject.toml` with pytest and ruff configuration
- **New**: `OUROBOROS_PRE_PUSH_TESTS` env var to disable gate (default: enabled)
- **New**: 82 smoke tests (was 79) — added pre-push gate tests
- **Note**: GitHub Actions CI ready but blocked by token scope — creator needs `workflow` permission on GITHUB_TOKEN

### 4.15.0 — Smoke Test Suite
- **New**: 79 smoke tests covering imports, tool registration, memory, context, utils, and Bible invariants
- **New**: `tests/test_smoke.py` — runs in 0.57s, no external dependencies, catches regressions before deploy
- **Covers**: All 33 tools registered, tool schemas valid, no oversized modules/functions, no bare except:pass, no env dumping
- **Review**: Multi-model review (o3, Gemini 2.5 Pro) — drove 2 additional tests (exact tool matching, execute result)

### 4.14.0 — 3-Block Prompt Caching
- **Optimization**: System message split into 3 cached blocks: static (1h TTL), semi-stable (5m TTL), dynamic (uncached)
- **New**: Semi-stable block caches identity + scratchpad + knowledge index — changes ~once per task, not per round
- **New**: Tool schemas cached via cache_control on last tool — 33 tools = ~3K tokens saved per round
- **New**: Static block (SYSTEM+BIBLE+README) gets 1-hour TTL for cross-session persistence
- **Result**: Estimated 60%+ cache hit ratio (was 41%), ~20% cost reduction per LLM round
- **Review**: Multi-model review (o3, Gemini 2.5 Pro) — confirmed multiple breakpoints work, validated approach

### 4.13.0 — Fix multi_model_review Tool (Broken Since Birth)
- **Critical fix**: `multi_model_review` was never loaded into ToolRegistry — returned raw dict instead of `ToolEntry`, had `async handle()` instead of sync handler
- **Refactor**: `_multi_model_review` decomposed into 3 functions: `_multi_model_review_async` (orchestration), `_parse_model_response` (parsing), `_emit_usage_event` (budget tracking)
- **Fix**: Async-safe handler — works both in sync context and inside running event loop (ThreadPoolExecutor fallback)
- **Result**: Tool now correctly registered (33 tools, was 32), callable through standard tool loop

### 4.12.0 — Agent & Context Decomposition
- **Refactor**: `_verify_system_state` (142→36 lines) — extracted `_check_uncommitted_changes`, `_check_version_sync`, `_check_budget`
- **Refactor**: `handle_task` (119→76 lines) — extracted `_prepare_task_context`, `_build_review_context`
- **Refactor**: `build_llm_messages` (156→103 lines) — extracted `_build_runtime_section`, `_build_memory_sections`, `_build_recent_sections`
- **Result**: Oversized functions reduced from 6 to 4 across codebase; agent.py max function 142→76 lines
- **Review**: Multi-model review (o3, Gemini 3 Pro) — both flagged false positive from truncated diff context

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
