"""
Ouroboros agent core — thin orchestrator.

Delegates to: tools/ (tool schemas/execution), llm.py (LLM calls),
memory.py (scratchpad/identity), context.py (context building),
review.py (code collection/metrics).
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl,
    safe_relpath, truncate_for_log,
    get_git_info, sanitize_task_for_event, sanitize_tool_args_for_log,
)
from ouroboros.llm import LLMClient, normalize_reasoning_effort, reasoning_rank, add_usage
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext
from ouroboros.memory import Memory
from ouroboros.context import build_llm_messages


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

        # Message injection: owner can send messages while agent is busy
        self._incoming_messages: queue.Queue = queue.Queue()
        self._busy = False

        # SSOT modules
        self.llm = LLMClient()
        self.tools = ToolRegistry(repo_dir=env.repo_dir, drive_root=env.drive_root)
        self.memory = Memory(drive_root=env.drive_root, repo_dir=env.repo_dir)

        self._log_worker_boot_once()

    def inject_message(self, text: str) -> None:
        """Thread-safe: inject owner message into the active conversation."""
        self._incoming_messages.put(text)

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
        self._busy = True
        start_time = time.time()
        self._pending_events = []
        self._current_chat_id = int(task.get("chat_id") or 0) or None
        self._current_task_type = str(task.get("type") or "")

        drive_logs = self.env.drive_path("logs")
        sanitized_task = sanitize_task_for_event(task, drive_logs)
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})

        # Set tool context for this task
        ctx = ToolContext(
            repo_dir=self.env.repo_dir,
            drive_root=self.env.drive_root,
            branch_dev=self.env.branch_dev,
            pending_events=self._pending_events,
            current_chat_id=self._current_chat_id,
            current_task_type=self._current_task_type,
            emit_progress_fn=self._emit_progress,
        )
        self.tools.set_context(ctx)

        # Typing indicator via event queue (no direct Telegram API)
        self._emit_typing_start()
        heartbeat_stop = self._start_task_heartbeat_loop(str(task.get("id") or ""))

        try:
            # --- Build context (delegated to context.py) ---
            messages, cap_info = build_llm_messages(
                env=self.env,
                memory=self.memory,
                task=task,
                review_context_builder=self._build_review_context,
            )

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

            # Send response via supervisor
            self._pending_events.append({
                "type": "send_message", "chat_id": task["chat_id"],
                "text": text or "\u200b", "log_text": text or "",
                "format": "markdown",
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
                    "response_len": len(text),
                })
            except Exception:
                pass

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
            self._busy = False
            while not self._incoming_messages.empty():
                try:
                    self._incoming_messages.get_nowait()
                except queue.Empty:
                    break
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
        accumulated_usage: Dict[str, Any] = {}
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

            # Inject owner messages received during task execution
            while not self._incoming_messages.empty():
                try:
                    injected = self._incoming_messages.get_nowait()
                    messages.append({"role": "user", "content": injected})
                except queue.Empty:
                    break

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
                    add_usage(accumulated_usage, usage)
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
                ), accumulated_usage, llm_trace

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
            return (content or ""), accumulated_usage, llm_trace

        return "", accumulated_usage, llm_trace

    # =====================================================================
    # Review context builder
    # =====================================================================

    def _build_review_context(self) -> str:
        """Collect code snapshot + complexity metrics for review tasks."""
        try:
            from ouroboros.review import collect_sections, compute_complexity_metrics, format_metrics
            sections, stats = collect_sections(self.env.repo_dir, self.env.drive_root)
            metrics = compute_complexity_metrics(sections)

            parts = [
                "## Code Review Context\n",
                format_metrics(metrics),
                f"\nFiles: {stats['files']}, chars: {stats['chars']}\n",
                "\nUse repo_read to inspect specific files. "
                "Use run_shell for tests. Key files below:\n",
            ]

            total_chars = 0
            max_chars = 80_000
            for path, content in sections:
                if total_chars >= max_chars:
                    parts.append(f"\n... ({len(sections) - len(parts)} more files, use repo_read)")
                    break
                preview = content[:2000] if len(content) > 2000 else content
                file_block = f"\n### {path}\n```\n{preview}\n```\n"
                total_chars += len(file_block)
                parts.append(file_block)

            return "\n".join(parts)
        except Exception as e:
            return f"## Code Review Context\n\n(Failed to collect: {e})\nUse repo_read and repo_list to inspect code."

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

    def _emit_typing_start(self) -> None:
        """Signal supervisor to start typing indicator for current chat."""
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "typing_start", "chat_id": self._current_chat_id,
                "ts": utc_now_iso(),
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


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)
