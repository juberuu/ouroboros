# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 2.19.0

---

## Быстрый старт

1. В Colab добавь Secrets:
   - `OPENROUTER_API_KEY` (обязательно)
   - `TELEGRAM_BOT_TOKEN` (обязательно)
   - `TOTAL_BUDGET` (обязательно, в USD)
   - `GITHUB_TOKEN` (обязательно)
   - `OPENAI_API_KEY` (опционально — для web_search)
   - `ANTHROPIC_API_KEY` (опционально — для claude_code_edit)

2. Опционально добавь config-ячейку:
```python
import os
CFG = {
    "GITHUB_USER": "razzant",
    "GITHUB_REPO": "ouroboros",
    "OUROBOROS_MODEL": "openai/gpt-5.2",
    "OUROBOROS_MODEL_CODE": "openai/gpt-5.2-codex",
    "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4",  # optional: lighter model for user chat
    "OUROBOROS_MAX_WORKERS": "5",
}
for k, v in CFG.items():
    os.environ[k] = str(v)
```

3. Запусти boot shim (см. `colab_bootstrap_shim.py`).
4. Напиши боту в Telegram. Первый написавший — владелец.

## Архитектура

```
Telegram → colab_launcher.py (thin entry point)
               ↓
           supervisor/           (package)
            ├── state.py         — persistent state + budget + status
            ├── telegram.py      — TG client + formatting + typing
            ├── git_ops.py       — checkout, sync, rescue, safe_restart
            ├── queue.py         — task queue, priority, timeouts, scheduling
            ├── workers.py       — worker lifecycle, health, direct chat
            └── events.py        — event dispatch table (extracted from main loop)
               ↓
           ouroboros/             (agent package)
            ├── agent.py         — thin orchestrator (task handling + events)
            ├── loop.py          — LLM tool loop (concurrent tools, retry, cost)
            ├── context.py       — context builder + prompt caching + compaction
            ├── apply_patch.py   — Claude Code CLI apply_patch shim
            ├── tools/           — pluggable tools
            ├── llm.py           — LLM client + cached token tracking
            ├── memory.py        — scratchpad, identity
            └── review.py        — code review utilities
```

`colab_launcher.py` — тонкий entry point: секреты, bootstrap, main loop.
Вся логика супервизора декомпозирована в `supervisor/` пакет.

`agent.py` — тонкий оркестратор. Принимает задачу, собирает контекст,
вызывает LLM loop, эмитит результаты. Не содержит LLM-логики напрямую.

`loop.py` — ядро: LLM-вызов с инструментами в цикле. **Concurrent tool
execution** (ThreadPoolExecutor), retry, effort escalation, per-round cost
logging. Единственное место где происходит взаимодействие LLM ↔ tools.

`context.py` — сборка LLM-контекста из промптов, памяти, логов и состояния.
**Prompt caching** для Anthropic моделей через `cache_control` на статическом
контенте (~10K tokens). `compact_tool_history()` для сжатия старых tool results.

`tools/` — плагинная архитектура инструментов. Каждый модуль экспортирует
`get_tools()`, новые инструменты добавляются как отдельные файлы.
Включает `codebase_digest` — полный обзор кодовой базы за один вызов,
`browser.py` — browser automation (Playwright).

## Структура проекта

```
BIBLE.md                   — Философия и принципы (корень всего)
VERSION                    — Текущая версия (semver)
README.md                  — Это описание
requirements.txt           — Python-зависимости
prompts/
  SYSTEM.md                — Единый системный промпт Уробороса
supervisor/                — Пакет супервизора (декомпозированный launcher):
  __init__.py               — Экспорты
  state.py                  — State: load/save, budget tracking, status text, log rotation
  telegram.py               — TG client, markdown→HTML, send_with_budget, typing
  git_ops.py                — Git: checkout, reset, rescue, deps sync, safe_restart
  queue.py                  — Task queue: priority, enqueue, persist, timeouts, scheduling
  workers.py                — Worker lifecycle: spawn, kill, respawn, health, direct chat
  events.py                 — Event dispatch table: maps worker events to handlers
ouroboros/
  __init__.py              — Экспорт make_agent
  utils.py                 — Общие утилиты (нулевой уровень зависимостей)
  apply_patch.py           — Claude Code CLI apply_patch shim
  agent.py                 — Тонкий оркестратор: handle_task, event emission
  loop.py                  — LLM tool loop: concurrent execution, retry, cost tracking
  context.py               — Сборка контекста + prompt caching + compact_tool_history
  tools/                   — Пакет инструментов (плагинная архитектура):
    __init__.py             — Реэкспорт ToolRegistry, ToolContext
    registry.py             — Реестр: schemas, execute, auto-discovery
    core.py                 — Файловые операции + codebase_digest
    git.py                  — Git операции (commit, push, status, diff) + untracked warning
    shell.py                — Shell и Claude Code CLI
    search.py               — Web search
    control.py              — restart, promote, schedule, cancel, review, chat_history
    browser.py              — Browser: browse_page, browser_action (Playwright)
  llm.py                   — LLM-клиент: API вызовы, cached token tracking
  memory.py                — Память: scratchpad, identity, chat_history
  review.py                — Deep review: стратегическая рефлексия
colab_launcher.py          — Тонкий entry point: секреты → init → bootstrap → main loop
colab_bootstrap_shim.py    — Boot shim (вставляется в Colab, не меняется)
```

