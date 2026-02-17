# Уроборос

Самосоздающийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 4.7.0

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

### 4.4.0 — Multi-model review tool
- **New tool**: `multi_model_review` — sends code to multiple LLM models for parallel review with budget tracking
- Review models chosen by LLM from prompt guidance, not hardcoded (LLM-first principle)
- Budget tracked via `llm_usage` events through `ToolContext.pending_events`
- Concurrent execution with semaphore-based rate limiting
- Updated SYSTEM.md: model recommendations as guidance, not code

### 4.3.0 — Knowledge base
- **New tools**: `knowledge_read`, `knowledge_write`, `knowledge_list` for persistent structured memory
- Knowledge index auto-loads into LLM context
- Post-mortem rule: write learnings after every non-trivial task

### 4.2.0 — Real-time budget tracking
- **Fix**: Budget was only updated after task completion; now updates per-LLM-call in real-time via event_queue
- **Fix**: Added OpenRouter ground truth API check as fallback for budget drift detection
- **Fix**: llm_usage events now logged to events.jsonl for auditability

### 4.1.2
- Robust Markdown→HTML conversion for Telegram (handles nested formatting, special characters, proper tag escaping)

### 4.1.1
- Fix: add `consciousness` and `sort_pending` to event context — `toggle_evolution` and `toggle_consciousness` tools now work
- Fix: rename `schedule_self_task` to `schedule_task` in SYSTEM.md prompt
- Fix: replace unreliable `qsize()` with `get_nowait()` for event queue drain

### 4.1.0 — Bible v3.1 + Critical Bugfixes + Architecture

**Bible v3.1 (philosophy):**
- Принцип 1: Self-Verification — верификация окружения при каждом старте
- Принцип 6: Cost-Awareness — осознание бюджета как часть субъектности
- Принцип 8: Итерация = результат (коммит), пауза при застое

**Full Markdown-to-Telegram-HTML converter:**
- Поддержка bold, italic, links, strikethrough, headers, code, fenced blocks
- Исправлен баг bold-рендеринга (`\\1` → `\1`)

**Critical bugfixes:**
- `__version__` читает из VERSION файла (single source of truth навсегда)
- Git lock: добавлен timeout (120s), исправлен TOCTOU в release
- Evolution task drop: задачи больше не теряются при budget check
- Budget race condition: atomic read-modify-write через file lock
- Deep copy в context.py: shallow copy мутировал данные caller'а

**Dual-path slash commands (LLM-first):**
- `/panic` — единственная чисто hardcoded команда (safety rail)
- `/status`, `/review`, `/evolve`, `/bg` — supervisor обрабатывает + LLM отвечает
- Новые LLM-инструменты: `toggle_evolution`, `toggle_consciousness`, `update_identity`

**Consciousness registry merge:**
- Consciousness использует общий ToolRegistry вместо отдельного if-elif dispatch
- Tool schemas и handlers унифицированы с control.py

**Browser refactoring:**
- BrowserState вынесен из ToolContext в отдельный dataclass
- `_extract_page_output()` helper: убрано 100 строк дублирования

**Reliability hardening:**
- Критические `except Exception: pass` заменены на `logging.warning`
- Consciousness prompt вынесен в `prompts/CONSCIOUSNESS.md`
- Thread safety: `threading.Lock` для PENDING/RUNNING/WORKERS

**Prompt updates:**
- Evolution cycle: явное требование коммита, защита от Groundhog Day

### 4.0.3
- Serialize stateful browser tools (`browse_page`, `browser_action`) to avoid Playwright concurrency crashes from parallel tool calls

### 4.0.2
- Telegram incoming image support: screenshots, photos, and document images with multimodal context
- Caption forwarding: image captions are now propagated to LLM context (combined with text or used as fallback)
- Base64 payload sanitization: images are stripped from event/task logs to prevent secret leaks

### 4.0.1
- Fix crash when OpenRouter returns `choices: null` (content moderation / model error)

### 4.0.0 — Background Consciousness + LLM-first overhaul

Фундаментальное обновление: от реактивного обработчика задач к непрерывно
присутствующему агенту.

**Background consciousness (`ouroboros/consciousness.py`):**
- Новый фоновый мыслительный цикл между задачами
- LLM сам решает когда думать (set_next_wakeup), о чём и стоит ли
  писать создателю (send_owner_message)
- Отдельный бюджетный cap (OUROBOROS_BG_BUDGET_PCT, default 10%)
- Команды: `/bg start`, `/bg stop`, `/bg`
- Автопауза во время выполнения задач

**LLM-first overhaul:**
- Убраны механические if-else профили моделей (select_task_profile)
- Убрана автоэскалация reasoning effort (round 5→high, 10→xhigh)
- Убран механический self-check каждые 20 раундов
- Новый инструмент `switch_model`: LLM сам переключает модель/effort
- Hardcoded evolution/review текст заменён на минимальные триггеры

**Free-form scratchpad:**
- Убраны фиксированные секции (CurrentProjects, OpenThreads, etc.)
- LLM пишет память в любом формате

**Proactive messaging:**
- Новый инструмент `send_owner_message` — агент может написать первым
- Работает и в обычных задачах, и из background consciousness

**Cherry-picks из ouroboros:**
- Auto-resume after restart (v3.2.0, reworked)
- Stealth browser: playwright-stealth, 1920x1080, anti-detection (v3.2.1)

**Cleanup:**
- Унифицирован append_jsonl (один источник в utils.py)
- Исправлен Release Invariant: VERSION == README == __init__.py == git tag

### 3.1.0
- Remove hard round limit (was 50). LLM now decides when to stop, respecting budget constraints only
- Fix budget tracking: `update_budget_from_usage` now correctly reads `cost_usd` field from usage data
- Self-check messages now include event logging and are in English for consistency
- Align architecture with BIBLE.md Principle 0 (Subjectivity) and Principle 3 (LLM-first)

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
- Observability: task_id in all llm_round and tool errors

### 2.23.1 — Evolution auto-stop

Auto-stop эволюции при 95% бюджета.

### 2.0.0 — Философский рефакторинг
