"""
Уроборос — Реестр инструментов.

Все tool schemas и реализации. Добавить/убрать инструмент → правишь один файл.
Контракт: schemas(), execute(name, args), available_tools().
"""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple


# --- Утилиты ---

def _utc_now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def _safe_relpath(p: str) -> str:
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p


def _read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(cmd: List[str], cwd: Optional[pathlib.Path] = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return res.stdout.strip()


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> List[str]:
    """List directory contents relative to root."""
    target = (root / _safe_relpath(rel)).resolve()
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


def _append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, data)
        finally:
            os.close(fd)
    except Exception:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _truncate_for_log(s: str, max_chars: int = 4000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2:]


class ToolContext:
    """Контекст выполнения инструмента — передаётся из агента."""

    def __init__(
        self,
        pending_events: List[Dict[str, Any]],
        current_chat_id: Optional[int] = None,
        current_task_type: Optional[str] = None,
    ):
        self.pending_events = pending_events
        self.current_chat_id = current_chat_id
        self.current_task_type = current_task_type
        self.last_push_succeeded = False


class ToolRegistry:
    """Реестр инструментов Уробороса.

    Добавить инструмент: добавить schema в _SCHEMAS и метод _tool_<name>.
    Удалить инструмент: убрать schema и метод.
    """

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self.repo_dir = repo_dir
        self.drive_root = drive_root
        self.branch_dev = "ouroboros"
        self.branch_stable = "ouroboros-stable"
        self._ctx: Optional[ToolContext] = None

    def set_context(self, ctx: ToolContext) -> None:
        """Устанавливает контекст для текущей задачи."""
        self._ctx = ctx

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / _safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / _safe_relpath(rel)).resolve()

    # --- Контракт ---

    def available_tools(self) -> List[str]:
        return [s["function"]["name"] for s in self.schemas()]

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        """Выполнить инструмент по имени. Возвращает результат как строку."""
        fn_map = {
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
        }
        fn = fn_map.get(name)
        if fn is None:
            return f"⚠️ Unknown tool: {name}"
        try:
            return fn(**args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"

    def schemas(self) -> List[Dict[str, Any]]:
        """OpenAI-совместимые tool schemas."""
        return [
            {"type": "function", "function": {
                "name": "repo_read",
                "description": "Прочитать текстовый файл из репозитория (относительный путь).",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }},
            {"type": "function", "function": {
                "name": "repo_list",
                "description": "Листинг директории репозитория.",
                "parameters": {"type": "object", "properties": {
                    "dir": {"type": "string", "default": "."},
                    "max_entries": {"type": "integer", "default": 500},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "drive_read",
                "description": "Прочитать файл с Google Drive (относительно MyDrive/Ouroboros/).",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            }},
            {"type": "function", "function": {
                "name": "drive_list",
                "description": "Листинг директории на Google Drive.",
                "parameters": {"type": "object", "properties": {
                    "dir": {"type": "string", "default": "."},
                    "max_entries": {"type": "integer", "default": 500},
                }, "required": []},
            }},
            {"type": "function", "function": {
                "name": "drive_write",
                "description": "Записать файл на Google Drive.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
                }, "required": ["path", "content"]},
            }},
            {"type": "function", "function": {
                "name": "repo_write_commit",
                "description": "Записать файл + commit + push в ветку ouroboros. Для маленьких точечных правок.",
                "parameters": {"type": "object", "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "commit_message": {"type": "string"},
                }, "required": ["path", "content", "commit_message"]},
            }},
            {"type": "function", "function": {
                "name": "repo_commit_push",
                "description": "Commit + push уже изменённых файлов. Делает pull --rebase перед push.",
                "parameters": {"type": "object", "properties": {
                    "commit_message": {"type": "string"},
                    "paths": {"type": "array", "items": {"type": "string"}, "description": "Файлы для add (пустой = git add -A)"},
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
                "description": "Выполнить shell-команду.",
                "parameters": {"type": "object", "properties": {
                    "cmd": {"type": "array", "items": {"type": "string"}},
                    "cwd": {"type": "string", "default": ""},
                }, "required": ["cmd"]},
            }},
            {"type": "function", "function": {
                "name": "claude_code_edit",
                "description": "Делегировать правки Claude Code CLI (основной путь для кода).",
                "parameters": {"type": "object", "properties": {
                    "prompt": {"type": "string"},
                    "cwd": {"type": "string", "default": ""},
                }, "required": ["prompt"]},
            }},
            {"type": "function", "function": {
                "name": "web_search",
                "description": "Поиск в интернете через OpenAI Responses API.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string"},
                }, "required": ["query"]},
            }},
            {"type": "function", "function": {
                "name": "request_restart",
                "description": "Запросить перезапуск runtime (после успешного push).",
                "parameters": {"type": "object", "properties": {
                    "reason": {"type": "string"},
                }, "required": ["reason"]},
            }},
            {"type": "function", "function": {
                "name": "promote_to_stable",
                "description": "Промоутить ouroboros → ouroboros-stable. Вызывай когда считаешь код стабильным.",
                "parameters": {"type": "object", "properties": {
                    "reason": {"type": "string"},
                }, "required": ["reason"]},
            }},
            {"type": "function", "function": {
                "name": "schedule_task",
                "description": "Запланировать фоновую задачу.",
                "parameters": {"type": "object", "properties": {
                    "description": {"type": "string"},
                }, "required": ["description"]},
            }},
            {"type": "function", "function": {
                "name": "cancel_task",
                "description": "Отменить задачу по ID.",
                "parameters": {"type": "object", "properties": {
                    "task_id": {"type": "string"},
                }, "required": ["task_id"]},
            }},
            {"type": "function", "function": {
                "name": "chat_history",
                "description": "Подтянуть произвольное количество сообщений из истории чата. Поддерживает поиск.",
                "parameters": {"type": "object", "properties": {
                    "count": {"type": "integer", "default": 100, "description": "Сколько сообщений (от последнего)"},
                    "offset": {"type": "integer", "default": 0, "description": "Пропустить N от конца (пагинация)"},
                    "search": {"type": "string", "default": "", "description": "Фильтр по тексту"},
                }, "required": []},
            }},
        ]

    # --- Реализации инструментов ---

    def _tool_repo_read(self, path: str) -> str:
        return _read_text(self.repo_path(path))

    def _tool_repo_list(self, dir: str = ".", max_entries: int = 500) -> str:
        return json.dumps(_list_dir(self.repo_dir, dir, max_entries=max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_read(self, path: str) -> str:
        return _read_text(self.drive_path(path))

    def _tool_drive_list(self, dir: str = ".", max_entries: int = 500) -> str:
        return json.dumps(_list_dir(self.drive_root, dir, max_entries=max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_write(self, path: str, content: str, mode: str = "overwrite") -> str:
        p = self.drive_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite":
            p.write_text(content, encoding="utf-8")
        else:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        return f"OK: wrote {mode} {path} ({len(content)} chars)"

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
                    os.write(fd, f"locked_at={_utc_now_iso()}\n".encode("utf-8"))
                finally:
                    os.close(fd)
                return lock_path
            except FileExistsError:
                time.sleep(0.5)

    def _release_git_lock(self, lock_path: pathlib.Path) -> None:
        if lock_path.exists():
            lock_path.unlink()

    def _tool_repo_write_commit(self, path: str, content: str, commit_message: str) -> str:
        if self._ctx:
            self._ctx.last_push_succeeded = False
        if not commit_message.strip():
            return "⚠️ ERROR: commit_message must be non-empty."
        lock = self._acquire_git_lock()
        try:
            try:
                _run(["git", "checkout", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout): {e}"
            try:
                _write_text(self.repo_path(path), content)
            except Exception as e:
                return f"⚠️ FILE_WRITE_ERROR: {e}"
            try:
                _run(["git", "add", _safe_relpath(path)], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (add): {e}"
            try:
                _run(["git", "commit", "-m", commit_message], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (commit): {e}"
            # Pull --rebase перед push (предотвращает конфликты между воркерами)
            try:
                _run(["git", "pull", "--rebase", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception:
                pass  # Если не удалось — попробуем push всё равно
            try:
                _run(["git", "push", "origin", self.branch_dev], cwd=self.repo_dir)
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
                _run(["git", "checkout", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout): {e}"
            if paths:
                try:
                    safe_paths = [_safe_relpath(p) for p in paths if str(p).strip()]
                except ValueError as e:
                    return f"⚠️ PATH_ERROR: {e}"
                add_cmd = ["git", "add"] + safe_paths
            else:
                add_cmd = ["git", "add", "-A"]
            try:
                _run(add_cmd, cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (add): {e}"
            try:
                status = _run(["git", "status", "--porcelain"], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (status): {e}"
            if not status.strip():
                return "⚠️ GIT_NO_CHANGES: nothing to commit."
            try:
                _run(["git", "commit", "-m", commit_message], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (commit): {e}"
            # Pull --rebase перед push
            try:
                _run(["git", "pull", "--rebase", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception:
                pass
            try:
                _run(["git", "push", "origin", self.branch_dev], cwd=self.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (push): {e}\nCommitted locally but NOT pushed."
        finally:
            self._release_git_lock(lock)
        if self._ctx:
            self._ctx.last_push_succeeded = True
        return f"OK: committed and pushed to {self.branch_dev}: {commit_message}"

    def _tool_git_status(self) -> str:
        try:
            return _run(["git", "status", "--porcelain"], cwd=self.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR: {e}"

    def _tool_git_diff(self) -> str:
        try:
            return _run(["git", "diff"], cwd=self.repo_dir)
        except Exception as e:
            return f"⚠️ GIT_ERROR: {e}"

    def _tool_run_shell(self, cmd: List[str], cwd: str = "") -> str:
        # Ограничение git в режиме эволюции
        if self._ctx and str(self._ctx.current_task_type or "") == "evolution":
            if isinstance(cmd, list) and cmd and str(cmd[0]).lower() == "git":
                return "⚠️ EVOLUTION_GIT_RESTRICTED: используй repo_write_commit/repo_commit_push."

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
        """Делегирует правки Claude Code CLI."""
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            return "⚠️ ANTHROPIC_API_KEY не задан, claude_code_edit недоступен."

        work_dir = str(self.repo_dir)
        if cwd and cwd.strip() not in ("", ".", "./"):
            candidate = (self.repo_dir / cwd).resolve()
            if candidate.exists():
                work_dir = str(candidate)

        try:
            full_prompt = (
                f"STRICT: Only modify files inside {work_dir}. "
                f"Git branch: {self.branch_dev}. Do NOT commit or push.\n\n"
                f"{prompt}"
            )
            res = subprocess.run(
                ["claude", "--print", "--dangerously-skip-permissions", "-p", full_prompt],
                cwd=work_dir, capture_output=True, text=True,
                timeout=300,
                env={**os.environ, "ANTHROPIC_API_KEY": api_key},
            )
            out = (res.stdout or "") + ("\n--- STDERR ---\n" + res.stderr if res.stderr else "")
            if len(out) > 50000:
                out = out[:25000] + "\n...(truncated)...\n" + out[-25000:]
            return f"exit_code={res.returncode}\n{out}"
        except subprocess.TimeoutExpired:
            return "⚠️ TIMEOUT: claude_code_edit exceeded 300s."
        except FileNotFoundError:
            return "⚠️ Claude CLI не найден. Убедись что ANTHROPIC_API_KEY задан."
        except Exception as e:
            return f"⚠️ CLAUDE_ERROR: {e}"

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
            # Извлекаем текстовый ответ
            text = ""
            for item in d.get("output", []) or []:
                if item.get("type") == "message":
                    for block in item.get("content", []) or []:
                        if block.get("type") == "output_text":
                            text += block.get("text", "")
            return json.dumps({"answer": text or "(no answer)"}, ensure_ascii=False, indent=2)
        except Exception as e:
            return json.dumps({"error": repr(e)}, ensure_ascii=False)

    def _tool_request_restart(self, reason: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        if str(self._ctx.current_task_type or "") == "evolution" and not self._ctx.last_push_succeeded:
            return "⚠️ RESTART_BLOCKED: в evolution mode сначала сделай commit+push."
        self._ctx.pending_events.append({"type": "restart_request", "reason": reason, "ts": _utc_now_iso()})
        self._ctx.last_push_succeeded = False
        return f"Restart requested: {reason}"

    def _tool_promote_to_stable(self, reason: str) -> str:
        """Промоут ouroboros → ouroboros-stable. Уроборос сам решает когда (LLM-first)."""
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "promote_to_stable", "reason": reason, "ts": _utc_now_iso()})
        return f"Promote to stable requested: {reason}"

    def _tool_schedule_task(self, description: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "schedule_task", "description": description, "ts": _utc_now_iso()})
        return f"Scheduled: {description}"

    def _tool_cancel_task(self, task_id: str) -> str:
        if not self._ctx:
            return "⚠️ No context."
        self._ctx.pending_events.append({"type": "cancel_task", "task_id": task_id, "ts": _utc_now_iso()})
        return f"Cancel requested: {task_id}"

    def _tool_chat_history(self, count: int = 100, offset: int = 0, search: str = "") -> str:
        """Подтянуть произвольное количество сообщений из chat.jsonl."""
        from ouroboros.memory import Memory
        mem = Memory(drive_root=self.drive_root, repo_dir=self.repo_dir)
        return mem.chat_history(count=count, offset=offset, search=search)
