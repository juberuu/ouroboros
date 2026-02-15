# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 2.13.0

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
            └── workers.py       — worker lifecycle, health, direct chat
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

### 2.13.0 — Browser Automation (Playwright)

Agent can now browse the web, interact with pages, and take screenshots.

- `ouroboros/tools/browser.py`: New tool module with `browse_page` and `browser_action`
- `browse_page`: Open URL, get page content as text/html/markdown/screenshot (base64 PNG)
- `browser_action`: Click, fill forms, select options, evaluate JS, scroll, take screenshots
- Headless Chromium with persistent browser session across tool calls within a task
- Auto-discovered via ToolEntry pattern — 22 tools total
- Playwright + deps auto-installed in Colab environment

### 2.12.0 — Vision: Image Support

Agent can now see images sent in Telegram (photos and image documents).

- `supervisor/telegram.py`: `download_file_base64()` downloads Telegram photos as base64
- `colab_launcher.py`: Extracts photos/image-documents from updates, passes to agent
- `supervisor/workers.py`: `handle_chat_direct` accepts `image_data` parameter
- `ouroboros/context.py`: `_build_user_content()` builds multipart user messages (text + image_url)
- Supports OpenAI Vision API format (`image_url` with `data:` URI) — works with all vision-capable models via OpenRouter
- Max 10MB per image, auto-detects MIME type from file extension

### 2.11.0 — Module Cache Purge on Restart (Critical)

Fixed: restart never activated new code because fork'd workers inherited stale sys.modules.

- `worker_main()`: purges all `ouroboros.*` from sys.modules before import (workers get fresh code)
- `reset_chat_agent()`: purges `ouroboros.*` so chat agent reimports after restart
- Root cause: `mp.get_context("fork")` copies parent's sys.modules → `import ouroboros` was no-op
- This means ALL improvements since v2.8.0 were never active in runtime until manual /restart from Colab
