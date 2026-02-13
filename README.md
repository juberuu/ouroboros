# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 1.1.0

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
Telegram → colab_launcher.py (supervisor)
               ↓
           agent.py (orchestrator)
            ↓      ↓      ↓      ↓
        tools.py  llm.py  memory.py  review.py
            ↓      ↓      ↓      ↓
              utils.py (shared utilities)
```

`agent.py` — тонкий оркестратор. Вся логика инструментов, LLM-вызовов,
памяти и review вынесена в соответствующие модули (SSOT-принцип).

## Структура проекта

```
BIBLE.md                   — Философия и принципы (корень всего)
VERSION                    — Текущая версия (semver)
README.md                  — Это описание
requirements.txt           — Python-зависимости
prompts/
  SYSTEM.md                — Единый системный промпт Уробороса
ouroboros/
  __init__.py              — Экспорт make_agent
  utils.py                 — Общие утилиты (нулевой уровень зависимостей)
  agent.py                 — Оркестратор: handle_task, LLM-цикл, контекст, Telegram
  tools.py                 — SSOT: реестр инструментов (схемы + реализации)
  llm.py                   — LLM-клиент: API вызовы, профили моделей
  memory.py                — Память: scratchpad, identity, chat_history
  review.py                — Deep review: сбор данных, анализ, синтез
colab_launcher.py          — Супервизор: Telegram polling, очередь, воркеры, git
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

## Google Drive (`MyDrive/Ouroboros/`)

- `state/state.json` — состояние (owner_id, бюджет, версия)
- `logs/` — JSONL логи (chat, events, tools, supervisor)
- `memory/scratchpad.md` — рабочая память
- `memory/identity.md` — self-model

## Инструменты агента

Единый реестр в `ouroboros/tools.py`:
- `repo_read`, `repo_list` — чтение репозитория
- `drive_read`, `drive_list`, `drive_write` — Google Drive
- `repo_write_commit` — запись файла + commit + push
- `repo_commit_push` — commit + push (с pull --rebase)
- `claude_code_edit` — делегирование правок Claude Code CLI
- `git_status`, `git_diff` — состояние repo
- `run_shell` — shell-команда
- `web_search` — поиск в интернете
- `chat_history` — произвольный доступ к истории чата
- `request_restart` — перезапуск после push
- `promote_to_stable` — промоут в stable
- `schedule_task`, `cancel_task` — управление задачами
- `request_review` — запросить deep review (агент сам решает когда)

## Режим эволюции

`/evolve` включает непрерывные self-improvement циклы.
Уроборос свободен в выборе направления. Цель — ускорение эволюции (принцип 5).
Каждый цикл: обдумай → спланируй → реализуй → проверь → закоммить → рестарт.

## Deep review

`/review` (владелец) или `request_review(reason)` (агент).
Полный анализ кода, промптов, состояния, логов.
Scope — на усмотрение Уробороса. Результат влияет на следующие улучшения.

## Самоизменение

1. `claude_code_edit(prompt)` — основной путь для кода
2. `repo_commit_push(message)` — commit + push (с rebase)
3. `request_restart(reason)` — перезапуск для применения
4. `promote_to_stable(reason)` — обновить fallback

---

## Changelog

### 1.1.0 — Dead Code Cleanup + Review Contract

Удаление мёртвого кода и восстановление разорванного контракта review.

**Новое:**
- `request_review(reason)` — LLM-first инструмент: агент сам решает когда запрашивать review (Принцип 7).
- `task_metrics` — агент эмитит метрики задач для supervisor (восстановлен разорванный контракт).
- Конкретные триггеры обновления `identity.md` в SYSTEM.md.

**Удалено (мёртвый код):**
- `Memory.save_identity()`, `Memory.summarize_narration()`, `Memory.repo_dir` — неиспользуемые.
- `LLMClient.chat_raw()`, профиль `memory_summary` — неиспользуемые.
- `ToolRegistry.branch_stable`, `Env.branch_stable` — неиспользуемые.
- `_slice_by_utf16_units()` в agent.py — неиспользуемый.
- `as_bool()`, `_running_task_type_counts()` в launcher — неиспользуемые.
- Мёртвые env: `OUROBOROS_MODEL_REVIEW`, `OUROBOROS_TASK_HEARTBEAT_SEC`.
- Мёртвые state defaults: `queue_seq`.
- Мёртвая ветка приоритета `idle`.
- Неиспользуемые импорты в agent.py: `subprocess`, `sha256_text`, `write_text`, `SCRATCHPAD_SECTIONS`.

### 1.0.0 — Bible Alignment Refactor

Полный архитектурный рефактор на соответствие BIBLE.md. Breaking changes.

**Новая архитектура:**
- `agent.py` — тонкий оркестратор (~550 строк вместо ~4100)
- `tools.py` — SSOT для всех tool schemas и реализаций
- `llm.py` — единственный LLM-клиент с профилями моделей
- `memory.py` — единственный источник scratchpad/identity/chat_history
- `review.py` — deep review через LLM-клиент
- `utils.py` — общие утилиты без внутренних зависимостей

**Удалено:**
- Дублирование tool schemas/implementations между agent.py и tools.py
- Дублирование LLM-клиента (agent.py vs llm.py)
- Дублирование memory-логики (agent.py vs memory.py)
- Дублирование утилит (4 копии в разных модулях)
- `_is_code_intent_text` (keyword routing — нарушение LLM-first)
- `telegram_send_voice`, `telegram_send_photo`, `telegram_generate_and_send_image` (tools)
- Механические Telegram-сообщения «Стартую задачу...» для обычного чата
- Legacy state поля: `approvals`, `idle_cursor`, `idle_stats`, `last_idle_task_at`
- ~50 env-переменных конфигурации (OUROBOROS_CONTEXT_*, OUROBOROS_REASONING_*, и т.д.)
- `prompts/SCRATCHPAD_SUMMARY.md` (ссылка на несуществующий файл)
- `smoke_test()` (ссылки на несуществующие файлы)

**Упрощено:**
- Env-переменные: ~6 вместо ~50+ (OUROBOROS_MODEL, OUROBOROS_MODEL_CODE, и дефолты в коде)
- Профили моделей: захардкожены разумные дефолты, не требуют env
- Memory update: детерминистический (без дополнительного LLM-вызова)
- Tool contract: единый параметр `prompt` для claude_code_edit

### 0.2.0 — Уроборос-собеседник

Архитектурное изменение: Уроборос — собеседник, а не система обработки заявок.

- Прямой диалог: сообщения владельца обрабатываются LLM напрямую (в потоке),
  без очереди задач и без механических сообщений «Стартую задачу...»
- Воркеры только для фоновых задач (эволюция, review)
- Обновлён Принцип 1 в BIBLE.md: chat-first интерфейс
- SYSTEM.md: агент знает что он собеседник, не обработчик заявок

### 0.1.0 — Рефакторинг по Библии

Первая версионированная версия. Радикальное упрощение архитектуры.
