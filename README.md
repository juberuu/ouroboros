# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 2.5.0

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
            ├── agent.py         — orchestrator (LLM loop + tools)
            ├── context.py       — context builder + tool history compaction
            ├── apply_patch.py   — Claude Code CLI apply_patch shim
            ├── tools/           — pluggable tools
            ├── llm.py           — LLM client + cached token tracking
            ├── memory.py        — scratchpad, identity
            └── review.py        — code review utilities
```

`colab_launcher.py` — тонкий entry point: секреты, bootstrap, main loop.
Вся логика супервизора декомпозирована в `supervisor/` пакет.

`agent.py` — тонкий оркестратор. Знает только про LLM и tools.
Не содержит Telegram API вызовов — всё идёт через event queue.

`context.py` — сборка LLM-контекста из промптов, памяти, логов и состояния.
Включает `compact_tool_history()` для сжатия старых tool results в длинных
диалогах — сокращает prompt tokens на 30-50% в evolution/review циклах.

`tools/` — плагинная архитектура инструментов. Каждый модуль экспортирует
`get_tools()`, новые инструменты добавляются как отдельные файлы.

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
  agent.py                 — Оркестратор: handle_task, LLM-цикл
  context.py               — Сборка контекста + compact_tool_history
  tools/                   — Пакет инструментов (плагинная архитектура):
    __init__.py             — Реэкспорт ToolRegistry, ToolContext
    registry.py             — Реестр: schemas, execute, auto-discovery
    core.py                 — Файловые операции (repo/drive read/write/list)
    git.py                  — Git операции (commit, push, status, diff) + untracked warning
    shell.py                — Shell и Claude Code CLI
    search.py               — Web search
    control.py              — restart, promote, schedule, cancel, review, chat_history
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

### 2.5.0 — Cost Tracking + Restart DRY

Два направления: observability для оптимизации стоимости + устранение дублирования restart-логики.

**Observability:**
- Per-round `llm_round` events в events.jsonl: model, effort, prompt/completion/cached tokens
- `cached_tokens` tracking в LLM client (из `prompt_tokens_details`)
- `spent_tokens_cached` в state.json и `/status`

**DRY:**
- `safe_restart()` в git_ops.py — единая функция для checkout→deps→import→fallback
- 3 копии restart-логики в launcher → 3 вызова safe_restart()
- colab_launcher.py: 537→472 строк (-65)

**Метрики:** все модули ≤515 строк, total 4647 строк

### 2.4.0 — Context Window Optimization

- `compact_tool_history()` — сжатие старых tool results (30-50% экономия tokens)
- apply_patch шим вынесен в `ouroboros/apply_patch.py`

### 2.3.0 — Queue Decomposition + Git Safety

Декомпозиция `supervisor/workers.py` (687→282 строк) и критический fix для git reliability.

### 2.2.0 — Agent Decomposition

Вынос context building из agent.py в context.py, удаление прямых Telegram API вызовов.

### 2.1.0 — Supervisor Decomposition

Декомпозиция 900-строчного монолита `colab_launcher.py` в модульный пакет `supervisor/`.