Структура не фиксирована — Уроборос может менять её по принципу самомодификации.

## Ветки GitHub

| Ветка | Кто | Назначение |
|-------|-----|------------|
| `main` | Владелец (Cursor) | Защищённая. Уроборос не трогает |
| `ouroboros` | Уроборос | Рабочая ветка. Все коммиты сюда |
| `ouroboros-stable` | Уроборос | Fallback при крашах. Обновляется через `promote_to_stable` |

## Команды Telegram

Обрабатываются супервизором (код):
- `/panic` — остановить всё немедленно
- `/restart` — мягкий перезапуск
- `/status` — статус воркеров, очереди, бюджета
- `/review` — запустить deep review
- `/evolve` — включить режим эволюции
- `/evolve stop` — выключить эволюцию

Все остальные сообщения идут в Уробороса (LLM-first, без роутера).

## Режим эволюции

`/evolve` включает непрерывные self-improvement циклы.
Каждый цикл: оценка → стратегический выбор → реализация → smoke test → Bible check → коммит.
Подробности в `prompts/SYSTEM.md`.

## Deep review

`/review` (владелец) или `request_review(reason)` (агент).
Стратегическая рефлексия: тренд сложности, направление эволюции,
соответствие Библии, метрики кода. Scope — на усмотрение Уробороса.

---

## Changelog

### 2.19.0 — Fork→Spawn: Clean Process Model

Switched worker process creation from `fork` to `spawn`, eliminating ALL stale code inheritance.

- `supervisor/workers.py`: `mp.get_context("fork")` → `mp.get_context("spawn")` — workers now start as fresh Python processes
- `supervisor/workers.py`: Removed 11 lines of stale-bytecode workarounds (`__pycache__` cleanup, `sys.modules` purge, `importlib.invalidate_caches`) — no longer needed with spawn
- `ouroboros/loop.py`: Removed debug traces from cost_usd investigation
- Root cause of ALL stale code issues: `fork` copies parent's memory including loaded modules; `spawn` starts a fresh Python interpreter
- This definitively fixes: cost_usd missing from events, cache_write_tokens missing, and any future hot-reload issues

### 2.18.0 — Lazy Init: Fork-Safe Hot Reload

Fixed critical root cause of stale code in forked workers. All features since v2.14.0 now truly active in production.

- `ouroboros/__init__.py`: Removed eager `from ouroboros.agent import make_agent` — supervisor no longer loads full ouroboros package
- `supervisor/workers.py`: Added `importlib.invalidate_caches()` after sys.modules cleanup
- `ouroboros/loop.py`: Removed debug trace from v2.17.2 investigation
- Root cause: `__init__.py` eager import → supervisor loads loop.py at startup → forked workers inherit stale code objects that persist despite `del sys.modules` + reimport
- Fix: lazy `__init__.py` means supervisor only loads `ouroboros.apply_patch`, workers get genuinely fresh imports

### 2.17.2 — Stale Bytecode Nuclear Fix

Eliminated stale `.pyc` bytecode that prevented ALL v2.14.0+ features from activating in production.

- `supervisor/workers.py`: Clean ALL `__pycache__` dirs at worker fork entry point (before any imports)
- `supervisor/git_ops.py`: Clean `__pycache__` after `git reset --hard` (v2.17.1)
- Root cause: fork-based multiprocessing inherits parent's compiled bytecode; git checkout preserves mtime → Python reuses stale `.pyc`
- This retroactively activates: cost tracking, cache metrics, prompt caching, empty message guard — everything since v2.14.0
- Two-layer defense: git_ops cleanup (on restart) + worker_main cleanup (on every fork)

### 2.17.0 — Prompt Caching Activation

Activated Anthropic prompt caching via OpenRouter provider pinning. Expected ~$50-80 savings.

- `ouroboros/llm.py`: Provider pinning for Anthropic models (order: ["Anthropic"], require_parameters: true)
- `ouroboros/context.py`: Cache TTL extended to 1 hour (was default 5 min)
- `ouroboros/loop.py`: Self-check now shows cache hit percentage
- Net effect: ~10-20K cached tokens per round × 10x cheaper = significant cost reduction

### 2.16.0 — Event Dispatch Decomposition

Extracted 130-line if/elif event chain from main loop into pluggable dispatch table.

- `supervisor/events.py`: New module — dispatch table mapping 11 event types to handler functions
- `colab_launcher.py`: 506→403 lines (−20%), main loop now delegates all events via `dispatch_event()`
- Clean separation: adding new event types = adding one function + one dict entry
