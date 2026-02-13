"""
Ouroboros agent core — thin orchestrator.

Delegates to: tools.py (tool schemas/execution), llm.py (LLM calls),
memory.py (scratchpad/identity), review.py (deep review).
"""

from __future__ import annotations

import html
import json
import os
import pathlib
import re
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl,
    safe_relpath, truncate_for_log, clip_text, estimate_tokens,
    get_git_info, sanitize_task_for_event, sanitize_tool_args_for_log,
)
from ouroboros.llm import LLMClient, normalize_reasoning_effort, reasoning_rank
from ouroboros.tools import ToolRegistry, ToolContext
from ouroboros.memory import Memory
from ouroboros.review import ReviewEngine


# ---------------------------------------------------------------------------
# Module-level guard for one-time worker boot logging
# ---------------------------------------------------------------------------
_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Environment + Paths
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class OuroborosAgent:
    """One agent instance per worker process. Mostly stateless; long-term state lives on Drive."""

    def __init__(self, env: Env, event_queue: Any = None):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []
        self._event_queue: Any = event_queue
        self._current_chat_id: Optional[int] = None
        self._current_task_type: Optional[str] = None

        # SSOT modules
        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)
        self.review = ReviewEngine(llm=self.llm, repo_dir=env.repo_dir, drive_root=env.drive_root)

        self._log_worker_boot_once()

    def _log_worker_boot_once(self) -> None:
        global _worker_boot_logged
        try:
            with _worker_boot_lock:
                if _worker_boot_logged:
                    return
                _worker_boot_logged = True
            git_branch, git_sha = get_git_info(self.env.repo_dir)
            append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                'ts': utc_now_iso(), 'type': 'worker_boot',
                'pid': os.getpid(), 'git_branch': git_branch, 'git_sha': git_sha,
            })
            # Restart verification (best-effort)
            try:
                pending_path = self.env.drive_path('state') / 'pending_restart_verify.json'
                claim_path = pending_path.with_name(f"pending_restart_verify.claimed.{os.getpid()}.json")
                try:
                    os.rename(str(pending_path), str(claim_path))
                except (FileNotFoundError, Exception):
                    return
                try:
                    claim_data = json.loads(read_text(claim_path))
                    expected_sha = str(claim_data.get("expected_sha", "")).strip()
                    ok = bool(expected_sha and expected_sha == git_sha)
                    append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                        'ts': utc_now_iso(), 'type': 'restart_verify',
                        'pid': os.getpid(), 'ok': ok,
                        'expected_sha': expected_sha, 'observed_sha': git_sha,
                    })
                except Exception:
                    pass
                try:
                    claim_path.unlink()
                except Exception:
                    pass
            except Exception:
                pass
        except Exception:
            return

    # =====================================================================
    # Main entry point
    # =====================================================================

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        start_time = time.time()
        self._pending_events = []
        self._current_chat_id = int(task.get("chat_id") or 0) or None
        self._current_task_type = str(task.get("type") or "")

        drive_logs = self.env.drive_path("logs")
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})

        # Set tool context for this task
        ctx = ToolContext(
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            emit_progress_fn=self._emit_progress,
        )
        self.tools.set_context(ctx)

        # Typing indicator
        typing_stop: Optional[threading.Event] = None
        heartbeat_stop = self._start_task_heartbeat_loop(str(task.get("id") or ""))
        try:
            chat_id = int(task.get("chat_id"))
            typing_stop = self._start_typing_loop(chat_id)
        except Exception:
            pass

        try:
            # Review tasks use dedicated pipeline
            if str(task.get("type") or "") == "review":
                return self._handle_review_task(task, start_time, drive_logs)

            # --- Build context ---
            base_prompt = self._safe_read(self.env.repo_path("prompts/SYSTEM.md"),
                                          fallback="You are Ouroboros. Your base prompt could not be loaded.")
            bible_md = self._safe_read(self.env.repo_path("BIBLE.md"))
            readme_md = self._safe_read(self.env.repo_path("README.md"))
            state_json = self._safe_read(self.env.drive_path("state/state.json"), fallback="{}")
            self.memory.ensure_files()
            scratchpad_raw = self.memory.load_scratchpad()
            identity_raw = self.memory.load_identity()

            # Summarize logs
            chat_summary = self.memory.summarize_chat(
                self.memory.read_jsonl_tail("chat.jsonl", 200))
            tools_summary = self.memory.summarize_tools(
                self.memory.read_jsonl_tail("tools.jsonl", 200))
            events_summary = self.memory.summarize_events(
                self.memory.read_jsonl_tail("events.jsonl", 200))
            supervisor_summary = self.memory.summarize_supervisor(
                self.memory.read_jsonl_tail("supervisor.jsonl", 200))

            # Git context
            try:
                git_branch, git_sha = get_git_info(self.env.repo_dir)
            except Exception:
                git_branch, git_sha = "unknown", "unknown"

            runtime_ctx = json.dumps({
                "utc_now": utc_now_iso(),
                "repo_dir": str(self.env.repo_dir),
                "drive_root": str(self.env.drive_root),
                "git_head": git_sha, "git_branch": git_branch,
                "task": {"id": task.get("id"), "type": task.get("type")},
            }, ensure_ascii=False, indent=2)

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": base_prompt},
                {"role": "system", "content": "## BIBLE.md\n\n" + clip_text(bible_md, 180000)},
                {"role": "system", "content": "## README.md\n\n" + clip_text(readme_md, 180000)},
                {"role": "system", "content": "## Drive state\n\n" + clip_text(state_json, 90000)},
                {"role": "system", "content": "## Scratchpad\n\n" + clip_text(scratchpad_raw, 90000)},
                {"role": "system", "content": "## Identity\n\n" + clip_text(identity_raw, 80000)},
                {"role": "system", "content": "## Runtime context\n\n" + runtime_ctx},
            ]
            if chat_summary:
                messages.append({"role": "system", "content": "## Recent chat\n\n" + chat_summary})
            if tools_summary:
                messages.append({"role": "system", "content": "## Recent tools\n\n" + tools_summary})
            if events_summary:
                messages.append({"role": "system", "content": "## Recent events\n\n" + events_summary})
            if supervisor_summary:
                messages.append({"role": "system", "content": "## Supervisor\n\n" + supervisor_summary})
            messages.append({"role": "user", "content": task.get("text", "")})

            # Soft-cap token trimming
            messages, cap_info = self._apply_message_token_soft_cap(messages, 200000)
            if cap_info.get("trimmed_sections"):
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "context_soft_cap_trim",
                    "task_id": task.get("id"), **cap_info,
                })

            tool_schemas = self.tools.schemas()

            # --- LLM loop ---
            usage: Dict[str, Any] = {}
            llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
            try:
                text, usage, llm_trace = self._llm_with_tools(
                    messages=messages, tools=tool_schemas,
                    task_type=str(task.get("type") or ""),
                )
            except Exception as e:
                tb = traceback.format_exc()
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "task_error",
                    "task_id": task.get("id"), "error": repr(e),
                    "traceback": truncate_for_log(tb, 2000),
                })
                text = f"⚠️ Ошибка при обработке: {type(e).__name__}: {e}"

            # Empty response guard
            if not isinstance(text, str) or not text.strip():
                text = "⚠️ Модель вернула пустой ответ. Попробуй переформулировать запрос."

            self._pending_events.append({
                "type": "llm_usage", "task_id": task.get("id"),
                "provider": "openrouter", "usage": usage, "ts": utc_now_iso(),
            })

            # Memory update (best-effort)
            self._update_memory_after_task(task, text, llm_trace)

            # Send response via Telegram (direct HTML if possible)
            direct_sent = self._try_direct_send(task, text)
            text_for_supervisor = "\u200b" if direct_sent else self._strip_markdown(text)
            if not text_for_supervisor or not text_for_supervisor.strip():
                text_for_supervisor = "\u200b"

            self._pending_events.append({
                "type": "send_message", "chat_id": task["chat_id"],
                "text": text_for_supervisor, "log_text": text or "",
                "task_id": task.get("id"), "ts": utc_now_iso(),
            })

            # Task eval event
            duration_sec = round(time.time() - start_time, 3)
            n_tool_calls = len(llm_trace.get("tool_calls", []))
            n_tool_errors = sum(1 for tc in llm_trace.get("tool_calls", [])
                                if isinstance(tc, dict) and tc.get("is_error"))
            try:
                append_jsonl(drive_logs / "events.jsonl", {
                    "ts": utc_now_iso(), "type": "task_eval", "ok": True,
                    "task_id": task.get("id"), "task_type": task.get("type"),
                    "duration_sec": duration_sec,
                    "tool_calls": n_tool_calls,
                    "tool_errors": n_tool_errors,
                    "direct_send_ok": direct_sent,
                    "response_len": len(text),
                })
            except Exception:
                pass

            # Task metrics for supervisor
            self._pending_events.append({
                "type": "task_metrics",
                "task_id": task.get("id"), "task_type": task.get("type"),
                "duration_sec": duration_sec,
                "tool_calls": n_tool_calls, "tool_errors": n_tool_errors,
                "ts": utc_now_iso(),
            })

            self._pending_events.append({"type": "task_done", "task_id": task.get("id"), "ts": utc_now_iso()})
            append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_done", "task_id": task.get("id")})
            return list(self._pending_events)

        finally:
            if typing_stop is not None:
                typing_stop.set()
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._current_task_type = None

    # =====================================================================
    # LLM loop with tools
    # =====================================================================

    def _llm_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        task_type: str = "",
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        drive_logs = self.env.drive_path("logs")

        profile_name = self.llm.select_task_profile(task_type)
        profile_cfg = self.llm.model_profile(profile_name)
        active_model = profile_cfg["model"]
        active_effort = profile_cfg["effort"]

        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
        last_usage: Dict[str, Any] = {}
        max_retries = 3
        soft_check_interval = 15

        def _safe_args(v: Any) -> Any:
            try:
                return json.loads(json.dumps(v, ensure_ascii=False, default=str))
            except Exception:
                return {"_repr": repr(v)}

        def _maybe_raise_effort(target: str) -> None:
            nonlocal active_effort
            t = normalize_reasoning_effort(target, default=active_effort)
            if reasoning_rank(t) > reasoning_rank(active_effort):
                active_effort = t

        def _switch_to_code_profile() -> None:
            nonlocal active_model, active_effort
            code_cfg = self.llm.model_profile("code_task")
            if code_cfg["model"] != active_model or reasoning_rank(code_cfg["effort"]) > reasoning_rank(active_effort):
                active_model = code_cfg["model"]
                active_effort = max(active_effort, code_cfg["effort"], key=reasoning_rank)

        round_idx = 0
        while True:
            round_idx += 1

            # Self-check
            if round_idx > 1 and round_idx % soft_check_interval == 0:
                messages.append({"role": "system", "content":
                    f"[Self-check] {round_idx} раундов. Оцени прогресс. Если застрял — смени подход."})

            # Escalate reasoning effort for long tasks
            if round_idx >= 5:
                _maybe_raise_effort("high")
            if round_idx >= 10:
                _maybe_raise_effort("xhigh")

            # --- LLM call with retry ---
            msg = None
            last_error: Optional[Exception] = None
            for attempt in range(max_retries):
                try:
                    resp_msg, usage = self.llm.chat(
                        messages=messages, model=active_model, tools=tools,
                        reasoning_effort=active_effort,
                    )
                    msg = resp_msg
                    last_usage = usage
                    break
                except Exception as e:
                    last_error = e
                    append_jsonl(drive_logs / "events.jsonl", {
                        "ts": utc_now_iso(), "type": "llm_api_error",
                        "round": round_idx, "attempt": attempt + 1,
                        "model": active_model, "error": repr(e),
                    })
                    if attempt < max_retries - 1:
                        time.sleep(min(2 ** attempt * 2, 30))

            if msg is None:
                return (
                    f"⚠️ Не удалось получить ответ от модели после {max_retries} попыток.\n"
                    f"Ошибка: {last_error}"
                ), last_usage, llm_trace

            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if tool_calls:
                messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

                if content and content.strip():
                    self._emit_progress(content.strip())
                    llm_trace["assistant_notes"].append(content.strip()[:320])

                saw_code_tool = False
                error_count = 0

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    if fn_name in self.tools.CODE_TOOLS:
                        saw_code_tool = True

                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError) as e:
                        result = f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}"
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                        llm_trace["tool_calls"].append({"tool": fn_name, "args": {}, "result": result, "is_error": True})
                        error_count += 1
                        continue

                    args_for_log = sanitize_tool_args_for_log(fn_name, args if isinstance(args, dict) else {})

                    # Execute via ToolRegistry (SSOT)
                    tool_ok = True
                    try:
                        result = self.tools.execute(fn_name, args)
                    except Exception as e:
                        tool_ok = False
                        result = f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}"
                        append_jsonl(drive_logs / "events.jsonl", {
                            "ts": utc_now_iso(), "type": "tool_error",
                            "tool": fn_name, "args": args_for_log, "error": repr(e),
                        })

                    append_jsonl(drive_logs / "tools.jsonl", {
                        "ts": utc_now_iso(), "tool": fn_name,
                        "args": args_for_log, "result_preview": truncate_for_log(result, 2000),
                    })
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    is_error = (not tool_ok) or str(result).startswith("⚠️")
                    llm_trace["tool_calls"].append({
                        "tool": fn_name, "args": _safe_args(args_for_log),
                        "result": truncate_for_log(result, 700), "is_error": is_error,
                    })
                    if is_error:
                        error_count += 1

                if saw_code_tool:
                    _switch_to_code_profile()
                if error_count >= 2:
                    _maybe_raise_effort("high")
                if error_count >= 4:
                    _maybe_raise_effort("xhigh")

                continue

            # No tool calls — final response
            if content and content.strip():
                llm_trace["assistant_notes"].append(content.strip()[:320])
            return (content or ""), last_usage, llm_trace

        return "", last_usage, llm_trace

    # =====================================================================
    # Review task
    # =====================================================================

    def _handle_review_task(
        self, task: Dict[str, Any], start_time: float, drive_logs: pathlib.Path,
    ) -> List[Dict[str, Any]]:
        text = ""
        usage: Dict[str, Any] = {}
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
        try:
            text, usage, llm_trace = self.review.run_review(task)
        except Exception as e:
            append_jsonl(drive_logs / "events.jsonl", {
                "ts": utc_now_iso(), "type": "review_task_error",
                "task_id": task.get("id"), "error": repr(e),
            })
            text = f"⚠️ REVIEW_ERROR: {type(e).__name__}: {e}"

        if usage:
            self._pending_events.append({
                "type": "llm_usage", "task_id": task.get("id"),
                "provider": "openrouter", "usage": usage, "ts": utc_now_iso(),
            })

        self._update_memory_after_task(task, text, llm_trace)
        self._pending_events.append({
            "type": "send_message", "chat_id": task["chat_id"],
            "text": self._strip_markdown(text) if text else "\u200b",
            "log_text": text or "", "task_id": task.get("id"), "ts": utc_now_iso(),
        })

        duration_sec = round(time.time() - start_time, 3)
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(), "type": "task_eval", "ok": True,
            "task_id": task.get("id"), "task_type": "review",
            "duration_sec": duration_sec, "tool_calls": 0, "tool_errors": 0,
        })

        # Task metrics for supervisor
        self._pending_events.append({
            "type": "task_metrics",
            "task_id": task.get("id"), "task_type": "review",
            "duration_sec": duration_sec,
            "tool_calls": 0, "tool_errors": 0,
            "ts": utc_now_iso(),
        })

        self._pending_events.append({"type": "task_done", "task_id": task.get("id"), "ts": utc_now_iso()})
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_done", "task_id": task.get("id")})
        return list(self._pending_events)

    # =====================================================================
    # Memory update after task (deterministic, no extra LLM call)
    # =====================================================================

    def _update_memory_after_task(self, task: Dict[str, Any], final_text: str, llm_trace: Dict[str, Any]) -> None:
        try:
            self.memory.ensure_files()
            delta = self._deterministic_scratchpad_delta(task, final_text, llm_trace)
            current = self.memory.load_scratchpad()
            merged = self.memory.parse_scratchpad(current)

            # Apply delta
            field_map = {
                "CurrentProjects": "project_updates",
                "OpenThreads": "open_threads",
                "InvestigateLater": "investigate_later",
                "RecentEvidence": "evidence_quotes",
            }
            limits = {"CurrentProjects": 12, "OpenThreads": 18, "InvestigateLater": 24, "RecentEvidence": 20}

            for section, field in field_map.items():
                new_items = delta.get(field) or []
                combined = (merged.get(section) or []) + new_items
                # Dedupe
                seen: set[str] = set()
                deduped: List[str] = []
                for item in combined:
                    key = re.sub(r"\s+", " ", item.strip()).lower()
                    if key not in seen:
                        seen.add(key)
                        deduped.append(item)
                merged[section] = deduped[:limits[section]]

            new_text = self.memory.render_scratchpad(merged)
            self.memory.save_scratchpad(new_text)
            self.memory.append_journal({
                "ts": utc_now_iso(), "task_id": task.get("id"),
                "task_type": task.get("type"),
                "task_text_preview": truncate_for_log(str(task.get("text") or ""), 600),
                "delta": delta,
            })
        except Exception as e:
            append_jsonl(self.env.drive_path("logs") / "events.jsonl", {
                "ts": utc_now_iso(), "type": "memory_update_error",
                "task_id": task.get("id"), "error": repr(e),
            })

    @staticmethod
    def _deterministic_scratchpad_delta(
        task: Dict[str, Any], final_text: str, llm_trace: Dict[str, Any],
    ) -> Dict[str, List[str]]:
        task_text = re.sub(r"\s+", " ", str(task.get("text") or "").strip())
        answer = re.sub(r"\s+", " ", str(final_text or "").strip())

        project_updates: List[str] = []
        if task_text:
            project_updates.append(f"Task: {task_text[:320]}")
        if answer:
            project_updates.append(f"Result: {answer[:320]}")

        evidence_quotes: List[str] = []
        open_threads: List[str] = []

        for call in (llm_trace.get("tool_calls") or [])[:24]:
            tool_name = str(call.get("tool") or "?")
            result = str(call.get("result") or "")
            is_error = bool(call.get("is_error"))
            first_line = result.splitlines()[0].strip() if result else ""
            if first_line:
                if len(first_line) > 300:
                    first_line = first_line[:297] + "..."
                evidence_quotes.append(f"`{tool_name}` -> {first_line}")
                if is_error or first_line.startswith("⚠️"):
                    open_threads.append(f"Resolve {tool_name} issue: {first_line[:220]}")

        return {
            "project_updates": project_updates[:12],
            "open_threads": open_threads[:16],
            "investigate_later": [],
            "evidence_quotes": evidence_quotes[:20],
        }

    # =====================================================================
    # Telegram helpers
    # =====================================================================

    def _try_direct_send(self, task: Dict[str, Any], text: str) -> bool:
        """Try to send formatted message directly via Telegram HTML. Returns True if successful."""
        try:
            chat_id = int(task["chat_id"])
            chunks = self._chunk_markdown_for_telegram(text or "", max_chars=3200)
            chunks = [c for c in chunks if isinstance(c, str) and c.strip()]
            if not chunks:
                return False

            for md_part in chunks:
                html_text = self._markdown_to_telegram_html(md_part)
                ok, _ = self._telegram_api_post("sendMessage", {
                    "chat_id": chat_id, "text": self._sanitize_telegram_text(html_text),
                    "parse_mode": "HTML", "disable_web_page_preview": "1",
                })
                if not ok:
                    # Fallback to plain text
                    plain = self._strip_markdown(md_part)
                    if not plain.strip():
                        return False
                    ok2, _ = self._telegram_api_post("sendMessage", {
                        "chat_id": chat_id, "text": self._sanitize_telegram_text(plain),
                        "disable_web_page_preview": "1",
                    })
                    if not ok2:
                        return False
            return True
        except Exception:
            return False

    @staticmethod
    def _strip_markdown(text: str) -> str:
        text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        text = re.sub(r"`([^`]+)`", r"\1", text)
        return text

    @staticmethod
    def _markdown_to_telegram_html(md: str) -> str:
        md = md or ""
        fence_re = re.compile(r"```[^\n]*\n([\s\S]*?)```", re.MULTILINE)
        inline_code_re = re.compile(r"`([^`\n]+)`")
        bold_re = re.compile(r"\*\*([^*\n]+)\*\*")

        parts: list[str] = []
        last = 0
        for m in fence_re.finditer(md):
            parts.append(md[last:m.start()])
            code_esc = html.escape(m.group(1), quote=False)
            parts.append(f"<pre><code>{code_esc}</code></pre>")
            last = m.end()
        parts.append(md[last:])

        def _render_span(text: str) -> str:
            out: list[str] = []
            pos = 0
            for mm in inline_code_re.finditer(text):
                out.append(html.escape(text[pos:mm.start()], quote=False))
                out.append(f"<code>{html.escape(mm.group(1), quote=False)}</code>")
                pos = mm.end()
            out.append(html.escape(text[pos:], quote=False))
            return bold_re.sub(r"<b>\1</b>", "".join(out))

        return "".join(_render_span(p) if not p.startswith("<pre><code>") else p for p in parts)

    @staticmethod
    def _sanitize_telegram_text(text: str) -> str:
        if text is None:
            return ""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return "".join(
            c for c in text
            if (ord(c) >= 32 or c in ("\n", "\t")) and not (0xD800 <= ord(c) <= 0xDFFF)
        )

    @staticmethod
    def _tg_utf16_len(text: str) -> int:
        if not text:
            return 0
        return sum(2 if ord(c) > 0xFFFF else 1 for c in text)

    @staticmethod
    def _chunk_markdown_for_telegram(md: str, max_chars: int = 3500) -> List[str]:
        md = md or ""
        max_chars = max(256, min(4096, int(max_chars)))
        lines = md.splitlines(keepends=True)
        chunks: List[str] = []
        cur = ""
        in_fence = False
        fence_open = "```\n"
        fence_close = "```\n"

        def _flush() -> None:
            nonlocal cur
            if cur and cur.strip():
                chunks.append(cur)
            cur = ""

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                if in_fence:
                    fence_open = line if line.endswith("\n") else (line + "\n")

            reserve = OuroborosAgent._tg_utf16_len(fence_close) if in_fence else 0
            if OuroborosAgent._tg_utf16_len(cur) + OuroborosAgent._tg_utf16_len(line) > max_chars - reserve:
                if in_fence and cur:
                    cur += fence_close
                _flush()
                cur = fence_open if in_fence else ""
            cur += line

        if in_fence:
            cur += fence_close
        _flush()
        return chunks or [md]

    # =====================================================================
    # Event emission helpers
    # =====================================================================

    def _emit_progress(self, text: str) -> None:
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "send_message", "chat_id": self._current_chat_id,
                "text": f"💬 {text}", "ts": utc_now_iso(),
            })
        except Exception:
            pass

    def _emit_task_heartbeat(self, task_id: str, phase: str) -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put({
                "type": "task_heartbeat", "task_id": task_id,
                "phase": phase, "ts": utc_now_iso(),
            })
        except Exception:
            pass

    def _start_task_heartbeat_loop(self, task_id: str) -> Optional[threading.Event]:
        if self._event_queue is None or not task_id.strip():
            return None
        interval = 30
        stop = threading.Event()
        self._emit_task_heartbeat(task_id, "start")

        def _loop() -> None:
            while not stop.wait(interval):
                self._emit_task_heartbeat(task_id, "running")

        threading.Thread(target=_loop, daemon=True).start()
        return stop

    def _telegram_api_post(self, method: str, data: Dict[str, Any]) -> Tuple[bool, str]:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False, "no_token"
        url = f"https://api.telegram.org/bot{token}/{method}"
        payload = urllib.parse.urlencode({k: str(v) for k, v in data.items()}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()
            try:
                j = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(j, dict) and j.get("ok") is False:
                    desc = str(j.get("description", ""))
                    return False, f"tg_ok_false: {desc}"
            except Exception:
                pass
            return True, "ok"
        except Exception as e:
            return False, f"exc_{type(e).__name__}: {e}"

    def _send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self._telegram_api_post("sendChatAction", {"chat_id": chat_id, "action": action})

    def _start_typing_loop(self, chat_id: int) -> threading.Event:
        stop = threading.Event()
        self._send_chat_action(chat_id, "typing")

        def _loop() -> None:
            stop.wait(1.0)
            if stop.is_set():
                return
            self._send_chat_action(chat_id, "typing")
            while not stop.wait(4):
                self._send_chat_action(chat_id, "typing")

        threading.Thread(target=_loop, daemon=True).start()
        return stop

    # =====================================================================
    # Helpers
    # =====================================================================

    @staticmethod
    def _safe_read(path: pathlib.Path, fallback: str = "") -> str:
        try:
            if path.exists():
                return read_text(path)
        except Exception:
            pass
        return fallback

    def _apply_message_token_soft_cap(
        self, messages: List[Dict[str, Any]], soft_cap_tokens: int,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        estimated = sum(estimate_tokens(str(m.get("content", ""))) + 6 for m in messages)
        info: Dict[str, Any] = {
            "estimated_tokens_before": estimated, "estimated_tokens_after": estimated,
            "soft_cap_tokens": soft_cap_tokens, "trimmed_sections": [],
        }
        if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
            return messages, info

        prunable = ["## Recent chat", "## Recent tools", "## Recent events", "## Supervisor"]
        pruned = list(messages)
        for prefix in prunable:
            if estimated <= soft_cap_tokens:
                break
            for i, msg in enumerate(pruned):
                content = msg.get("content")
                if isinstance(content, str) and content.startswith(prefix):
                    pruned.pop(i)
                    info["trimmed_sections"].append(prefix)
                    estimated = sum(estimate_tokens(str(m.get("content", ""))) + 6 for m in pruned)
                    break

        info["estimated_tokens_after"] = estimated
        return pruned, info


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)
