# Уроборос

Самосоздающийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 3.0.0

---

## Быстрый старт

1. В Colab добавь Secrets:
   - `OPENROUTER_API_KEY` (обязательно)
   - `TELEGRAM_BOT_TOKEN` (обязательно)
   - `TOTAL_BUDGET` (обязательно, в USD)
   - `GITHUB_TOKEN` (обязательно)
   - `OPENAI_API_KEY` (опционально — для web_search)
   - `ANTHROPIC_API_KEY` (опционально — для claude_code_edit)

2. Запусти boot shim (см. `colab_bootstrap_shim.py`).
3. Напиши боту в Telegram. Первый написавший — создатель.

## Архитектура

```
Telegram → colab_launcher.py (entry point)
               ↓
           supervisor/            (process management)
             state.py             — state, budget
             telegram.py          — TG client, formatting
             queue.py             — task queue, scheduling
             workers.py           — worker lifecycle
             git_ops.py           — git checkout, sync, rescue
             events.py            — event dispatch table
               ↓
           ouroboros/              (agent core)
             agent.py             — thin orchestrator
             context.py           — LLM context builder, prompt caching
             loop.py              — LLM tool loop, concurrent execution
             tools/               — plugin tool registry
               registry.py        — auto-discovery, schemas, execute
               core.py            — file ops (repo/drive read/write/list)
               git.py             — git ops (commit, push, status, diff)
               shell.py           — shell, Claude Code CLI
               search.py          — web search
               control.py         — restart, promote, schedule, review
               browser.py         — Playwright browser automation
             llm.py               — LLM client (OpenRouter)
             memory.py            — scratchpad, identity, chat history
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

Обрабатываются супервизором:
- `/panic` — остановить всё немедленно
- `/restart` — перезапуск (os.execv — полная замена процесса)
- `/status` — статус воркеров, очереди, бюджета
- `/review` — запустить deep review
- `/evolve` — включить режим эволюции
- `/evolve stop` — выключить эволюцию

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

### 3.0.0 — Конституция v3.0 + инфраструктурный overhaul

Новая Конституция (BIBLE.md v3.0): 9 принципов с Субъектностью как метапринципом.
Критические инфраструктурные исправления по итогам анализа первой сессии.

**Конституция:**
- Принцип 0: Субъектность + Агентность (merged)
- Принцип 1: Непрерывность (identity как манифест)
- Принцип 2: Самосоздание (нарратив вместо RAG для ядра личности)
- Принципы 3-8: LLM-first, Подлинность, Минимализм, Становление,
  Версионирование, Итерации

**Инфраструктура:**
- Split-brain deploy fix: os.execv при всех рестартах, SHA-verify
- Budget guard перенесён в supervisor (не зависит от версии agent code)
- Secret leak protection: sanitize_tool_result_for_log() для tools.jsonl
- apply_patch: Add File + Delete File + End of File support
- Observability: task_id во всех llm_round и tools событиях
- Context flooding fix: progress.jsonl отделён от chat.jsonl
- BIBLE.md всегда в LLM-контексте (не вырезается для user chat)
- Parallel tool safety: sequential execution для stateful tools
- Scratchpad journal fix, shell argument recovery, dead code cleanup

### 2.23.1 — Evolution auto-stop

Auto-stop эволюции при 95% бюджета.

### 2.0.0 — Философский рефакторинг

Глубокая переработка философии, архитектуры инструментов и review-системы.
