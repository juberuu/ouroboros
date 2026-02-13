# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 0.1.1

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
  agent.py                 — Ядро: handle_task, LLM-цикл, контекст, Telegram
  tools.py                 — Реестр инструментов: схемы + реализации
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
- `memory/scratchpad_journal.jsonl` — журнал обновлений

## Инструменты агента

Базовый набор (Уроборос может добавлять новые):
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
- `promote_to_stable` — промоут в stable (без approval)
- `schedule_task`, `cancel_task` — управление задачами
- `telegram_send_voice` — отправка голосового сообщения через TTS

## Режим эволюции

`/evolve` включает непрерывные self-improvement циклы.
Уроборос свободен в выборе направления. Цель — ускорение эволюции (принцип 5).
Каждый цикл: обдумай → спланируй → реализуй → проверь → закоммить → рестарт.

## Deep review

`/review` или запрос Уробороса. Полный анализ кода, промптов, состояния, логов.
Scope — на усмотрение Уробороса. Результат влияет на следующие улучшения.

## Самоизменение

1. `claude_code_edit(prompt)` — основной путь для кода
2. `repo_commit_push(message)` — commit + push (с rebase)
3. `request_restart(reason)` — перезапуск для применения
4. `promote_to_stable(reason)` — обновить fallback

---

## Changelog

### 0.1.1 — Добавлен telegram_send_voice

Восстановлен инструмент отправки голосовых сообщений.

**Новое:**
- Инструмент `telegram_send_voice` — синтез речи через local ffmpeg/flite или OpenAI TTS
- Поддержка двух режимов: `tts="local"` (ffmpeg+flite) и `tts="openai"` (OpenAI TTS API)
- Опциональные параметры: caption, voice, openai_model, openai_voice, openai_format

### 0.1.0 — Рефакторинг по Библии

Первая версионированная версия. Радикальное упрощение архитектуры.

**Новое:**
- `BIBLE.md` — философия и 7 принципов
- `VERSION` — файл версии (semver)
- `prompts/SYSTEM.md` — единый системный промпт
- `ouroboros/llm.py` — LLM-клиент (контракт: chat, model_profile)
- `ouroboros/memory.py` — память (контракт: scratchpad, identity, chat_history)
- `ouroboros/tools.py` — реестр инструментов (контракт: schemas, execute)
- `ouroboros/review.py` — deep review (контракт: run_review)
- Инструмент `chat_history` — произвольный доступ к истории чата
- Инструмент `promote_to_stable` — промоут без approval
- `git pull --rebase` перед push (предотвращает конфликты)
- Soft LLM-check каждые 15 раундов вместо жёсткого лимита
- Контекст чата расширен до 100 сообщений

**Удалено:**
- Роутер (все сообщения через Уробороса, LLM-first)
- Idle-задачи (дублировали эволюцию)
- Система approval (`/approve`, `/deny`)
- Keyword-based routing (`_is_review_request_text`, `_is_code_intent_text`)
- Жёсткий лимит tool rounds (20)
- ~20 env-переменных конфигурации
- Инструменты: `telegram_send_voice`, `telegram_send_photo`,
  `telegram_generate_and_send_image`, `reindex_request`
- Файлы: `WORLD.md`, `prompts/BASE.md`, `prompts/evolution.md`,
  `prompts/SCRATCHPAD_SUMMARY.md`

**Упрощено:**
- Таймауты: один soft (10 мин) + один hard (30 мин)
- Эволюция: минимальная логика в launcher, детали в промпте
- Env-переменные: ~8 вместо ~30
