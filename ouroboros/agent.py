"""
Ouroboros agent core (modifiable).

This module is intentionally self-contained (minimal dependencies) so that Ouroboros can edit it safely.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import json
import re
import os
import pathlib
import subprocess
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# Utilities
# -----------------------------

def utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: List[str], cwd: Optional[pathlib.Path] = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        )
    return res.stdout.strip()


def safe_relpath(p: str) -> str:
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p


def truncate_for_log(s: str, max_chars: int = 4000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2 :]


def list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> Dict[str, Any]:
    base = (root / safe_relpath(rel)).resolve()
    if not base.exists():
        return {"error": f"Path does not exist: {rel}", "hint": "Use repo_list('.') or drive_list('.') to see available paths."}
    if not base.is_dir():
        return {"error": f"Not a directory: {rel}", "hint": "This is a file, not a directory. Use repo_read or drive_read instead."}
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(sorted(base.rglob("*"))):
        if i >= max_entries:
            break
        out.append(
            {
                "path": str(p.relative_to(root)),
                "is_dir": p.is_dir(),
                "size": (p.stat().st_size if p.is_file() else None),
            }
        )
    return {"base": str(base), "count": len(out), "items": out, "truncated": (len(out) >= max_entries)}


# -----------------------------
# Environment + Paths
# -----------------------------

@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"
    branch_stable: str = "ouroboros-stable"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


# -----------------------------
# Agent
# -----------------------------

class OuroborosAgent:
    """
    One agent instance per worker process.

    Mostly stateless; long-term state lives on Drive.
    """

    def __init__(self, env: Env, event_queue: Any = None):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []
        self._event_queue: Any = event_queue  # multiprocessing.Queue for real-time progress
        self._current_chat_id: Optional[int] = None

    def _emit_progress(self, text: str) -> None:
        """Push a progress message to the supervisor queue (best-effort, non-blocking)."""
        if self._event_queue is None or self._current_chat_id is None:
            return
        try:
            self._event_queue.put({
                "type": "send_message",
                "chat_id": self._current_chat_id,
                "text": f"💬 {text}",
                "ts": utc_now_iso(),
            })
        except Exception:
            pass  # best-effort; never crash on progress

    def _safe_read(self, path: pathlib.Path, fallback: str = "") -> str:
        """Read a text file, returning *fallback* on any error (file missing, permission, encoding, etc.)."""
        try:
            if path.exists():
                return read_text(path)
        except Exception:
            pass
        return fallback

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._pending_events = []
        self._current_chat_id = int(task.get("chat_id") or 0) or None

        drive_logs = self.env.drive_path("logs")
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": task})

        # Telegram typing indicator (best-effort).
        # Note: we can't show typing at the exact moment of message receipt (handled by supervisor),
        # but we can show it as soon as a worker starts processing the task.
        typing_stop: Optional[threading.Event] = None
        if os.environ.get("OUROBOROS_TG_TYPING", "1").lower() not in ("0", "false", "no", "off", ""):
            try:
                chat_id = int(task.get("chat_id"))
                typing_stop = self._start_typing_loop(chat_id)
            except Exception as e:
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {"ts": utc_now_iso(), "type": "typing_start_error", "task_id": task.get("id"), "error": repr(e)},
                )

        try:
            # --- Load context (resilient: errors produce fallbacks, not crashes) ---
            _fallback_prompt = (
                "You are Ouroboros. Your base prompt could not be loaded. "
                "Analyze available context, help the owner, and report the loading issue."
            )
            base_prompt = self._safe_read(self.env.repo_path("prompts/BASE.md"), fallback=_fallback_prompt)
            world_md = self._safe_read(self.env.repo_path("WORLD.md"))
            readme_md = self._safe_read(self.env.repo_path("README.md"))
            notes_md = self._safe_read(self.env.drive_path("NOTES.md"))
            state_json = self._safe_read(self.env.drive_path("state/state.json"), fallback="{}")
            index_summaries = self._safe_read(self.env.drive_path("index/summaries.json"))
            chat_log = self._safe_read(self.env.drive_path("logs/chat.jsonl"))

            # Git context (non-fatal if unavailable)
            ctx_warnings: List[str] = []
            try:
                git_head = self._git_head()
            except Exception as e:
                git_head = "unknown"
                ctx_warnings.append(f"git HEAD: {e}")
            try:
                git_branch = self._git_branch()
            except Exception as e:
                git_branch = "unknown"
                ctx_warnings.append(f"git branch: {e}")

            runtime_ctx: Dict[str, Any] = {
                "utc_now": utc_now_iso(),
                "repo_dir": str(self.env.repo_dir),
                "drive_root": str(self.env.drive_root),
                "git_head": git_head,
                "git_branch": git_branch,
                "task": {"id": task.get("id"), "type": task.get("type")},
            }
            if ctx_warnings:
                runtime_ctx["context_loading_warnings"] = ctx_warnings

            messages = [
                {"role": "system", "content": base_prompt},
                {"role": "system", "content": "## WORLD.md\n\n" + world_md},
                {"role": "system", "content": "## README.md\n\n" + readme_md},
                {"role": "system", "content": "## Drive state (state/state.json)\n\n" + state_json},
                {"role": "system", "content": "## NOTES.md (Drive)\n\n" + notes_md},
                {"role": "system", "content": "## Index summaries (Drive: index/summaries.json)\n\n" + index_summaries},
                {"role": "system", "content": "## Runtime context (JSON)\n\n" + json.dumps(runtime_ctx, ensure_ascii=False, indent=2)},
                {"role": "system", "content": "## Raw chat log (Drive: logs/chat.jsonl)\n\n" + chat_log},
                {"role": "user", "content": task.get("text", "")},
            ]

            tools = self._tools_schema()

            usage: Dict[str, Any] = {}
            try:
                text, usage = self._llm_with_tools(messages=messages, tools=tools)
            except Exception as e:
                tb = traceback.format_exc()
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "task_error",
                        "task_id": task.get("id"),
                        "error": repr(e),
                        "traceback": truncate_for_log(tb, 2000),
                    },
                )
                text = (
                    f"⚠️ Ошибка при обработке: {type(e).__name__}: {e}\n\n"
                    f"Залогировал traceback. Попробуй повторить запрос — "
                    f"я постараюсь обработать его по-другому."
                )

            self._pending_events.append(
                {
                    "type": "llm_usage",
                    "task_id": task.get("id"),
                    "provider": "openrouter",
                    "usage": usage,
                    "ts": utc_now_iso(),
                }
            )

            # Telegram formatting: render Markdown -> Telegram HTML directly from the worker (best-effort).
            # Rationale: supervisor currently sends plain text; parse_mode is not guaranteed there.
            direct_sent = False
            if os.environ.get("OUROBOROS_TG_MARKDOWN", "1").lower() not in ("0", "false", "no", "off", ""):
                try:
                    chat_id_int = int(task["chat_id"])
                    html_text = self._markdown_to_telegram_html(text)
                    ok, status = self._telegram_send_message_html(chat_id_int, html_text)
                    direct_sent = bool(ok)
                    append_jsonl(
                        self.env.drive_path("logs") / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "telegram_send_direct",
                            "task_id": task.get("id"),
                            "chat_id": chat_id_int,
                            "ok": ok,
                            "status": status,
                        },
                    )
                except Exception as e:
                    append_jsonl(
                        self.env.drive_path("logs") / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "telegram_send_direct_error",
                            "task_id": task.get("id"),
                            "error": repr(e),
                        },
                    )

            # If we sent the formatted message directly, ask supervisor to send only the budget line.
            # We must send a non-empty text, otherwise Telegram rejects it.
            if direct_sent:
                text_for_supervisor = "\u200b"
            else:
                # Strip markdown for plain-text fallback so raw ** and ``` don't clutter the message
                text_for_supervisor = self._strip_markdown(text) if text else text

            self._pending_events.append(
                {
                    "type": "send_message",
                    "chat_id": task["chat_id"],
                    "text": text_for_supervisor,
                    "task_id": task.get("id"),
                    "ts": utc_now_iso(),
                }
            )

            self._pending_events.append({"type": "task_done", "task_id": task.get("id"), "ts": utc_now_iso()})
            append_jsonl(
                drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_done", "task_id": task.get("id")}
            )
            return list(self._pending_events)
        finally:
            if typing_stop is not None:
                typing_stop.set()

    # ---------- git helpers ----------

    def _git_head(self) -> str:
        return run(["git", "rev-parse", "HEAD"], cwd=self.env.repo_dir)

    def _git_branch(self) -> str:
        return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.env.repo_dir)

    # ---------- telegram helpers (direct API calls) ----------

    @staticmethod
    def _strip_markdown(text: str) -> str:
        """Remove common markdown formatting for plain-text fallback."""
        # Remove code fences (```lang\n...\n```)
        text = re.sub(r"```[^\n]*\n([\s\S]*?)```", r"\1", text)
        # Remove bold **text**
        text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
        # Remove inline code `text`
        text = re.sub(r"`([^`]+)`", r"\1", text)
        return text

    def _markdown_to_telegram_html(self, md: str) -> str:
        """Convert a small, safe subset of Markdown into Telegram-compatible HTML.

        Supported (best-effort):
          - **bold** -> <b>
          - `inline code` -> <code>
          - ```code blocks``` -> <pre><code>

        Everything else is HTML-escaped.
        """
        md = md or ""

        fence_re = re.compile(r"```[^\n]*\n([\s\S]*?)```", re.MULTILINE)
        inline_code_re = re.compile(r"`([^`\n]+)`")
        bold_re = re.compile(r"\*\*([^*\n]+)\*\*")

        parts: list[str] = []
        last = 0
        for m in fence_re.finditer(md):
            # text before code block
            parts.append(md[last : m.start()])
            code = m.group(1)
            code_esc = html.escape(code)
            parts.append(f"<pre><code>{code_esc}</code></pre>")
            last = m.end()
        parts.append(md[last:])

        def _render_span(text: str) -> str:
            # Inline code first
            out: list[str] = []
            pos = 0
            for mm in inline_code_re.finditer(text):
                out.append(html.escape(text[pos : mm.start()]))
                out.append(f"<code>{html.escape(mm.group(1))}</code>")
                pos = mm.end()
            out.append(html.escape(text[pos:]))
            s = "".join(out)
            # Bold
            s = bold_re.sub(r"<b>\1</b>", s)
            return s

        return "".join(_render_span(p) if not p.startswith("<pre><code>") else p for p in parts)

    def _telegram_send_message_html(self, chat_id: int, html_text: str) -> tuple[bool, str]:
        """Send formatted message via Telegram sendMessage(parse_mode=HTML)."""
        return self._telegram_api_post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "true",
            },
        )

    def _telegram_api_post(self, method: str, data: Dict[str, Any]) -> Tuple[bool, str]:
        """Best-effort Telegram Bot API call.

        We intentionally do not log request URLs or payloads verbatim to avoid any chance of leaking secrets.

        Returns: (ok, status)
          - ok: True if request succeeded
          - status: "ok" | "no_token" | "error"
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False, "no_token"

        url = f"https://api.telegram.org/bot{token}/{method}"
        payload = urllib.parse.urlencode({k: str(v) for k, v in data.items()}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
            return True, "ok"
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "telegram_api_error", "method": method, "error": repr(e)},
            )
            return False, "error"

    def _send_chat_action(self, chat_id: int, action: str = "typing", log: bool = False) -> None:
        ok, status = self._telegram_api_post("sendChatAction", {"chat_id": chat_id, "action": action})
        if log:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "telegram_chat_action",
                    "chat_id": chat_id,
                    "action": action,
                    "ok": ok,
                    "status": status,
                },
            )

    def _start_typing_loop(self, chat_id: int) -> threading.Event:
        """Start a background loop that periodically sends 'typing…' while the task is being processed.

        Why there is a start delay:
        - Supervisor often sends an immediate "accepted/started" message.
        - Telegram clients may not show typing if a bot just sent a message; delaying the first logged "typing"
          increases the chance it becomes visible.

        Settings:
        - OUROBOROS_TG_TYPING=0/1
        - OUROBOROS_TG_TYPING_INTERVAL (seconds)
        - OUROBOROS_TG_TYPING_START_DELAY (seconds)
        """
        stop = threading.Event()
        interval = float(os.environ.get("OUROBOROS_TG_TYPING_INTERVAL", "4"))
        start_delay = float(os.environ.get("OUROBOROS_TG_TYPING_START_DELAY", "1.0"))

        # Best effort: send immediately once (not logged).
        self._send_chat_action(chat_id, "typing", log=False)

        def _loop() -> None:
            # Wait a bit, then send the first logged typing action.
            if start_delay > 0:
                stop.wait(start_delay)
                if stop.is_set():
                    return

            self._send_chat_action(chat_id, "typing", log=True)

            # Telegram clients typically show typing for a few seconds; refresh periodically.
            while not stop.wait(interval):
                self._send_chat_action(chat_id, "typing", log=False)

        threading.Thread(target=_loop, daemon=True).start()
        return stop

    # ---------- tools + LLM loop ----------

    def _openrouter_client(self):
        from openai import OpenAI

        headers = {"HTTP-Referer": "https://colab.research.google.com/", "X-Title": "Ouroboros"}
        return OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            default_headers=headers,
        )

    def _llm_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        model = os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2")
        client = self._openrouter_client()
        drive_logs = self.env.drive_path("logs")

        tool_name_to_fn = {
            "repo_read": self._tool_repo_read,
            "repo_list": self._tool_repo_list,
            "drive_read": self._tool_drive_read,
            "drive_list": self._tool_drive_list,
            "drive_write": self._tool_drive_write,
            "repo_write_commit": self._tool_repo_write_commit,
            "git_status": self._tool_git_status,
            "git_diff": self._tool_git_diff,
            "run_shell": self._tool_run_shell,
            "web_search": self._tool_web_search,
            "request_restart": self._tool_request_restart,
            "request_stable_promotion": self._tool_request_stable_promotion,
            "schedule_task": self._tool_schedule_task,
            "cancel_task": self._tool_cancel_task,
            "reindex_request": self._tool_reindex_request,
        }

        max_tool_rounds = int(os.environ.get("OUROBOROS_MAX_TOOL_ROUNDS", "20"))
        llm_max_retries = int(os.environ.get("OUROBOROS_LLM_MAX_RETRIES", "3"))
        last_usage: Dict[str, Any] = {}

        for round_idx in range(max_tool_rounds):
            # ---- LLM call with retry on transient errors ----
            resp_dict = None
            last_llm_error: Optional[Exception] = None

            for attempt in range(llm_max_retries):
                try:
                    resp = client.chat.completions.create(
                        model=model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                    )
                    resp_dict = resp.model_dump()
                    break
                except Exception as e:
                    last_llm_error = e
                    append_jsonl(
                        drive_logs / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "llm_api_error",
                            "round": round_idx,
                            "attempt": attempt + 1,
                            "max_retries": llm_max_retries,
                            "error": repr(e),
                        },
                    )
                    if attempt < llm_max_retries - 1:
                        wait_sec = min(2**attempt * 2, 30)
                        self._emit_progress(
                            f"Ошибка LLM API (попытка {attempt + 1}/{llm_max_retries}): "
                            f"{type(e).__name__}. Повторяю через {wait_sec}с..."
                        )
                        time.sleep(wait_sec)

            if resp_dict is None:
                return (
                    f"⚠️ Не удалось получить ответ от модели после {llm_max_retries} попыток.\n"
                    f"Ошибка: {type(last_llm_error).__name__}: {last_llm_error}\n"
                    f"Попробуй повторить запрос через минуту."
                ), last_usage

            last_usage = resp_dict.get("usage", {}) or {}

            choice = (resp_dict.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if tool_calls:
                messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

                # Emit the LLM's reasoning/plan as a progress message (human-readable narration)
                if content and content.strip():
                    self._emit_progress(content.strip())

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]

                    # ---- Parse arguments safely ----
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError) as e:
                        result = (
                            f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}\n"
                            f"Raw: {truncate_for_log(tc['function'].get('arguments', ''), 500)}\n"
                            f"Retry with valid JSON arguments."
                        )
                        append_jsonl(
                            drive_logs / "tools.jsonl",
                            {"ts": utc_now_iso(), "tool": fn_name, "error": "json_parse", "detail": repr(e)},
                        )
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                        continue

                    # ---- Check tool exists ----
                    if fn_name not in tool_name_to_fn:
                        result = (
                            f"⚠️ UNKNOWN_TOOL: '{fn_name}' does not exist.\n"
                            f"Available: {', '.join(sorted(tool_name_to_fn.keys()))}"
                        )
                        append_jsonl(
                            drive_logs / "tools.jsonl",
                            {"ts": utc_now_iso(), "tool": fn_name, "error": "unknown_tool"},
                        )
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                        continue

                    # ---- Execute tool safely ----
                    try:
                        result = tool_name_to_fn[fn_name](**args)
                    except Exception as e:
                        tb = traceback.format_exc()
                        result = (
                            f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}\n\n"
                            f"Traceback (last 2000 chars):\n{truncate_for_log(tb, 2000)}\n\n"
                            f"The tool raised an exception. Analyze the error and try a different approach."
                        )
                        append_jsonl(
                            drive_logs / "events.jsonl",
                            {
                                "ts": utc_now_iso(),
                                "type": "tool_error",
                                "tool": fn_name,
                                "args": args,
                                "error": repr(e),
                                "traceback": truncate_for_log(tb, 2000),
                            },
                        )
                        self._emit_progress(
                            f"Tool '{fn_name}' вернул ошибку: {type(e).__name__}: {e}\n"
                            f"Анализирую и пробую другой подход..."
                        )

                    append_jsonl(
                        drive_logs / "tools.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "tool": fn_name,
                            "args": args,
                            "result_preview": truncate_for_log(result, 2000),
                        },
                    )
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            return (content or ""), last_usage

        return "⚠️ Превышен лимит tool rounds. Остановился.", last_usage

    def _tools_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "repo_read",
                    "description": "Read a UTF-8 text file from the GitHub repo (relative path).",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "repo_list",
                    "description": "List files under a repo directory (relative path).",
                    "parameters": {
                        "type": "object",
                        "properties": {"dir": {"type": "string"}, "max_entries": {"type": "integer"}},
                        "required": ["dir"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "drive_read",
                    "description": "Read a UTF-8 text file from Google Drive root (relative path).",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "drive_list",
                    "description": "List files under a Drive directory (relative path).",
                    "parameters": {
                        "type": "object",
                        "properties": {"dir": {"type": "string"}, "max_entries": {"type": "integer"}},
                        "required": ["dir"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "drive_write",
                    "description": "Write a UTF-8 text file in Google Drive root (relative path).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "mode": {"type": "string", "enum": ["overwrite", "append"]},
                        },
                        "required": ["path", "content", "mode"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "repo_write_commit",
                    "description": "Write a UTF-8 text file in repo, then git add/commit/push to ouroboros branch. Canonical self-modification.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "commit_message": {"type": "string"}},
                        "required": ["path", "content", "commit_message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {"name": "git_status", "description": "Run git status --porcelain in repo.", "parameters": {"type": "object", "properties": {}, "required": []}},
            },
            {
                "type": "function",
                "function": {"name": "git_diff", "description": "Run git diff in repo.", "parameters": {"type": "object", "properties": {}, "required": []}},
            },
            {
                "type": "function",
                "function": {
                    "name": "run_shell",
                    "description": "Run a shell command (list of args) inside the repo (dangerous; use carefully). Returns stdout+stderr.",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "OpenAI web search via Responses API tool web_search (fresh web). Returns JSON with answer + sources.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "allowed_domains": {"type": "array", "items": {"type": "string"}}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {"name": "request_restart", "description": "Ask supervisor to restart Ouroboros runtime (apply new code).", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
            {
                "type": "function",
                "function": {"name": "request_stable_promotion", "description": "Ask owner approval to promote current ouroboros HEAD to ouroboros-stable.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
            {
                "type": "function",
                "function": {"name": "schedule_task", "description": "Schedule a background task (queued by supervisor).", "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
            },
            {
                "type": "function",
                "function": {"name": "cancel_task", "description": "Request supervisor to cancel a task by id.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
            },
            {
                "type": "function",
                "function": {"name": "reindex_request", "description": "Request owner approval to run full reindexing of summaries.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
        ]

    # ---------- tool implementations ----------

    def _tool_repo_read(self, path: str) -> str:
        return read_text(self.env.repo_path(path))

    def _tool_repo_list(self, dir: str, max_entries: int = 500) -> str:
        return json.dumps(list_dir(self.env.repo_dir, dir, max_entries=max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_read(self, path: str) -> str:
        return read_text(self.env.drive_path(path))

    def _tool_drive_list(self, dir: str, max_entries: int = 500) -> str:
        return json.dumps(list_dir(self.env.drive_root, dir, max_entries=max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_write(self, path: str, content: str, mode: str) -> str:
        p = self.env.drive_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite":
            p.write_text(content, encoding="utf-8")
        else:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        return f"OK: wrote {mode} {path} ({len(content)} chars)"

    def _acquire_git_lock(self) -> pathlib.Path:
        lock_dir = self.env.drive_path("locks")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "git.lock"
        while lock_path.exists():
            time.sleep(0.5)
        lock_path.write_text(f"locked_at={utc_now_iso()}\n", encoding="utf-8")
        return lock_path

    def _release_git_lock(self, lock_path: pathlib.Path) -> None:
        if lock_path.exists():
            lock_path.unlink()

    def _tool_repo_write_commit(self, path: str, content: str, commit_message: str) -> str:
        if not commit_message.strip():
            return "⚠️ ERROR: commit_message must be non-empty."

        lock = self._acquire_git_lock()
        try:
            # Step 1: checkout
            try:
                run(["git", "checkout", self.env.branch_dev], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout {self.env.branch_dev}): {e}"

            # Step 2: write file
            try:
                write_text(self.env.repo_path(path), content)
            except Exception as e:
                return f"⚠️ FILE_WRITE_ERROR ({path}): {e}"

            # Step 3: git add
            try:
                run(["git", "add", safe_relpath(path)], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (add {path}): {e}"

            # Step 4: git commit
            try:
                run(["git", "commit", "-m", commit_message], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (commit): {e}\nFile was written and staged but not committed."

            # Step 5: git push
            try:
                run(["git", "push", "origin", self.env.branch_dev], cwd=self.env.repo_dir)
            except Exception as e:
                return (
                    f"⚠️ GIT_ERROR (push): {e}\n"
                    f"Committed locally but NOT pushed. "
                    f"Retry with: run_shell(['git', 'push', 'origin', '{self.env.branch_dev}'])"
                )
        finally:
            self._release_git_lock(lock)

        return f"OK: committed and pushed to {self.env.branch_dev}: {commit_message}"

    def _tool_git_status(self) -> str:
        try:
            return run(["git", "status", "--porcelain"], cwd=self.env.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (status): {e}"

    def _tool_git_diff(self) -> str:
        try:
            return run(["git", "diff"], cwd=self.env.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR (diff): {e}"

    def _tool_run_shell(self, cmd: List[str], cwd: str = "") -> str:
        wd = self.env.repo_dir if not cwd else (self.env.repo_dir / safe_relpath(cwd)).resolve()
        try:
            res = subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return f"⚠️ Command timed out after 120s: {' '.join(cmd)}"
        except Exception as e:
            return f"⚠️ Failed to execute command: {type(e).__name__}: {e}"
        output = (res.stdout + "\n" + res.stderr).strip()
        if res.returncode != 0:
            return (
                f"⚠️ Command exited with code {res.returncode}: {' '.join(cmd)}\n\n"
                f"STDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
            )
        return output

    def _tool_request_restart(self, reason: str) -> str:
        self._pending_events.append({"type": "restart_request", "reason": reason, "ts": utc_now_iso()})
        return f"Restart requested: {reason}"

    def _tool_request_stable_promotion(self, reason: str) -> str:
        self._pending_events.append({"type": "stable_promotion_request", "reason": reason, "ts": utc_now_iso()})
        return f"Stable promotion requested (needs owner approval): {reason}"

    def _tool_schedule_task(self, description: str) -> str:
        self._pending_events.append({"type": "schedule_task", "description": description, "ts": utc_now_iso()})
        return f"Scheduled task request: {description}"

    def _tool_cancel_task(self, task_id: str) -> str:
        self._pending_events.append({"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()})
        return f"Cancel requested for task_id={task_id}"

    def _tool_reindex_request(self, reason: str) -> str:
        self._pending_events.append({"type": "reindex_request", "reason": reason, "ts": utc_now_iso()})
        return f"Reindex requested (needs owner approval): {reason}"

    def _tool_web_search(self, query: str, allowed_domains: Optional[List[str]] = None) -> str:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return json.dumps({"error": "OPENAI_API_KEY is not set; web_search unavailable."}, ensure_ascii=False)

        from openai import OpenAI

        client = OpenAI(api_key=api_key)

        tool: Dict[str, Any] = {"type": "web_search"}
        if allowed_domains:
            tool["filters"] = {"allowed_domains": allowed_domains}

        resp = client.responses.create(
            model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
            tools=[tool],
            tool_choice="auto",
            include=["web_search_call.action.sources"],
            input=query,
        )
        d = resp.model_dump()

        sources: List[Dict[str, Any]] = []
        for item in d.get("output", []) or []:
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                sources = action.get("sources") or []

        out = {"answer": d.get("output_text", ""), "sources": sources}
        return json.dumps(out, ensure_ascii=False, indent=2)

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)


def smoke_test() -> str:
    required = ["prompts/BASE.md", "README.md", "WORLD.md"]
    return "OK: " + ", ".join(required)
