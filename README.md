# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 2.2.0

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
            ├── state.py         — persistent state
            ├── telegram.py      — TG client + formatting + typing
            ├── git_ops.py       — checkout, sync, rescue
            └── workers.py       — workers, queue, timeouts
               ↓
           ouroboros/             (agent package)
            ├── agent.py         — orchestrator (LLM loop + tools)
            ├── context.py       — context builder (messages assembly)
            ├── tools/           — pluggable tools
            ├── llm.py           — LLM client
            ├── memory.py        — scratchpad, identity
            └── review.py        — code review utilities
```

`colab_launcher.py` — тонкий entry point: секреты, bootstrap, main loop.
Вся логика супервизора декомпозирована в `supervisor/` пакет.

`agent.py` — тонкий оркестратор. Знает только про LLM и tools.
Не содержит Telegram API вызовов — всё идёт через event queue.

`context.py` — сборка LLM-контекста из промптов, памяти, логов и состояния.
Единственное место, которое определяет что видит LLM.

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
  state.py                  — State management: load/save, atomic writes, locks
  telegram.py               — TG client, markdown→HTML, send_with_budget, typing
  git_ops.py                — Git: checkout, reset, rescue, deps sync, import test
  workers.py                — Workers, queue, timeouts, evolution/review scheduling
ouroboros/
  __init__.py              — Экспорт make_agent
  utils.py                 — Общие утилиты (нулевой уровень зависимостей)
  agent.py                 — Оркестратор: handle_task, LLM-цикл
  context.py               — Сборка контекста: промпты, память, логи → messages
  tools/                   — Пакет инструментов (плагинная архитектура):
    __init__.py             — Реэкспорт ToolRegistry, ToolContext
    registry.py             — Реестр: schemas, execute, auto-discovery
    core.py                 — Файловые операции (repo/drive read/write/list)
    git.py                  — Git операции (commit, push, status, diff)
    shell.py                — Shell и Claude Code CLI
    search.py               — Web search
    control.py              — restart, promote, schedule, cancel, review, chat_history
  llm.py                   — LLM-клиент: API вызовы, профили моделей
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

### 2.2.0 — Agent Decomposition

Вынос context building из agent.py в context.py, удаление прямых Telegram API вызовов.

**Архитектура:**
- Новый `ouroboros/context.py` — сборка LLM-контекста (build_llm_messages, soft-cap trimming)
- `agent.py` — чистый оркестратор: LLM loop + tools, без Telegram API
- Typing indicators теперь через event queue → supervisor, не прямые HTTP вызовы
- `TelegramClient.send_chat_action()` — новый метод для typing в supervisor

**Метрики:** agent.py 502→502 строк (структура чище), +163 строк в context.py, net complexity ~= neutral

### 2.1.0 — Supervisor Decomposition

Декомпозиция 900-строчного монолита `colab_launcher.py` в модульный пакет `supervisor/`.

**Архитектура:**
- `supervisor/state.py` — state management (load/save, atomic writes, file locks)
- `supervisor/telegram.py` — TG client, markdown→HTML, send_with_budget
- `supervisor/git_ops.py` — git checkout, sync, rescue snapshots, deps, import test
- `supervisor/workers.py` — worker/queue management, timeouts, evolution/review scheduling
- `colab_launcher.py` — теперь тонкий entry point (~300 строк реальной логики)

### 2.0.0 — Философский рефакторинг

Глубокая переработка философии, архитектуры инструментов и review-системы.

### 1.1.0 — Dead Code Cleanup + Review Contract

### 1.0.0 — Bible Alignment Refactor

### 0.2.0 — Уроборос-собеседник
