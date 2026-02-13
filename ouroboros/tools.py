"""
Уроборос — Реестр инструментов (SSOT).

Единственный источник tool schemas и реализаций.
Контракт: schemas(), execute(name, args), available_tools().
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ouroboros.utils import (
    utc_now_iso, read_text, write_text, append_jsonl,
    safe_relpath, truncate_for_log, run_cmd,
)


class ToolContext:
    """Контекст выполнения инструмента — передаётся из агента перед каждой задачей."""

    def __init__(
        self,
        pending_events: List[Dict[str, Any]],
        current_chat_id: Optional[int] = None,
        current_task_type: Optional[str] = None,
        emit_progress_fn: Optional[Callable[[str], None]] = None,
    ):
        self.pending_events = pending_events
        self.current_chat_id = current_chat_id
        self.current_task_type = current_task_type
        self.last_push_succeeded = False
        self.emit_progress_fn = emit_progress_fn or (lambda _: None)


class ToolRegistry:
    """Реестр инструментов Уробороса (SSOT).

    Добавить инструмент: добавить schema в schemas() и метод _tool_<name>.
    Удалить инструмент: убрать schema и метод.
    """

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self.repo_dir = repo_dir
        self.drive_root = drive_root
        self.branch_dev = "ouroboros"
        self._ctx: Optional[ToolContext] = None

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()

    def drive_logs(self) -> pathlib.Path:
        return (self.drive_root / "logs").resolve()

    # --- Контракт ---

    def available_tools(self) -> List[str]:
        return [s["function"]["name"] for s in self.schemas()]

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Выполнить инструмент по имени. Возвращает результат как строку."""
        fn = self._fn_map().get(name)
        if fn is None:
            return f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._fn_map().keys()))}"
        try:
            return fn(**args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"

    def _fn_map(self) -> Dict[str, Any]:
        return {
            "repo_read": self._tool_repo_read,
            "repo_list": self._tool_repo_list,
            "drive_read": self._tool_drive_read,
            "drive_list": self._tool_drive_list,
            "drive_write": self._tool_drive_write,
            "repo_write_commit": self._tool_repo_write_commit,
            "repo_commit_push": self._tool_repo_commit_push,
            "git_status": self._tool_git_status,
            "git_diff": self._tool_git_diff,
            "run_shell": self._tool_run_shell,
            "claude_code_edit": self._tool_claude_code_edit,
            "web_search": self._tool_web_search,
            "request_restart": self._tool_request_restart,
            "promote_to_stable": self._tool_promote_to_stable,
            "schedule_task": self._tool_schedule_task,
            "cancel_task": self._tool_cancel_task,
            "chat_history": self._tool_chat_history,
            "request_review": self._tool_request_review,
        }

    CODE_TOOLS = frozenset({
        "repo_write_commit", "repo_commit_push", "git_status",
        "git_diff", "run_shell", "claude_code_edit",
    })

    def schemas(self) -> List[Dict[str, Any]]:
        """OpenAI-совместимые tool schemas."""
        return [
            {"type": "function", "function": {
                "name": "repo_read",
                "description": "Read a UTF-8 text file from the GitHub repo (relative path).",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }},
            {"type": "function", "function": {
                "name": "repo_list",
                "description": "List files under a repo directory (relative path).",
                "parameters": {"type": "object", "properties": {
                    "dir": {"type": "string", "default": "."},
                    "max_entries": {"type": "integer", "default": 500},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "drive_read",
                "description": "Read a UTF-8 text file from Google Drive (relative to MyDrive/Ouroboros/).",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }},
            {"type": "function", "function": {
                "name": "drive_list",
                "description": "List files under a Drive directory.",
                "parameters": {"type": "object", "properties": {
                    "dir": {"type": "string", "default": "."},
                    "max_entries": {"type": "integer", "default": 500},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "drive_write",
                "description": "Write a UTF-8 text file on Google Drive.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                }, "required": ["path", "content"]},
            }},
            {"type": "function", "function": {
                "name": "repo_write_commit",
                "description": "Write one file + commit + push to ouroboros branch. For small deterministic edits.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "commit_message": {"type": "string"},
                }, "required": ["path", "content", "commit_message"]},
            }},
            {"type": "function", "function": {
                "name": "repo_commit_push",
                "description": "Commit + push already-changed files. Does pull --rebase before push.",
                "parameters": {"type": "object", "properties": {
                    "commit_message": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Files to add (empty = git add -A)"},
                }, "required": ["commit_message"]},
            }},
            {"type": "function", "function": {
                "name": "git_status",
                "description": "git status --porcelain",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            {"type": "function", "function": {
                "name": "git_diff",
                "description": "git diff",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }},
            {"type": "function", "function": {
                "name": "run_shell",
                "description": "Run a shell command (list of args) inside the repo. Returns stdout+stderr.",
                "parameters": {"type": "object", "properties": {
                    "cmd": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "default": ""},
                }, "required": ["cmd"]},
            }},
            {"type": "function", "function": {
                "name": "claude_code_edit",
                "description": "Delegate code edits to Claude Code CLI. Preferred for multi-file changes and refactors. Follow with repo_commit_push.",
                "parameters": {"type": "object", "properties": {
                    "prompt": {"type": "string"},
                    "cwd": {"type": "string", "default": ""},
                }, "required": ["prompt"]},
            }},
            {"type": "function", "function": {
                "name": "web_search",
                "description": "Search the web via OpenAI Responses API. Returns JSON with answer + sources.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"},
                }, "required": ["query"]},
            }},
            {"type": "function", "function": {
                "name": "request_restart",
                "description": "Ask supervisor to restart runtime (after successful push).",
                "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
            }},
            {"type": "function", "function": {
                "name": "promote_to_stable",
                "description": "Promote ouroboros -> ouroboros-stable. Call when you consider the code stable.",
                "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]},
            }},
            {"type": "function", "function": {
                "name": "schedule_task",
                "description": "Schedule a background task.",
                "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]},
            }},
            {"type": "function", "function": {
                "name": "cancel_task",
                "description": "Cancel a task by ID.",
                "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
            }},
            {"type": "function", "function": {
                "name": "chat_history",
                "description": "Retrieve messages from chat history. Supports search.",
                "parameters": {"type": "object", "properties": {
                    "count": {"type": "integer", "default": 100, "description": "Number of messages (from latest)"},
                    "offset": {"type": "integer", "default": 0, "description": "Skip N from end (pagination)"},
                    "search": {"type": "string", "default": "", "description": "Text filter"},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "request_review",
                "description": "Request a deep review of code, prompts, and state. You decide when a review is needed.",
                "parameters": {"type": "object", "properties": {
                    "reason": {"type": "string", "description": "Why you want a review (context for the reviewer)"},
                }, "required": ["reason"]},
            }},
        ]

    # --- Git lock ---

    def _acquire_git_lock(self) -> pathlib.Path:
        lock_dir = self.drive_path("locks")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "git.lock"
        stale_sec = 600
        while True:
            if lock_path.exists():
                try:
                    age = time.time() - lock_path.stat().st_mtime
                    if age > stale_sec:
                        lock_path.unlink()
                        continue
                except (FileNotFoundError, OSError):
                    pass
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, f"locked_at={utc_now_iso()}\n".encode("utf-8"))
                finally:
                    os.close(fd)
                return lock_path
            except FileExistsError:
                time.sleep(0.5)

    def _release_git_lock(self, lock_path: pathlib.Path) -> None:
        if lock_path.exists():
            lock_path.unlink()

    # --- Directory listing ---

    def _list_dir(self, root: pathlib.Path, rel: str, max_entries: int = 500) -> List[str]:
        target = (root / safe_relpath(rel)).resolve()
        if not target.exists():
            return [f"⚠️ Directory not found: {rel}"]
        if not target.is_dir():
            return [f"⚠️ Not a directory: {rel}"]
        items = []
        try:
            for entry in sorted(target.iterdir()):
                if len(items) >= max_entries:
                    items.append(f"...(truncated at {max_entries})")
                    break
                suffix = "/" if entry.is_dir() else ""
                items.append(str(entry.relative_to(root)) + suffix)
        except Exception as e:
            items.append(f"⚠️ Error listing: {e}")
        return items

    # --- Tool implementations ---

    def _tool_repo_read(self, path: str) -> str:
        return read_text(self.repo_path(path))

    def _tool_repo_list(self, dir: str = ".", max_entries: int = 500) -> str:
        return json.dumps(self._list_dir(self.repo_dir, dir, max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_read(self, path: str) -> str:
        return read_text(self.drive_path(path))

    def _tool_drive_list(self, dir: str = ".", max_entries: int = 500) -> str:
        return json.dumps(self._list_dir(self.drive_root, dir, max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_write(self, path: str, content: str, mode: str = "overwrite") -> str:
        p = self.drive_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite":
            p.write_text(content, encoding="utf-8")
        else:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        return f"OK: wrote {mode} {path} ({len(content)} chars)"

    def _tool_repo_write_commit(self, path: str, content: str, commit_message: str) -> str:
        if self._ctx:
            self._ctx.last_push_succeeded = False
        if not commit_message.strip():
            return "⚠️ ERROR: commit_message must be non-empty."
        lock = self._acquire_git_lock()
        try:
            try:
                run_cmd(["git", "checkout", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout): {e}"
            try:
                write_text(self.repo_path(path), content)
            except Exception as e:
                return f"⚠️ FILE_WRITE_ERROR: {e}"
            try:
                run_cmd(["git", "add", safe_relpath(path)], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (add): {e}"
            try:
                run_cmd(["git", "commit", "-m", commit_message], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (commit): {e}"
            try:
                run_cmd(["git", "pull", "--rebase", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception:
                pass
            try:
                run_cmd(["git", "push", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (push): {e}\nCommitted locally but NOT pushed."
        finally:
            self._release_git_lock(lock)
        if self._ctx:
            self._ctx.last_push_succeeded = True
        return f"OK: committed and pushed to {self.branch_dev}: {commit_message}"

    def _tool_repo_commit_push(self, commit_message: str, paths: Optional[List[str]] = None) -> str:
        if self._ctx:
            self._ctx.last_push_succeeded = False
        if not commit_message.strip():
            return "⚠️ ERROR: commit_message must be non-empty."
        lock = self._acquire_git_lock()
        try:
            try:
                run_cmd(["git", "checkout", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout): {e}"
            if paths:
                try:
                    safe_paths = [safe_relpath(p) for p in paths if str(p).strip()]
                except ValueError as e:
                    return f"⚠️ PATH_ERROR: {e}"
                add_cmd = ["git", "add"] + safe_paths
            else:
                add_cmd = ["git", "add", "-A"]
            try:
                run_cmd(add_cmd, cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (add): {e}"
            try:
                status = run_cmd(["git", "status", "--porcelain"], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (status): {e}"
            if not status.strip():
                return "⚠️ GIT_NO_CHANGES: nothing to commit."
            try:
                run_cmd(["git", "commit", "-m", commit_message], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (commit): {e}"
            try:
                run_cmd(["git", "pull", "--rebase", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception:
                pass
            try:
                run_cmd(["git", "push", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (push): {e}\nCommitted locally but NOT pushed."
        finally:
            self._release_git_lock(lock)
        if self._ctx:
            self._ctx.last_push_succeeded = True
        return f"OK: committed and pushed to {self.branch_dev}: {commit_message}"

    def _tool_git_status(self) -> str:
        try:
            return run_cmd(["git", "status", "--porcelain"], cwd=self.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR: {e}"

    def _tool_git_diff(self) -> str:
        try:
            return run_cmd(["git", "diff"], cwd=self.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR: {e}"

    def _tool_run_shell(self, cmd: List[str], cwd: str = "") -> str:
        # Block git in evolution mode
        if self._ctx and str(self._ctx.current_task_type or "") == "evolution":
            if isinstance(cmd, list) and cmd and str(cmd[0]).lower() == "git":
                return "⚠️ EVOLUTION_GIT_RESTRICTED: use repo_write_commit/repo_commit_push."

        work_dir = self.repo_dir
        if cwd and cwd.strip() not in ("", ".", "./"):
            candidate = (self.repo_dir / cwd).resolve()
            if candidate.exists() and candidate.is_dir():
                work_dir = candidate

        try:
            res = subprocess.run(
                cmd, cwd=str(work_dir),
                capture_output=True, text=True, timeout=120,
            )
            out = res.stdout + ("\n--- STDERR ---\n" + res.stderr if res.stderr else "")
            if len(out) > 50000:
                out = out[:25000] + "\n...(truncated)...\n" + out[-25000:]
            prefix = f"exit_code={res.returncode}\n"
            return prefix + out
        except subprocess.TimeoutExpired:
            return "⚠️ TIMEOUT: command exceeded 120s."
        except Exception as e:
            return f"⚠️ SHELL_ERROR: {e}"

    def _tool_claude_code_edit(self, prompt: str, cwd: str = "") -> str:
        """Delegate code edits to Claude Code CLI."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "⚠️ ANTHROPIC_API_KEY not set, claude_code_edit unavailable."

        work_dir = str(self.repo_dir)
        if cwd and cwd.strip() not in ("", ".", "./"):
            candidate = (self.repo_dir / cwd).resolve()
            if candidate.exists():
                work_dir = str(candidate)

        claude_bin = shutil.which("claude")
        if not claude_bin:
            return "⚠️ Claude CLI not found. Ensure ANTHROPIC_API_KEY is set."

        if self._ctx:
            self._ctx.emit_progress_fn(f"Delegating to Claude Code CLI...")

        lock = self._acquire_git_lock()
        try:
            try:
                run_cmd(["git", "checkout", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout): {e}"

            full_prompt = (
                f"STRICT: Only modify files inside {work_dir}. "
                f"Git branch: {self.branch_dev}. Do NOT commit or push.\n\n"
                f"{prompt}"
            )

            env = os.environ.copy()
            env["ANTHROPIC_API_KEY"] = api_key
            try:
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    env.setdefault("IS_SANDBOX", "1")
            except Exception:
                pass
            local_bin = str(pathlib.Path.home() / ".local" / "bin")
            if local_bin not in env.get("PATH", ""):
                env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

            cmd = [
                claude_bin, "-p", full_prompt,
                "--output-format", "json",
                "--max-turns", "12",
                "--tools", "Read,Edit,Grep,Glob",
            ]

            # Try --permission-mode first, fallback to --dangerously-skip-permissions
            perm_mode = os.environ.get("OUROBOROS_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions").strip()
            primary_cmd = cmd + ["--permission-mode", perm_mode]
            legacy_cmd = cmd + ["--dangerously-skip-permissions"]

            res = subprocess.run(
                primary_cmd, cwd=work_dir,
                capture_output=True, text=True, timeout=600, env=env,
            )

            if res.returncode != 0:
                combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
                if "--permission-mode" in combined and any(
                    m in combined for m in ("unknown option", "unknown argument", "unrecognized option", "unexpected argument")
                ):
                    res = subprocess.run(
                        legacy_cmd, cwd=work_dir,
                        capture_output=True, text=True, timeout=600, env=env,
                    )

            stdout = (res.stdout or "").strip()
            stderr = (res.stderr or "").strip()
            if res.returncode != 0:
                return f"⚠️ CLAUDE_CODE_ERROR: exit={res.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            if not stdout:
                return "OK: Claude Code completed with empty output."

        except subprocess.TimeoutExpired:
            return "⚠️ CLAUDE_CODE_TIMEOUT: exceeded 600s."
        except Exception as e:
            return f"⚠️ CLAUDE_CODE_FAILED: {type(e).__name__}: {e}"
        finally:
            self._release_git_lock(lock)

        # Parse JSON output and account cost
        try:
            payload = json.loads(stdout)
            out: Dict[str, Any] = {
                "result": payload.get("result", ""),
                "session_id": payload.get("session_id"),
            }
            # Account Claude Code CLI cost
            if self._ctx and isinstance(payload.get("total_cost_usd"), (int, float)):
                self._ctx.pending_events.append({
                    "type": "llm_usage",
                    "provider": "claude_code_cli",
                    "usage": {"cost": float(payload["total_cost_usd"])},
                    "source": "claude_code_edit",
                    "ts": utc_now_iso(),
                })
            return json.dumps(out, ensure_ascii=False, indent=2)
        except Exception:
            return stdout

    def _tool_web_search(self, query: str) -> str:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return json.dumps({"error": "OPENAI_API_KEY not set; web_search unavailable."})
        try:
            from openai import OpenAI
            client = OpenAI(api_key=api_key)
            resp = client.responses.create(
                model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
                tools=[{"type": "web_search"}],
                tool_choice="auto",
                input=query,
            )
            d = resp.model_dump()
            text = ""
            for item in d.get("output", []) or []:
                if item.get("type") == "message":
                    for block in item.get("content", []) or []:
                        if block.get("type") in ("output_text", "text"):
                            text += block.get("text", "")
            return json.dumps({"answer": text or "(no answer)"}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": repr(e)}, ensure_ascii=False)

    def _tool_request_restart(self, reason: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        if str(self._ctx.current_task_type or "") == "evolution" and not self._ctx.last_push_succeeded:
            return "⚠️ RESTART_BLOCKED: in evolution mode, commit+push first."
        # Persist expected SHA for post-restart verification
        try:
            sha = run_cmd(["git", "rev-parse", "HEAD"], cwd=self.repo_dir)
            branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo_dir)
            verify_path = self.drive_path("state") / "pending_restart_verify.json"
            write_text(verify_path, json.dumps({
                "ts": utc_now_iso(), "expected_sha": sha,
                "expected_branch": branch, "reason": reason,
            }, ensure_ascii=False, indent=2))
        except Exception:
            pass
        self._ctx.pending_events.append({"type": "restart_request", "reason": reason, "ts": utc_now_iso()})
        self._ctx.last_push_succeeded = False
        return f"Restart requested: {reason}"

    def _tool_promote_to_stable(self, reason: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "promote_to_stable", "reason": reason, "ts": utc_now_iso()})
        return f"Promote to stable requested: {reason}"

    def _tool_schedule_task(self, description: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "schedule_task", "description": description, "ts": utc_now_iso()})
        return f"Scheduled: {description}"

    def _tool_cancel_task(self, task_id: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()})
        return f"Cancel requested: {task_id}"

    def _tool_request_review(self, reason: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "review_request", "reason": reason, "ts": utc_now_iso()})
        return f"Review requested: {reason}"

    def _tool_chat_history(self, count: int = 100, offset: int = 0, search: str = "") -> str:
        from ouroboros.memory import Memory
        mem = Memory(drive_root=self.drive_root)
        return mem.chat_history(count=count, offset=offset, search=search)
