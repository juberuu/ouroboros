"""
Ouroboros agent core — thin orchestrator.

Delegates to: loop.py (LLM tool loop), tools/ (tool schemas/execution),
llm.py (LLM calls), memory.py (scratchpad/identity),
context.py (context building), review.py (code collection/metrics).
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

from ouroboros.utils import (
    utc_now_iso, read_text, append_jsonl,
    safe_relpath, truncate_for_log,
    get_git_info, sanitize_task_for_event,
)
from ouroboros.llm import LLMClient, add_usage
from ouroboros.tools import ToolRegistry
from ouroboros.tools.registry import ToolContext
from ouroboros.memory import Memory
from ouroboros.context import build_llm_messages
from ouroboros.loop import run_llm_loop


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
        self._last_progress_ts: float = 0.0
        self._task_started_ts: float = 0.0

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
            self._verify_restart(git_sha)
            self._verify_system_state(git_sha)
        except Exception:
            log.warning("Worker boot logging failed", exc_info=True)
            return

    def _verify_restart(self, git_sha: str) -> None:
        """Best-effort restart verification."""
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
                log.debug("Failed to log restart verify event", exc_info=True)
                pass
            try:
                claim_path.unlink()
            except Exception:
                log.debug("Failed to delete restart verify claim file", exc_info=True)
                pass
        except Exception:
            log.debug("Restart verification failed", exc_info=True)
            pass

    def _verify_system_state(self, git_sha: str) -> None:
        """Bible Principle 1: verify system state on every startup.

        Checks:
        - Uncommitted changes (auto-rescue commit & push)
        - VERSION file sync with git tags
        - Budget remaining (warning thresholds)
        """
        import re
        import subprocess
        checks = {}
        issues = 0
        drive_logs = self.env.drive_path("logs")

        # 1. Uncommitted changes
        try:
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=str(self.env.repo_dir),
                capture_output=True, text=True, timeout=10, check=True
            )
            dirty_files = [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
            if dirty_files:
                issues += 1
                # Auto-rescue: commit and push
                auto_committed = False
                try:
                    # Only stage tracked files (not secrets/notebooks)
                    subprocess.run(["git", "add", "-u"], cwd=str(self.env.repo_dir), timeout=10, check=True)
                    subprocess.run(
                        ["git", "commit", "-m", "auto-rescue: uncommitted changes detected on startup"],
                        cwd=str(self.env.repo_dir), timeout=30, check=True
                    )
                    # Validate branch name
                    if not re.match(r'^[a-zA-Z0-9_/-]+$', self.env.branch_dev):
                        raise ValueError(f"Invalid branch name: {self.env.branch_dev}")
                    # Pull with rebase before push
                    subprocess.run(
                        ["git", "pull", "--rebase", "origin", self.env.branch_dev],
                        cwd=str(self.env.repo_dir), timeout=60, check=True
                    )
                    # Push
                    try:
                        subprocess.run(
                            ["git", "push", "origin", self.env.branch_dev],
                            cwd=str(self.env.repo_dir), timeout=60, check=True
                        )
                        auto_committed = True
                        log.warning(f"Auto-rescued {len(dirty_files)} uncommitted files on startup")
                    except subprocess.CalledProcessError:
                        # If push fails, undo the commit
                        subprocess.run(
                            ["git", "reset", "HEAD~1"],
                            cwd=str(self.env.repo_dir), timeout=10, check=True
                        )
                        raise
                except Exception as e:
                    log.warning(f"Failed to auto-rescue uncommitted changes: {e}", exc_info=True)
                checks["uncommitted_changes"] = {
                    "status": "warning", "files": dirty_files[:20],
                    "auto_committed": auto_committed,
                }
            else:
                checks["uncommitted_changes"] = {"status": "ok"}
        except Exception as e:
            checks["uncommitted_changes"] = {"status": "error", "error": str(e)}

        # 2. VERSION vs git tag
        try:
            version_file = read_text(self.env.repo_path("VERSION")).strip()
            result = subprocess.run(
                ["git", "describe", "--tags", "--abbrev=0"],
                cwd=str(self.env.repo_dir),
                capture_output=True, text=True, timeout=10
            )
            if result.returncode != 0:
                # No tags found
                checks["version_sync"] = {
                    "status": "warning",
                    "message": "no_tags",
                    "version_file": version_file,
                }
            else:
                latest_tag = result.stdout.strip().lstrip('v')
                if version_file != latest_tag:
                    issues += 1
                    checks["version_sync"] = {
                        "status": "warning",
                        "version_file": version_file,
                        "latest_tag": latest_tag,
                    }
                else:
                    checks["version_sync"] = {"status": "ok", "version": version_file}
        except Exception as e:
            checks["version_sync"] = {"status": "error", "error": str(e)}

        # 3. Budget check
        try:
            state_path = self.env.drive_path("state") / "state.json"
            state_data = json.loads(read_text(state_path))
            total_budget_str = os.environ.get("TOTAL_BUDGET", "")

            # Handle unset or zero budget gracefully
            if not total_budget_str or float(total_budget_str) == 0:
                checks["budget"] = {"status": "unconfigured"}
            else:
                total_budget = float(total_budget_str)
                spent = float(state_data.get("spent_usd", 0))
                remaining = max(0, total_budget - spent)

                if remaining < 10:
                    status = "emergency"
                    issues += 1
                elif remaining < 50:
                    status = "critical"
                    issues += 1
                elif remaining < 100:
                    status = "warning"
                else:
                    status = "ok"

                checks["budget"] = {
                    "status": status,
                    "remaining_usd": round(remaining, 2),
                    "total_usd": total_budget,
                    "spent_usd": round(spent, 2),
                }
        except Exception as e:
            checks["budget"] = {"status": "error", "error": str(e)}

        # Log verification result
        event = {
            "ts": utc_now_iso(),
            "type": "startup_verification",
            "checks": checks,
            "issues_count": issues,
            "git_sha": git_sha,
        }
        append_jsonl(drive_logs / "events.jsonl", event)

        if issues > 0:
            log.warning(f"Startup verification found {issues} issue(s): {checks}")

    # =====================================================================
    # Main entry point
    # =====================================================================

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._busy = True
        start_time = time.time()
        self._task_started_ts = start_time
        self._last_progress_ts = start_time
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
                try:
                    append_jsonl(drive_logs / "events.jsonl", {
                        "ts": utc_now_iso(), "type": "context_soft_cap_trim",
                        "task_id": task.get("id"), **cap_info,
                    })
                except Exception:
                    log.warning("Failed to log context soft cap trim event", exc_info=True)
                    pass

            # Read budget remaining for cost guard
            budget_remaining = None
            try:
                state_path = self.env.drive_path("state") / "state.json"
                state_data = json.loads(read_text(state_path))
                total_budget = float(os.environ.get("TOTAL_BUDGET", 0))
                spent = float(state_data.get("spent_usd", 0))
                if total_budget > 0:
                    budget_remaining = max(0, total_budget - spent)
            except Exception:
                pass

            # --- LLM loop (delegated to loop.py) ---
            usage: Dict[str, Any] = {}
            llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}

            # Set initial reasoning effort based on task type
            task_type_str = str(task.get("type") or "").lower()
            if task_type_str in ("evolution", "review"):
                initial_effort = "high"
            else:
                initial_effort = "medium"

            try:
                text, usage, llm_trace = run_llm_loop(
                    messages=messages,
                    tools=self.tools,
                    llm=self.llm,
                    drive_logs=drive_logs,
                    emit_progress=self._emit_progress,
                    incoming_messages=self._incoming_messages,
                    task_type=task_type_str,
                    task_id=str(task.get("id") or ""),
                    budget_remaining_usd=budget_remaining,
                    event_queue=self._event_queue,
                    initial_effort=initial_effort,
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

            # Emit events for supervisor
            self._emit_task_results(task, text, usage, llm_trace, start_time, drive_logs)
            return list(self._pending_events)

        finally:
            self._busy = False
            # Clean up browser if it was used during this task
            try:
                from ouroboros.tools.browser import cleanup_browser
                cleanup_browser(self.tools._ctx)
            except Exception:
                log.debug("Failed to cleanup browser", exc_info=True)
                pass
            while not self._incoming_messages.empty():
                try:
                    self._incoming_messages.get_nowait()
                except queue.Empty:
                    break
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._current_task_type = None

    # =====================================================================
    # Task result emission
    # =====================================================================

    def _emit_task_results(
        self, task: Dict[str, Any], text: str,
        usage: Dict[str, Any], llm_trace: Dict[str, Any],
        start_time: float, drive_logs: pathlib.Path,
    ) -> None:
        """Emit all end-of-task events to supervisor."""
        self._pending_events.append({
            "type": "llm_usage", "task_id": task.get("id"),
            "provider": "openrouter", "usage": usage, "ts": utc_now_iso(),
        })

        self._pending_events.append({
            "type": "send_message", "chat_id": task["chat_id"],
            "text": text or "\u200b", "log_text": text or "",
            "format": "markdown",
            "task_id": task.get("id"), "ts": utc_now_iso(),
        })

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
            log.warning("Failed to log task eval event", exc_info=True)
            pass

        self._pending_events.append({
            "type": "task_metrics",
            "task_id": task.get("id"), "task_type": task.get("type"),
            "duration_sec": duration_sec,
            "tool_calls": n_tool_calls, "tool_errors": n_tool_errors,
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_rounds": int(usage.get("rounds") or 0),
            "ts": utc_now_iso(),
        })

        self._pending_events.append({
            "type": "task_done",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "total_rounds": int(usage.get("rounds") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "ts": utc_now_iso(),
        })
        append_jsonl(drive_logs / "events.jsonl", {
            "ts": utc_now_iso(),
            "type": "task_done",
            "task_id": task.get("id"),
            "task_type": task.get("type"),
            "cost_usd": round(float(usage.get("cost") or 0), 6),
            "total_rounds": int(usage.get("rounds") or 0),
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
        })

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
        self._last_progress_ts = time.time()
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "send_message", "chat_id": self._current_chat_id,
                "text": f"💬 {text}", "format": "markdown", "is_progress": True,
                "ts": utc_now_iso(),
            })
        except Exception:
            log.warning("Failed to emit progress event", exc_info=True)
            pass

    def _emit_typing_start(self) -> None:
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "typing_start", "chat_id": self._current_chat_id,
                "ts": utc_now_iso(),
            })
        except Exception:
            log.warning("Failed to emit typing start event", exc_info=True)
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
            log.warning("Failed to emit task heartbeat event", exc_info=True)
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
