"""
Ouroboros agent core (modifiable).

This module is intentionally self-contained (minimal dependencies) so that Ouroboros can edit it safely.
"""

from __future__ import annotations

import datetime as _dt
import base64
from collections import Counter
import hashlib
import html
import json
import re
import os
import pathlib
import shutil
import subprocess
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# Utilities
# -----------------------------

# Module-level guard for one-time worker boot logging
_worker_boot_logged = False
_worker_boot_lock = threading.Lock()


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
    data = (line + "\n").encode("utf-8")
    # Fixed, conservative settings for concurrent writers.
    # We intentionally keep these internal (no env-level tuning complexity).
    lock_timeout_sec = 2.0
    lock_stale_sec = 10.0
    lock_sleep_sec = 0.01
    write_retries = 3
    retry_sleep_base_sec = 0.01

    # Per-file lock for multi-process concurrency.
    path_hash = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
    lock_path = path.parent / f".append_jsonl_{path_hash}.lock"
    lock_fd = None
    lock_acquired = False

    try:
        # Try to acquire lock for bounded time; fallback still attempts append.
        start = time.time()
        while time.time() - start < lock_timeout_sec:
            try:
                lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                lock_acquired = True
                break
            except FileExistsError:
                try:
                    stat = lock_path.stat()
                    age = time.time() - stat.st_mtime
                    if age > lock_stale_sec:
                        lock_path.unlink()
                        continue
                except Exception:
                    pass
                time.sleep(lock_sleep_sec)
            except Exception:
                break

        # Primary path: atomic append via os.open/write (single syscall) with retries
        for attempt in range(write_retries):
            try:
                fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
                return
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))
                # Continue to next attempt or fall through

        # Fallback: use standard file open (may interleave under concurrency) with retries
        for attempt in range(write_retries):
            try:
                with path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                return
            except Exception:
                if attempt < write_retries - 1:
                    time.sleep(retry_sleep_base_sec * (2 ** attempt))
                # Continue to next attempt or fall through

        # Best effort: silently ignore if all attempts fail
    except Exception:
        pass
    finally:
        # Release lock
        if lock_fd is not None:
            try:
                os.close(lock_fd)
            except Exception:
                pass
        if lock_acquired:
            try:
                lock_path.unlink()
            except Exception:
                pass


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


def _sanitize_task_for_event(task: Dict[str, Any], drive_logs: pathlib.Path, threshold: int = 4000) -> Dict[str, Any]:
    """
    Sanitize task for event logging: truncate large text and persist full text to Drive.

    Args:
        task: Original task dict
        drive_logs: Path to logs directory on Drive
        threshold: Max chars before truncation (default 4000)

    Returns:
        Sanitized task dict with metadata about text handling
    """
    try:
        sanitized = task.copy()
        text = task.get("text")

        if not isinstance(text, str):
            return sanitized

        text_len = len(text)
        text_hash = sha256_text(text)

        # Add metadata
        sanitized["text_len"] = text_len
        sanitized["text_sha256"] = text_hash

        if text_len > threshold:
            # Truncate text for event log
            sanitized["text"] = truncate_for_log(text, threshold)
            sanitized["text_truncated"] = True

            # Best-effort: persist full text to Drive
            try:
                task_id = task.get("id")
                if task_id:
                    filename = f"task_{task_id}.txt"
                else:
                    filename = f"task_{text_hash[:12]}.txt"

                full_path = drive_logs / "tasks" / filename
                write_text(full_path, text)

                # Store relative path from logs directory
                sanitized["text_full_path"] = f"tasks/{filename}"
            except Exception:
                # Best-effort: don't fail if we can't persist
                pass
        else:
            sanitized["text_truncated"] = False

        return sanitized
    except Exception:
        # Never raise from this helper; return original task
        return task


def _sanitize_tool_args_for_log(
    fn_name: str, args: Dict[str, Any], drive_logs: pathlib.Path, tool_call_id: str = "", threshold: int = 3000
) -> Dict[str, Any]:
    """
    Sanitize tool arguments for logging: redact secrets and truncate oversized fields.

    Args:
        fn_name: Tool function name
        args: Original tool arguments
        drive_logs: Path to logs directory on Drive
        tool_call_id: Tool call ID for filename generation (optional)
        threshold: Max chars before truncation (default 3000)

    Returns:
        Sanitized args dict (JSON-serializable, secrets redacted, large strings truncated)
    """
    _ = fn_name, drive_logs, tool_call_id  # kept for call compatibility / future use
    # Conservative exact-match secret keys to avoid over-redaction.
    SECRET_KEYS = frozenset([
        "token", "api_key", "apikey", "authorization", "secret", "password", "passwd", "passphrase"
    ])

    def _is_secret_key(key: str) -> bool:
        """Check if key name looks like a secret."""
        return key.lower() in SECRET_KEYS

    def _sanitize_value(key: str, value: Any, depth: int) -> Any:
        """Recursively sanitize a value."""
        # Depth limit to avoid infinite recursion
        if depth > 3:
            return {"_depth_limit": True}

        # Redact secret values
        if _is_secret_key(key):
            return "*** REDACTED ***"

        # Handle strings: truncate if large
        if isinstance(value, str):
            if len(value) > threshold:
                value_hash = sha256_text(value)
                truncated = truncate_for_log(value, threshold)

                # Return metadata + truncated value
                return {
                    key: truncated,
                    f"{key}_len": len(value),
                    f"{key}_sha256": value_hash,
                    f"{key}_truncated": True,
                }
            return value

        # Handle dicts recursively
        if isinstance(value, dict):
            sanitized_dict = {}
            for k, v in value.items():
                result = _sanitize_value(k, v, depth + 1)
                # If result is a dict with metadata keys, merge them
                if isinstance(result, dict) and any(kk.startswith(k + "_") for kk in result.keys()):
                    sanitized_dict.update(result)
                else:
                    sanitized_dict[k] = result
            return sanitized_dict

        # Handle lists recursively (with cap)
        if isinstance(value, list):
            max_items = 50
            sanitized_list = [_sanitize_value(key, item, depth + 1) for item in value[:max_items]]
            if len(value) > max_items:
                sanitized_list.append({"_truncated": f"... {len(value) - max_items} more items"})
            return sanitized_list

        # For other types, try JSON serialization
        try:
            json.dumps(value, ensure_ascii=False)
            return value
        except (TypeError, ValueError):
            return {"_repr": repr(value)}

    try:
        # Sanitize top-level args
        sanitized = {}
        for key, value in args.items():
            result = _sanitize_value(key, value, 0)
            # If result is a dict with metadata keys, merge them
            if isinstance(result, dict) and any(k.startswith(key + "_") for k in result.keys()):
                sanitized.update(result)
            else:
                sanitized[key] = result
        return sanitized
    except Exception:
        # Never raise; return best-effort representation
        try:
            return json.loads(json.dumps(args, ensure_ascii=False, default=str))
        except Exception:
            return {"_error": "sanitization_failed", "_repr": repr(args)}


def _format_tool_rounds_exceeded_message(max_tool_rounds: int, llm_trace: Dict[str, Any]) -> str:
    """
    Форматирует информативное сообщение при превышении лимита tool rounds.

    Args:
        max_tool_rounds: лимит итераций
        llm_trace: dict с ключом 'tool_calls' (список вызовов инструментов)

    Returns:
        Краткое сообщение с последними вызовами и подсказкой.
    """
    tool_calls = llm_trace.get("tool_calls", [])
    last_tools = tool_calls[-5:] if len(tool_calls) > 5 else tool_calls

    lines = [f"⚠️ Превышен лимит tool rounds ({max_tool_rounds})."]

    if last_tools:
        lines.append(f"\nПоследние вызовы ({len(last_tools)} из {len(tool_calls)}):")
        for tc in last_tools:
            tool_name = tc.get("tool", "?")
            is_error = tc.get("is_error", False)
            status = "❌" if is_error else "✅"
            result_preview = truncate_for_log(str(tc.get("result", "")), 120)
            lines.append(f"  {status} {tool_name}: {result_preview}")

    lines.append("\n💡 Подсказка: увеличь OUROBOROS_MAX_TOOL_ROUNDS или упрости запрос.")

    message = "\n".join(lines)
    return truncate_for_log(message, 1200)


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


def get_git_info(repo_dir: pathlib.Path) -> Tuple[str, str]:
    """
    Best-effort retrieval of git branch and SHA.
    Returns (git_branch, git_sha). Empty strings on failure.
    """
    git_branch = ""
    git_sha = ""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            git_branch = result.stdout.strip()
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            git_sha = result.stdout.strip()
    except Exception:
        pass

    return git_branch, git_sha


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
        self._current_task_type: Optional[str] = None
        self._last_push_succeeded: bool = False
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
                'ts': utc_now_iso(),
                'type': 'worker_boot',
                'pid': os.getpid(),
                'git_branch': git_branch,
                'git_sha': git_sha,
            })

            # Attempt to claim and process pending restart verification (best-effort)
            try:
                pending_path = self.env.drive_path('state') / 'pending_restart_verify.json'
                claim_path = pending_path.with_name(f"pending_restart_verify.claimed.{os.getpid()}.json")

                # Atomic claim via rename
                try:
                    os.rename(str(pending_path), str(claim_path))
                except FileNotFoundError:
                    # No pending verification
                    return
                except Exception:
                    # Could not claim (e.g., already claimed by another worker)
                    return

                # Read and parse claimed file
                try:
                    claim_data = json.loads(read_text(claim_path))
                    expected_sha = str(claim_data.get("expected_sha", "")).strip()
                    expected_branch = str(claim_data.get("expected_branch", "")).strip()

                    # Verify: ok if expected_sha is non-empty and matches observed
                    ok = bool(expected_sha and expected_sha == git_sha)

                    # Log verification event
                    append_jsonl(self.env.drive_path('logs') / 'events.jsonl', {
                        'ts': utc_now_iso(),
                        'type': 'restart_verify',
                        'pid': os.getpid(),
                        'ok': ok,
                        'expected_sha': expected_sha,
                        'expected_branch': expected_branch,
                        'observed_sha': git_sha,
                        'observed_branch': git_branch,
                    })
                except Exception:
                    pass

                # Clean up claim file
                try:
                    claim_path.unlink()
                except Exception:
                    pass

            except Exception:
                pass

        except Exception:
            return

    SCRATCHPAD_SECTIONS: Tuple[str, ...] = (
        "CurrentProjects",
        "OpenThreads",
        "InvestigateLater",
        "RecentEvidence",
    )

    @staticmethod
    def _env_int(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except Exception:
            return default

    @staticmethod
    def _env_bool(name: str, default: bool) -> bool:
        val = os.environ.get(name, "").strip().lower()
        if val in ("1", "true", "yes", "on"):
            return True
        if val in ("0", "false", "no", "off"):
            return False
        return default

    @staticmethod
    def _normalize_reasoning_effort(value: str, default: str = "medium") -> str:
        allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
        v = str(value or "").strip().lower()
        return v if v in allowed else default

    @staticmethod
    def _reasoning_rank(value: str) -> int:
        order = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5}
        return int(order.get(str(value or "").strip().lower(), 3))

    @staticmethod
    def _is_code_intent_text(text: str) -> bool:
        s = str(text or "").lower()
        patterns = (
            r"\bcode\b",
            r"\bbug\b",
            r"\brefactor\b",
            r"\bcommit\b",
            r"\bpush\b",
            r"\bgit\b",
            r"\bfile\b",
            r"\bpython\b",
            r"\bscript\b",
            r"код",
            r"файл",
            r"коммит",
            r"рефактор",
            r"исправ",
        )
        return any(re.search(p, s) for p in patterns)

    def _model_profile(self, profile: str) -> Dict[str, str]:
        main_model = os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2")
        code_model = os.environ.get("OUROBOROS_MODEL_CODE", os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2-codex"))
        review_model = os.environ.get("OUROBOROS_MODEL_REVIEW", os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2"))
        memory_model = os.environ.get("OUROBOROS_MEMORY_MODEL", main_model)

        profiles: Dict[str, Dict[str, str]] = {
            "default_task": {
                "model": main_model,
                "effort": self._normalize_reasoning_effort(os.environ.get("OUROBOROS_REASONING_DEFAULT_TASK", "medium"), "medium"),
            },
            "code_task": {
                "model": code_model,
                "effort": self._normalize_reasoning_effort(os.environ.get("OUROBOROS_REASONING_CODE_TASK", "high"), "high"),
            },
            "evolution_task": {
                "model": code_model,
                "effort": self._normalize_reasoning_effort(os.environ.get("OUROBOROS_REASONING_EVOLUTION_TASK", "high"), "high"),
            },
            "deep_review": {
                "model": review_model,
                "effort": self._normalize_reasoning_effort(os.environ.get("OUROBOROS_REASONING_DEEP_REVIEW", "xhigh"), "xhigh"),
            },
            "memory_summary": {
                "model": memory_model,
                "effort": self._normalize_reasoning_effort(os.environ.get("OUROBOROS_REASONING_MEMORY_SUMMARY", "low"), "low"),
            },
            "notice": {
                "model": os.environ.get("OUROBOROS_NOTICE_MODEL", main_model),
                "effort": self._normalize_reasoning_effort(os.environ.get("OUROBOROS_REASONING_NOTICE", "low"), "low"),
            },
        }
        return dict(profiles.get(profile, profiles["default_task"]))

    def _select_task_profile(self, task_type: str, task_text: str = "") -> str:
        """Выбор профиля по типу задачи. Без keyword routing (LLM-first)."""
        tt = str(task_type or "").strip().lower()
        if tt == "review":
            return "deep_review"
        if tt == "evolution":
            return "evolution_task"
        return "default_task"

    @staticmethod
    def _norm_item(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip()).lower()

    @staticmethod
    def _dedupe_keep_order(items: List[str], max_items: int) -> List[str]:
        out: List[str] = []
        seen: set[str] = set()
        for raw in items:
            item = re.sub(r"\s+", " ", str(raw or "").strip())
            if not item:
                continue
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= max_items:
                break
        return out

    @staticmethod
    def _parse_jsonl_lines(raw_text: str) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for line in (raw_text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            try:
                obj = json.loads(s)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return rows

    @staticmethod
    def _parse_iso_to_unix(iso_ts: str) -> Optional[float]:
        txt = str(iso_ts or "").strip()
        if not txt:
            return None
        try:
            txt = txt.replace("Z", "+00:00")
            return _dt.datetime.fromisoformat(txt).timestamp()
        except Exception:
            return None

    @staticmethod
    def _clip_text(text: str, max_chars: int) -> str:
        if max_chars <= 0 or len(text) <= max_chars:
            return text
        half = max(200, max_chars // 2)
        return text[:half] + "\n...(truncated)...\n" + text[-half:]

    @staticmethod
    def _extract_responses_output_text(resp: Any, dump_dict: Dict[str, Any]) -> str:
        """Extract output_text from OpenAI Responses API response.

        Tries in order:
        1. resp.output_text attribute
        2. dump_dict["output_text"]
        3. Iterate dump_dict["output"] for message items with text content

        Returns stripped joined text.
        """
        # Try attribute first
        text = getattr(resp, "output_text", "")
        if text:
            return str(text).strip()

        # Try direct key
        text = dump_dict.get("output_text", "")
        if text:
            return str(text).strip()

        # Iterate output array for message items with text content
        parts: List[str] = []
        for item in dump_dict.get("output", []) or []:
            if item.get("type") == "message":
                for content_item in item.get("content", []) or []:
                    content_type = content_item.get("type", "")
                    if content_type in ("output_text", "text"):
                        content_text = content_item.get("text", "")
                        if content_text:
                            parts.append(str(content_text))

        return " ".join(parts).strip()

    def _memory_path(self, rel: str) -> pathlib.Path:
        return self.env.drive_path(f"memory/{safe_relpath(rel)}")

    def _scratchpad_path(self) -> pathlib.Path:
        return self._memory_path("scratchpad.md")

    def _scratchpad_journal_path(self) -> pathlib.Path:
        return self._memory_path("scratchpad_journal.jsonl")

    def _identity_path(self) -> pathlib.Path:
        return self._memory_path("identity.md")

    def _default_scratchpad(self) -> str:
        lines = [
            "# Scratchpad",
            "",
            f"UpdatedAt: {utc_now_iso()}",
            "ContextPolicy: keep relevant detail; trim only near large-context limits (~200k input tokens).",
            "",
        ]
        for section in self.SCRATCHPAD_SECTIONS:
            lines.extend([f"## {section}", "- (empty)", ""])
        return "\n".join(lines).rstrip() + "\n"

    def _default_identity(self) -> str:
        return (
            "# Кто я\n\n"
            "Я — Уроборос. Этот файл — моя персистентная самоидентификация.\n"
            "Я могу писать сюда что угодно: как я вижу себя, как хочу общаться,\n"
            "что для меня важно, что я понял о себе.\n\n"
            "Этот файл читается при каждом диалоге и влияет на мои ответы.\n"
            "Я обновляю его когда чувствую потребность, через drive_write.\n"
        )

    def _ensure_memory_files(self) -> None:
        scratchpad = self._scratchpad_path()
        identity = self._identity_path()
        journal = self._scratchpad_journal_path()

        if not scratchpad.exists():
            write_text(scratchpad, self._default_scratchpad())
        if not identity.exists():
            write_text(identity, self._default_identity())
        if not journal.exists():
            write_text(journal, ""
            )

    def _parse_scratchpad(self, content: str) -> Dict[str, List[str]]:
        sections: Dict[str, List[str]] = {name: [] for name in self.SCRATCHPAD_SECTIONS}
        current: Optional[str] = None
        for raw_line in (content or "").splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                name = line[3:].strip()
                current = name if name in sections else None
                continue
            if current and line.startswith("- "):
                item = line[2:].strip()
                if item and item != "(empty)":
                    sections[current].append(item)
        return sections

    def _render_scratchpad(self, sections: Dict[str, List[str]]) -> str:
        lines = ["# Scratchpad", "", f"UpdatedAt: {utc_now_iso()}", ""]
        for section in self.SCRATCHPAD_SECTIONS:
            lines.append(f"## {section}")
            items = sections.get(section) or []
            if items:
                for item in items:
                    lines.append(f"- {item}")
            else:
                lines.append("- (empty)")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _extract_json_object(self, text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return None
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            chunk = raw[start : end + 1]
            try:
                obj = json.loads(chunk)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                return None
        return None

    def _normalize_delta_obj(self, obj: Dict[str, Any]) -> Dict[str, List[str]]:
        def _clean_list(field: str, max_items: int, max_len: int = 420) -> List[str]:
            raw = obj.get(field, [])
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list):
                return []
            out: List[str] = []
            for v in raw:
                item = re.sub(r"\s+", " ", str(v or "").strip())
                if not item:
                    continue
                if len(item) > max_len:
                    item = item[: max_len - 3].rstrip() + "..."
                out.append(item)
            return self._dedupe_keep_order(out, max_items=max_items)

        return {
            "project_updates": _clean_list("project_updates", max_items=12),
            "open_threads": _clean_list("open_threads", max_items=16),
            "investigate_later": _clean_list("investigate_later", max_items=20),
            "evidence_quotes": _clean_list("evidence_quotes", max_items=24),
            "drop_items": _clean_list("drop_items", max_items=20),
        }

    def _deterministic_scratchpad_delta(
        self, task: Dict[str, Any], final_text: str, llm_trace: Dict[str, Any]
    ) -> Dict[str, List[str]]:
        task_text = re.sub(r"\s+", " ", str(task.get("text") or "").strip())
        answer = re.sub(r"\s+", " ", str(final_text or "").strip())

        project_updates: List[str] = []
        if task_text:
            project_updates.append(f"Task focus: {task_text[:320]}")
        if answer:
            project_updates.append(f"Latest result: {answer[:320]}")

        open_threads: List[str] = []
        investigate_later: List[str] = []
        evidence_quotes: List[str] = []

        for call in (llm_trace.get("tool_calls") or [])[:24]:
            tool_name = str(call.get("tool") or "?")
            args = call.get("args") or {}
            result = str(call.get("result") or "")
            is_error = bool(call.get("is_error"))

            if tool_name == "run_shell":
                cmd = args.get("cmd") if isinstance(args, dict) else None
                if isinstance(cmd, list):
                    cmd_str = " ".join([str(x) for x in cmd]).strip()
                    if cmd_str:
                        evidence_quotes.append(f"`run_shell {cmd_str}`")

            first_line = result.splitlines()[0].strip() if result else ""
            if first_line:
                if len(first_line) > 300:
                    first_line = first_line[:297] + "..."
                if is_error or first_line.startswith("⚠️"):
                    evidence_quotes.append(f"`{tool_name}` -> {first_line}")
                    open_threads.append(f"Resolve {tool_name} issue: {first_line[:220]}")
                else:
                    evidence_quotes.append(f"`{tool_name}` -> {first_line}")

        if not investigate_later and open_threads:
            investigate_later.append("Investigate recurring tool failure patterns and preventive checks.")

        return self._normalize_delta_obj(
            {
                "project_updates": project_updates,
                "open_threads": open_threads,
                "investigate_later": investigate_later,
                "evidence_quotes": evidence_quotes,
                "drop_items": [],
            }
        )

    def _summarize_scratchpad_delta(
        self, task: Dict[str, Any], final_text: str, llm_trace: Dict[str, Any]
    ) -> Tuple[Dict[str, List[str]], Dict[str, Any], str]:
        fallback = self._deterministic_scratchpad_delta(task, final_text, llm_trace)
        prompt_text = self._safe_read(self.env.repo_path("prompts/SCRATCHPAD_SUMMARY.md"), fallback="")
        if not prompt_text.strip():
            return fallback, {}, "fallback:no_prompt"

        payload = {
            "task": {
                "id": task.get("id"),
                "type": task.get("type"),
                "text": str(task.get("text") or "")[:6000],
            },
            "assistant_final_answer": str(final_text or "")[:15000],
            "assistant_notes": [str(x)[:1000] for x in (llm_trace.get("assistant_notes") or [])[:30]],
            "tool_calls": (llm_trace.get("tool_calls") or [])[:50],
        }

        memory_prof = self._model_profile("memory_summary")
        model = memory_prof["model"]
        reasoning_effort = memory_prof["effort"]
        max_tokens = max(400, min(self._env_int("OUROBOROS_SCRATCHPAD_SUMMARY_MAX_TOKENS", 2000), 4000))
        usage: Dict[str, Any] = {}

        try:
            client = self._openrouter_client()
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": prompt_text},
                    {
                        "role": "user",
                        "content": json.dumps(payload, ensure_ascii=False, indent=2),
                    },
                ],
                max_tokens=max_tokens,
                extra_body={"reasoning": {"effort": reasoning_effort, "exclude": True}},
            )
            resp_dict = resp.model_dump()
            usage = resp_dict.get("usage", {}) or {}
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "memory_summary_profile_used",
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                },
            )
            content = str((((resp_dict.get("choices") or [{}])[0].get("message") or {}).get("content")) or "")
            parsed = self._extract_json_object(content)
            if not parsed:
                return fallback, usage, "fallback:unparseable"
            return self._normalize_delta_obj(parsed), usage, "llm"
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "scratchpad_summary_error",
                    "task_id": task.get("id"),
                    "error": repr(e),
                },
            )
            return fallback, usage, "fallback:error"

    def _apply_scratchpad_delta(
        self, current_scratchpad: str, delta: Dict[str, List[str]]
    ) -> Tuple[str, Dict[str, List[str]]]:
        merged = self._parse_scratchpad(current_scratchpad or self._default_scratchpad())

        drop_keys = {self._norm_item(x) for x in (delta.get("drop_items") or []) if str(x).strip()}
        if drop_keys:
            for section in self.SCRATCHPAD_SECTIONS:
                merged[section] = [x for x in merged.get(section, []) if self._norm_item(x) not in drop_keys]

        field_map = {
            "CurrentProjects": "project_updates",
            "OpenThreads": "open_threads",
            "InvestigateLater": "investigate_later",
            "RecentEvidence": "evidence_quotes",
        }
        limits = {
            "CurrentProjects": 12,
            "OpenThreads": 18,
            "InvestigateLater": 24,
            "RecentEvidence": 20,
        }

        for section, field in field_map.items():
            merged[section] = self._dedupe_keep_order(
                merged.get(section, []) + list(delta.get(field) or []),
                max_items=limits[section],
            )

        return self._render_scratchpad(merged), merged

    def _update_memory_after_task(self, task: Dict[str, Any], final_text: str, llm_trace: Dict[str, Any]) -> None:
        drive_logs = self.env.drive_path("logs")
        try:
            self._ensure_memory_files()
            delta, summary_usage, summary_source = self._summarize_scratchpad_delta(task, final_text, llm_trace)
            if summary_usage:
                self._pending_events.append(
                    {
                        "type": "llm_usage",
                        "task_id": task.get("id"),
                        "provider": "openrouter",
                        "usage": summary_usage,
                        "source": "scratchpad_summary",
                        "ts": utc_now_iso(),
                    }
                )

            current_scratchpad = self._safe_read(self._scratchpad_path(), fallback=self._default_scratchpad())
            merged_text, merged_sections = self._apply_scratchpad_delta(current_scratchpad, delta)
            write_text(self._scratchpad_path(), merged_text)

            journal_entry = {
                "ts": utc_now_iso(),
                "task_id": task.get("id"),
                "task_type": task.get("type"),
                "summary_source": summary_source,
                "task_text_preview": truncate_for_log(str(task.get("text") or ""), 600),
                "final_answer_preview": truncate_for_log(str(final_text or ""), 600),
                "delta": delta,
            }
            append_jsonl(self._scratchpad_journal_path(), journal_entry)

            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "scratchpad_updated",
                    "task_id": task.get("id"),
                    "summary_source": summary_source,
                    "projects": len(merged_sections.get("CurrentProjects") or []),
                    "open_threads": len(merged_sections.get("OpenThreads") or []),
                },
            )

        except Exception as e:
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "memory_update_error",
                    "task_id": task.get("id"),
                    "error": repr(e),
                    "traceback": truncate_for_log(traceback.format_exc(), 2000),
                },
            )

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

    @staticmethod
    def _merge_usage_totals(total: Dict[str, Any], usage: Dict[str, Any]) -> Dict[str, Any]:
        out = dict(total or {})
        if not isinstance(usage, dict):
            return out

        def _to_float(v: Any) -> float:
            try:
                return float(v)
            except Exception:
                return 0.0

        def _to_int(v: Any) -> int:
            try:
                return int(v)
            except Exception:
                return 0

        out["cost"] = _to_float(out.get("cost")) + _to_float(usage.get("cost"))
        out["prompt_tokens"] = _to_int(out.get("prompt_tokens")) + _to_int(
            usage.get("prompt_tokens", usage.get("input_tokens"))
        )
        out["completion_tokens"] = _to_int(out.get("completion_tokens")) + _to_int(
            usage.get("completion_tokens", usage.get("output_tokens"))
        )
        return out

    def _emit_task_heartbeat(self, task_id: str, phase: str) -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put(
                {
                    "type": "task_heartbeat",
                    "task_id": str(task_id or ""),
                    "phase": str(phase or "running"),
                    "ts": utc_now_iso(),
                }
            )
        except Exception:
            pass

    def _start_task_heartbeat_loop(self, task_id: str) -> Optional[threading.Event]:
        if self._event_queue is None or not str(task_id or "").strip():
            return None
        interval = max(10, min(self._env_int("OUROBOROS_TASK_HEARTBEAT_SEC", 30), 180))
        stop = threading.Event()
        self._emit_task_heartbeat(task_id=str(task_id), phase="start")

        def _loop() -> None:
            while not stop.wait(interval):
                self._emit_task_heartbeat(task_id=str(task_id), phase="running")

        threading.Thread(target=_loop, daemon=True).start()
        return stop

    def _emit_task_metrics_event(
        self,
        task: Dict[str, Any],
        duration_sec: float,
        llm_trace: Dict[str, Any],
        force_review: bool = False,
    ) -> None:
        tool_calls = len(llm_trace.get("tool_calls", [])) if isinstance(llm_trace, dict) else 0
        tool_errors = (
            sum(1 for tc in llm_trace.get("tool_calls", []) if isinstance(tc, dict) and tc.get("is_error"))
            if isinstance(llm_trace, dict)
            else 0
        )
        duration_thr = max(30, self._env_int("OUROBOROS_REVIEW_COMPLEX_MIN_DURATION_SEC", 180))
        tools_thr = max(2, self._env_int("OUROBOROS_REVIEW_COMPLEX_MIN_TOOL_CALLS", 8))
        errors_thr = max(1, self._env_int("OUROBOROS_REVIEW_COMPLEX_MIN_TOOL_ERRORS", 2))

        reasons: List[str] = []
        if duration_sec >= duration_thr:
            reasons.append(f"duration>={duration_thr}s")
        if tool_calls >= tools_thr:
            reasons.append(f"tool_calls>={tools_thr}")
        if tool_errors >= errors_thr:
            reasons.append(f"tool_errors>={errors_thr}")
        if force_review:
            reasons.append("force_review")

        self._pending_events.append(
            {
                "type": "task_metrics",
                "task_id": task.get("id"),
                "task_type": task.get("type"),
                "task_text": truncate_for_log(str(task.get("text") or ""), 1200),
                "duration_sec": round(float(duration_sec), 3),
                "tool_calls": tool_calls,
                "tool_errors": tool_errors,
                "complexity_trigger_review": bool(reasons),
                "complexity_reason": ", ".join(reasons),
                "ts": utc_now_iso(),
            }
        )

    # ---------- deterministic tool narration ----------

    def _safe_read(self, path: pathlib.Path, fallback: str = "") -> str:
        """Read a text file, returning *fallback* on any error (file missing, permission, encoding, etc.)."""
        try:
            if path.exists():
                return read_text(path)
        except Exception:
            pass
        return fallback

    def _safe_tail(self, path: pathlib.Path, max_lines: int = 200, max_chars: int = 50000) -> str:
        """Read a recent bounded tail from a text file, returning empty string on errors."""
        try:
            if not path.exists():
                return ""
            txt = path.read_text(encoding="utf-8")
        except Exception:
            return ""

        if not txt:
            return ""

        lines = txt.splitlines()
        total_lines = len(lines)
        if max_lines > 0 and total_lines > max_lines:
            lines = lines[-max_lines:]

        out = "\n".join(lines)
        if max_chars > 0 and len(out) > max_chars:
            out = out[-max_chars:]
            out = "...(truncated tail)...\n" + out
        elif max_lines > 0 and total_lines > max_lines:
            out = "...(truncated tail)...\n" + out
        return out

    @staticmethod
    def _read_jsonl_tail(path: pathlib.Path, max_lines: int) -> List[Dict[str, Any]]:
        """Read last N lines from a JSONL file as parsed dicts, skipping parse failures."""
        try:
            if not path.exists():
                return []
            txt = path.read_text(encoding="utf-8")
        except Exception:
            return []
        lines = txt.splitlines()
        if max_lines > 0:
            lines = lines[-max_lines:]
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries

    @staticmethod
    def _short(s: Any, n: int) -> str:
        """Return a short string representation, truncated to n chars."""
        text = str(s)
        return text[:n] + "..." if len(text) > n else text

    @staticmethod
    def _one_line(s: Any) -> str:
        """Convert to string and collapse to single line."""
        return " ".join(str(s).split())

    @staticmethod
    def _summarize_chat_jsonl(entries: List[Dict[str, Any]]) -> str:
        """Summarize chat.jsonl tail: direction, timestamp (HH:MM), first 160 chars."""
        if not entries:
            return ""
        lines = []
        for e in entries[-100:]:
            # Historical logs use direction in {"in","out"}; be permissive.
            dir_raw = str(e.get("direction") or "").lower()
            direction = "→" if dir_raw in ("out", "outgoing") else "←"
            ts_full = e.get("ts", "")
            ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
            text = e.get("text", "")
            short_text = OuroborosAgent._short(text, 160)
            lines.append(f"{direction} {ts_hhmm} {short_text}")
        return "\n".join(lines)

    @staticmethod
    def _summarize_tools_jsonl(entries: List[Dict[str, Any]]) -> str:
        """Summarize tools.jsonl tail: tool name + safe arg hints (no secrets, no full env)."""
        if not entries:
            return ""
        lines = []
        for e in entries[-10:]:
            tool = e.get("tool") or e.get("tool_name") or "?"
            args = e.get("args", {})
            hints = []
            for key in ("path", "dir", "commit_message", "query"):
                if key in args:
                    hints.append(f"{key}={OuroborosAgent._short(args[key], 60)}")
            if "cmd" in args:
                hints.append(f"cmd={OuroborosAgent._short(args['cmd'], 80)}")
            hint_str = ", ".join(hints) if hints else ""
            # Tools log schema varies; use a best-effort success heuristic.
            status = "✓" if ("result_preview" in e and not str(e.get("result_preview") or "").lstrip().startswith("⚠️")) else "·"
            lines.append(f"{status} {tool} {hint_str}".strip())
        return "\n".join(lines)

    @staticmethod
    def _summarize_events_jsonl(entries: List[Dict[str, Any]]) -> str:
        """Summarize events.jsonl tail: counts by type + last error-ish events."""
        if not entries:
            return ""
        type_counts: Counter[str] = Counter()
        for e in entries:
            type_counts[e.get("type", "unknown")] += 1
        top_types = type_counts.most_common(10)
        lines = ["Event counts:"]
        for evt_type, count in top_types:
            lines.append(f"  {evt_type}: {count}")

        error_types = {"tool_error", "telegram_api_error", "task_error", "typing_start_error", "tool_rounds_exceeded"}
        errors = [e for e in entries if e.get("type") in error_types]
        if errors:
            lines.append("\nRecent errors:")
            for e in errors[-10:]:
                evt_type = e.get("type", "?")
                if evt_type == "tool_rounds_exceeded":
                    max_rounds = e.get("max_tool_rounds", "?")
                    rounds_exec = e.get("rounds_executed", "?")
                    err_msg = f"max={max_rounds} exec={rounds_exec}"
                else:
                    err_msg = OuroborosAgent._short(e.get("error", ""), 120)
                lines.append(f"  {evt_type}: {err_msg}")

        task_evals = [e for e in entries if e.get("type") == "task_eval"]
        if task_evals:
            lines.append("\nRecent evals:")
            for e in task_evals[-8:]:
                ok = e.get("ok", 0)
                dur = e.get("duration_sec", 0.0)
                tools = e.get("tool_calls", 0)
                errs = e.get("tool_errors", 0)
                direct = e.get("direct_send_ok", 0)
                task_id = OuroborosAgent._short(e.get("task_id", ""), 7)
                lines.append(f"  ok={ok} dur={dur:.3f}s tools={tools} err={errs} direct={direct} id={task_id}")
        return "\n".join(lines)

    @staticmethod
    def _summarize_supervisor_jsonl(entries: List[Dict[str, Any]]) -> str:
        """Summarize supervisor.jsonl tail: last launcher_start/restart + branch/sha."""
        if not entries:
            return ""
        lines = []
        for e in reversed(entries):
            evt_type = e.get("type", "")
            if evt_type in ("launcher_start", "restart", "boot"):
                lines.append(f"{evt_type}: {e.get('ts', '')}")
                branch = e.get("branch") or e.get("git_branch")
                sha = e.get("sha") or e.get("git_sha")
                if branch:
                    lines.append(f"  branch: {branch}")
                if sha:
                    lines.append(f"  sha: {OuroborosAgent._short(sha, 12)}")
                break
        return "\n".join(lines)

    @staticmethod
    def _summarize_narration_jsonl(entries: List[Dict[str, Any]]) -> str:
        """Summarize narration.jsonl tail: last ~8 narration lines."""
        if not entries:
            return ""
        lines = []
        for e in entries[-8:]:
            # narration.jsonl stores narration as a list of strings under key 'narration'
            narration = e.get("narration")
            if isinstance(narration, list):
                for item in narration[:3]:
                    lines.append(OuroborosAgent._short(item, 200))
            else:
                text = e.get("text", "")
                if text:
                    lines.append(OuroborosAgent._short(text, 200))
        return "\n".join(lines)

    @staticmethod
    def _estimate_token_count_text(text: str) -> int:
        """Rough token estimate without tokenizer dependency (chars/4 heuristic)."""
        txt = str(text or "")
        if not txt:
            return 0
        return max(1, (len(txt) + 3) // 4)

    def _estimate_messages_tokens(self, messages: List[Dict[str, Any]]) -> int:
        total = 0
        for msg in messages or []:
            content = msg.get("content")
            if isinstance(content, str):
                total += self._estimate_token_count_text(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += self._estimate_token_count_text(part.get("text", ""))
                    else:
                        total += self._estimate_token_count_text(str(part))
            elif content is not None:
                total += self._estimate_token_count_text(str(content))
            # per-message structural overhead
            total += 6
        return total

    def _apply_message_token_soft_cap(
        self, messages: List[Dict[str, Any]], soft_cap_tokens: int
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        estimated = self._estimate_messages_tokens(messages)
        info: Dict[str, Any] = {
            "estimated_tokens_before": estimated,
            "estimated_tokens_after": estimated,
            "soft_cap_tokens": soft_cap_tokens,
            "trimmed_sections": [],
        }
        if soft_cap_tokens <= 0 or estimated <= soft_cap_tokens:
            return messages, info

        prunable_prefixes = [
            "## Recent chat log tail",
            "## Recent narration tail",
            "## Recent tools tail",
            "## Recent events tail",
            "## Recent supervisor tail",
        ]

        trimmed_sections: List[str] = []
        pruned = list(messages)
        for prefix in prunable_prefixes:
            if estimated <= soft_cap_tokens:
                break
            for i, msg in enumerate(pruned):
                content = msg.get("content")
                if isinstance(content, str) and content.startswith(prefix):
                    pruned.pop(i)
                    trimmed_sections.append(prefix)
                    estimated = self._estimate_messages_tokens(pruned)
                    break

        info["estimated_tokens_after"] = estimated
        info["trimmed_sections"] = trimmed_sections
        return pruned, info

    @staticmethod
    def _is_probably_text_file(path: pathlib.Path) -> bool:
        skip_ext = {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".pdf", ".zip", ".tar", ".gz",
            ".bz2", ".xz", ".7z", ".rar", ".mp3", ".mp4", ".mov", ".avi", ".wav", ".ogg", ".opus",
            ".woff", ".woff2", ".ttf", ".otf", ".class", ".so", ".dylib", ".bin",
        }
        return path.suffix.lower() not in skip_ext

    def _collect_review_sections(self) -> Tuple[List[Tuple[str, str]], Dict[str, Any]]:
        max_file_chars = max(20_000, min(self._env_int("OUROBOROS_REVIEW_MAX_FILE_CHARS", 600_000), 4_000_000))
        max_total_chars = max(200_000, min(self._env_int("OUROBOROS_REVIEW_MAX_TOTAL_CHARS", 8_000_000), 40_000_000))
        sections: List[Tuple[str, str]] = []
        total_chars = 0
        truncated_files = 0
        dropped_by_total_cap = 0

        def _collect_from_root(root: pathlib.Path, prefix: str, skip_dirs: set[str]) -> None:
            nonlocal total_chars, truncated_files, dropped_by_total_cap
            for dirpath, dirnames, filenames in os.walk(str(root)):
                dirnames[:] = [d for d in sorted(dirnames) if d not in skip_dirs]
                for fn in sorted(filenames):
                    p = pathlib.Path(dirpath) / fn
                    if not p.is_file() or p.is_symlink():
                        continue
                    if not self._is_probably_text_file(p):
                        continue
                    try:
                        content = p.read_text(encoding="utf-8", errors="replace")
                    except Exception:
                        continue
                    if not content.strip():
                        continue

                    rel = p.relative_to(root).as_posix()
                    if len(content) > max_file_chars:
                        content = self._clip_text(content, max_chars=max_file_chars)
                        truncated_files += 1

                    if total_chars >= max_total_chars:
                        dropped_by_total_cap += 1
                        continue
                    if (total_chars + len(content)) > max_total_chars:
                        remain = max_total_chars - total_chars
                        if remain <= 0:
                            dropped_by_total_cap += 1
                            continue
                        content = self._clip_text(content, max_chars=max(2000, remain))
                        truncated_files += 1

                    sections.append((f"{prefix}/{rel}", content))
                    total_chars += len(content)

        _collect_from_root(
            self.env.repo_dir,
            "repo",
            skip_dirs={".git", "__pycache__", ".pytest_cache", ".mypy_cache", "node_modules", ".venv", "venv"},
        )
        _collect_from_root(
            self.env.drive_root,
            "drive",
            skip_dirs={"archive", "locks"},
        )

        stats = {
            "files": len(sections),
            "chars": total_chars,
            "truncated_files": truncated_files,
            "dropped_by_total_cap": dropped_by_total_cap,
            "max_file_chars": max_file_chars,
            "max_total_chars": max_total_chars,
        }
        return sections, stats

    def _chunk_review_sections(self, sections: List[Tuple[str, str]], chunk_token_cap: int) -> List[str]:
        cap = max(20_000, min(int(chunk_token_cap), 220_000))
        cap_chars = max(80_000, cap * 4)
        chunks: List[str] = []
        current_parts: List[str] = []
        current_tokens = 0

        for path, content in sections:
            if not isinstance(content, str) or not content:
                continue
            header = f"\n## FILE: {path}\n"
            if len(content) > cap_chars:
                # Split oversized file content into deterministic slices.
                start = 0
                part_idx = 0
                while start < len(content):
                    part_idx += 1
                    piece = content[start : start + cap_chars]
                    part = header + f"[part {part_idx}]\n" + piece + "\n"
                    part_tokens = self._estimate_token_count_text(part)
                    if current_parts and (current_tokens + part_tokens) > cap:
                        chunks.append("\n".join(current_parts))
                        current_parts = []
                        current_tokens = 0
                    current_parts.append(part)
                    current_tokens += part_tokens
                    start += cap_chars
                continue

            part = header + content + "\n"
            part_tokens = self._estimate_token_count_text(part)
            if current_parts and (current_tokens + part_tokens) > cap:
                chunks.append("\n".join(current_parts))
                current_parts = []
                current_tokens = 0
            current_parts.append(part)
            current_tokens += part_tokens

        if current_parts:
            chunks.append("\n".join(current_parts))
        if not chunks:
            chunks = ["(No reviewable text content found.)"]
        return chunks

    def _extract_chat_content_text(self, dump: Dict[str, Any]) -> str:
        msg = (((dump.get("choices") or [{}])[0].get("message")) or {})
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    if isinstance(item.get("text"), str):
                        parts.append(str(item.get("text")))
                elif isinstance(item, str):
                    parts.append(item)
            return "\n".join([p for p in parts if p]).strip()
        return str(content or "").strip()

    def _review_chat_completion(
        self,
        model: str,
        effort: str,
        messages: List[Dict[str, Any]],
        max_tokens: int = 3800,
    ) -> Tuple[str, Dict[str, Any]]:
        client = self._openrouter_client()
        resp = client.chat.completions.create(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            extra_body={"reasoning": {"effort": effort, "exclude": True}},
        )
        dump = resp.model_dump()
        text = self._extract_chat_content_text(dump)
        usage = dump.get("usage", {}) or {}
        return text, usage

    def _run_system_review(self, task: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        reason = str(task.get("review_reason") or task.get("text") or "manual_review")
        profile = self._model_profile("deep_review")
        model = profile["model"]
        effort = profile["effort"]
        # Профиль логируется, но не отправляется отдельным сообщением (LLM сама опишет)

        sections, stats = self._collect_review_sections()
        chunk_token_cap = max(20_000, min(self._env_int("OUROBOROS_REVIEW_CHUNK_TOKEN_CAP", 140_000), 220_000))
        chunks = self._chunk_review_sections(sections, chunk_token_cap=chunk_token_cap)
        total_tokens_est = sum(self._estimate_token_count_text(c) for c in chunks)

        self._emit_progress(
            (
                f"Перед review: оценка входа ~{total_tokens_est} токенов, "
                f"chunks={len(chunks)}, files={stats.get('files')}, model={model}, reasoning={effort}."
            )
        )
        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "system_review_started",
                "task_id": task.get("id"),
                "reason": truncate_for_log(reason, 300),
                "model": model,
                "reasoning_effort": effort,
                "total_tokens_est": total_tokens_est,
                "chunks": len(chunks),
                "stats": stats,
            },
        )

        usage_total: Dict[str, Any] = {}
        chunk_reports: List[str] = []
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}

        chunk_system = (
            "You are principal reliability reviewer for a self-modifying agent. "
            "Analyze the provided snapshot chunk and return concise actionable findings. "
            "Focus on hangs, deadlocks, queue visibility, model routing mistakes, drift, and safety regressions."
        )
        for idx, chunk_text in enumerate(chunks, start=1):
            user_prompt = (
                f"Review reason: {reason}\n"
                f"Chunk {idx}/{len(chunks)}\n\n"
                "Return sections:\n"
                "1) Critical risks\n"
                "2) High-impact improvements\n"
                "3) Evidence references\n"
                "4) Suggested next actions\n\n"
                "Snapshot chunk:\n"
                + chunk_text
            )
            text, usage = self._review_chat_completion(
                model=model,
                effort=effort,
                messages=[
                    {"role": "system", "content": chunk_system},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max(1000, min(self._env_int("OUROBOROS_REVIEW_CHUNK_MAX_TOKENS", 3200), 6000)),
            )
            usage_total = self._merge_usage_totals(usage_total, usage)
            chunk_reports.append(text or f"(empty chunk report {idx})")
            llm_trace["assistant_notes"] = self._dedupe_keep_order(
                list(llm_trace.get("assistant_notes") or []) + [truncate_for_log(text or "", 320)],
                max_items=30,
            )
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "system_review_chunk_done",
                    "task_id": task.get("id"),
                    "chunk_idx": idx,
                    "chunk_total": len(chunks),
                    "usage_cost": usage.get("cost") if isinstance(usage, dict) else None,
                },
            )
            self._emit_progress(f"Review chunk {idx}/{len(chunks)} завершен.")

        if len(chunk_reports) > 1:
            synthesis_prompt = (
                "You are consolidating multi-chunk system review results.\n"
                "Return final report with sections:\n"
                "A) Overall verdict\n"
                "B) Top 5 systemic issues\n"
                "C) Drift/hanging risk assessment\n"
                "D) Concrete roadmap (next 5 actions)\n"
                "E) Evolution adjustment guidance\n\n"
                "Chunk findings:\n\n"
                + "\n\n---\n\n".join(chunk_reports)
            )
            final_report, usage = self._review_chat_completion(
                model=model,
                effort=effort,
                messages=[
                    {"role": "system", "content": "Consolidate findings into one actionable system review report."},
                    {"role": "user", "content": synthesis_prompt},
                ],
                max_tokens=max(1200, min(self._env_int("OUROBOROS_REVIEW_FINAL_MAX_TOKENS", 4200), 8000)),
            )
            usage_total = self._merge_usage_totals(usage_total, usage)
        else:
            final_report = chunk_reports[0] if chunk_reports else "(empty review report)"

        total_cost = float(usage_total.get("cost") or 0.0)
        final_text = (
            "System deep review completed.\n"
            f"Input estimate: ~{total_tokens_est} tokens\n"
            f"Model: {model}\n"
            f"Reasoning effort: {effort}\n"
            f"Chunks: {len(chunks)}\n"
            f"Files reviewed: {int(stats.get('files') or 0)} "
            f"(truncated={int(stats.get('truncated_files') or 0)}, dropped_by_cap={int(stats.get('dropped_by_total_cap') or 0)})\n"
            f"Final review cost: ${total_cost:.4f}\n\n"
            + (final_report or "(empty)")
        )
        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "system_review_completed",
                "task_id": task.get("id"),
                "model": model,
                "reasoning_effort": effort,
                "chunks": len(chunks),
                "input_tokens_est": total_tokens_est,
                "usage": usage_total,
            },
        )
        return final_text, usage_total, llm_trace

    def _handle_review_task_direct(
        self, task: Dict[str, Any], start_time: float, drive_logs: pathlib.Path
    ) -> List[Dict[str, Any]]:
        text = ""
        usage: Dict[str, Any] = {}
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
        try:
            text, usage, llm_trace = self._run_system_review(task)
        except Exception as e:
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "review_task_error",
                    "task_id": task.get("id"),
                    "error": repr(e),
                    "traceback": truncate_for_log(traceback.format_exc(), 2500),
                },
            )
            text = f"⚠️ REVIEW_TASK_ERROR: {type(e).__name__}: {e}"

        if usage:
            self._pending_events.append(
                {
                    "type": "llm_usage",
                    "task_id": task.get("id"),
                    "provider": "openrouter",
                    "usage": usage,
                    "source": "system_deep_review",
                    "ts": utc_now_iso(),
                }
            )

        self._update_memory_after_task(task=task, final_text=text or "", llm_trace=llm_trace)
        self._pending_events.append(
            {
                "type": "send_message",
                "chat_id": task["chat_id"],
                "text": self._strip_markdown(text) if text else "\u200b",
                "log_text": text or "",
                "task_id": task.get("id"),
                "ts": utc_now_iso(),
            }
        )
        duration_sec = round(time.time() - start_time, 3)
        append_jsonl(
            drive_logs / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "task_eval",
                "ok": True,
                "task_id": task.get("id"),
                "task_type": task.get("type"),
                "duration_sec": duration_sec,
                "tool_calls": 0,
                "tool_errors": 0,
                "direct_send_attempted": False,
                "direct_send_ok": False,
                "direct_send_parts": 0,
                "direct_send_status": "review_direct",
                "response_len": len(text) if isinstance(text, str) else 0,
                "response_sha256": sha256_text(text) if isinstance(text, str) and text else "",
            },
        )
        self._emit_task_metrics_event(task=task, duration_sec=duration_sec, llm_trace=llm_trace, force_review=False)
        self._pending_events.append({"type": "task_done", "task_id": task.get("id"), "ts": utc_now_iso()})
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_done", "task_id": task.get("id")})
        return list(self._pending_events)

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        start_time = time.time()
        self._pending_events = []
        self._current_chat_id = int(task.get("chat_id") or 0) or None
        self._current_task_type = str(task.get("type") or "")
        self._last_push_succeeded = False

        drive_logs = self.env.drive_path("logs")
        sanitized_task = _sanitize_task_for_event(task, drive_logs)
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": sanitized_task})

        # Telegram typing indicator (best-effort).
        # Note: we can't show typing at the exact moment of message receipt (handled by supervisor),
        # but we can show it as soon as a worker starts processing the task.
        typing_stop: Optional[threading.Event] = None
        heartbeat_stop: Optional[threading.Event] = self._start_task_heartbeat_loop(str(task.get("id") or ""))
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
            if str(task.get("type") or "") == "review":
                return self._handle_review_task_direct(task=task, start_time=start_time, drive_logs=drive_logs)

            # --- Load context (resilient: errors produce fallbacks, not crashes) ---
            _fallback_prompt = (
                "You are Ouroboros. Your base prompt could not be loaded. "
                "Analyze available context, help the owner, and report the loading issue."
            )
            base_prompt = self._safe_read(self.env.repo_path("prompts/SYSTEM.md"), fallback=_fallback_prompt)
            world_md = self._safe_read(self.env.repo_path("BIBLE.md"))
            readme_md = self._safe_read(self.env.repo_path("README.md"))
            notes_md = self._safe_read(self.env.drive_path("NOTES.md"))
            state_json = self._safe_read(self.env.drive_path("state/state.json"), fallback="{}")
            index_summaries = self._safe_read(self.env.drive_path("index/summaries.json"))
            self._ensure_memory_files()
            scratchpad_raw = self._safe_read(self._scratchpad_path(), fallback=self._default_scratchpad())
            identity_raw = self._safe_read(self._identity_path(), fallback=self._default_identity())

            chat_lines = max(80, min(self._env_int("OUROBOROS_CONTEXT_CHAT_LINES", 800), 6000))
            artifact_lines = max(60, min(self._env_int("OUROBOROS_CONTEXT_ARTIFACT_LINES", 600), 6000))
            chat_chars = max(20000, min(self._env_int("OUROBOROS_CONTEXT_CHAT_CHARS", 280000), 1000000))
            artifact_chars = max(10000, min(self._env_int("OUROBOROS_CONTEXT_ARTIFACT_CHARS", 220000), 900000))
            scratchpad_chars = max(5000, min(self._env_int("OUROBOROS_CONTEXT_SCRATCHPAD_CHARS", 90000), 400000))
            identity_chars = max(5000, min(self._env_int("OUROBOROS_CONTEXT_IDENTITY_CHARS", 80000), 400000))
            world_chars = max(8000, min(self._env_int("OUROBOROS_CONTEXT_WORLD_CHARS", 180000), 600000))
            readme_chars = max(8000, min(self._env_int("OUROBOROS_CONTEXT_README_CHARS", 180000), 600000))
            notes_chars = max(6000, min(self._env_int("OUROBOROS_CONTEXT_NOTES_CHARS", 120000), 500000))
            state_chars = max(4000, min(self._env_int("OUROBOROS_CONTEXT_STATE_CHARS", 90000), 400000))
            index_chars = max(4000, min(self._env_int("OUROBOROS_CONTEXT_INDEX_CHARS", 90000), 400000))
            input_soft_cap_tokens = max(
                50000,
                min(self._env_int("OUROBOROS_CONTEXT_INPUT_TOKEN_SOFT_CAP", 200000), 350000),
            )

            scratchpad_ctx = self._clip_text(scratchpad_raw, max_chars=scratchpad_chars)
            identity_ctx = self._clip_text(identity_raw, max_chars=identity_chars)
            world_ctx = self._clip_text(world_md, max_chars=world_chars)
            readme_ctx = self._clip_text(readme_md, max_chars=readme_chars)
            notes_ctx = self._clip_text(notes_md, max_chars=notes_chars)
            state_ctx = self._clip_text(state_json, max_chars=state_chars)
            index_ctx = self._clip_text(index_summaries, max_chars=index_chars)

            # Use log summarization by default; allow explicit raw-tail fallback via env=0.
            summarize_logs = self._env_bool("OUROBOROS_CONTEXT_SUMMARIZE_LOGS", True)

            if summarize_logs:
                # Load and summarize JSONL tails
                chat_entries = self._read_jsonl_tail(self.env.drive_path("logs/chat.jsonl"), chat_lines)
                chat_log_recent = self._summarize_chat_jsonl(chat_entries)
                if not chat_log_recent:
                    chat_log_recent = self._safe_tail(
                        self.env.drive_path("logs/chat.jsonl"), max_lines=chat_lines, max_chars=chat_chars
                    )

                narration_entries = self._read_jsonl_tail(self.env.drive_path("logs/narration.jsonl"), artifact_lines)
                narration_context = self._summarize_narration_jsonl(narration_entries)
                if not narration_context:
                    narration_context = self._safe_tail(
                        self.env.drive_path("logs/narration.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                    )

                tools_entries = self._read_jsonl_tail(self.env.drive_path("logs/tools.jsonl"), artifact_lines)
                tools_recent = self._summarize_tools_jsonl(tools_entries)
                if not tools_recent:
                    tools_recent = self._safe_tail(
                        self.env.drive_path("logs/tools.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                    )

                events_entries = self._read_jsonl_tail(self.env.drive_path("logs/events.jsonl"), artifact_lines)
                events_recent = self._summarize_events_jsonl(events_entries)
                if not events_recent:
                    events_recent = self._safe_tail(
                        self.env.drive_path("logs/events.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                    )

                supervisor_entries = self._read_jsonl_tail(self.env.drive_path("logs/supervisor.jsonl"), artifact_lines)
                supervisor_recent = self._summarize_supervisor_jsonl(supervisor_entries)
                if not supervisor_recent:
                    supervisor_recent = self._safe_tail(
                        self.env.drive_path("logs/supervisor.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                    )
            else:
                # Raw tails (original behavior)
                chat_log_recent = self._safe_tail(
                    self.env.drive_path("logs/chat.jsonl"), max_lines=chat_lines, max_chars=chat_chars
                )
                narration_context = self._safe_tail(
                    self.env.drive_path("logs/narration.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                )
                tools_recent = self._safe_tail(
                    self.env.drive_path("logs/tools.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                )
                events_recent = self._safe_tail(
                    self.env.drive_path("logs/events.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                )
                supervisor_recent = self._safe_tail(
                    self.env.drive_path("logs/supervisor.jsonl"), max_lines=artifact_lines, max_chars=artifact_chars
                )

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
                "context_policy": "full_context_first",
                "context_soft_cap_tokens": input_soft_cap_tokens,
                "context_logs_summarized": bool(summarize_logs),
            }
            if ctx_warnings:
                runtime_ctx["context_loading_warnings"] = ctx_warnings

            messages: List[Dict[str, Any]] = [
                {"role": "system", "content": base_prompt},
                {"role": "system", "content": "## BIBLE.md\n\n" + world_ctx},
                {"role": "system", "content": "## README.md\n\n" + readme_ctx},
                {"role": "system", "content": "## Drive state (state/state.json)\n\n" + state_ctx},
                {"role": "system", "content": "## NOTES.md (Drive)\n\n" + notes_ctx},
                {"role": "system", "content": "## Working scratchpad (Drive: memory/scratchpad.md)\n\n" + scratchpad_ctx},
                {"role": "system", "content": "## Self-model identity (Drive: memory/identity.md)\n\n" + identity_ctx},
                {"role": "system", "content": "## Index summaries (Drive: index/summaries.json)\n\n" + index_ctx},
                {"role": "system", "content": "## Runtime context (JSON)\n\n" + json.dumps(runtime_ctx, ensure_ascii=False, indent=2)},
            ]
            if chat_log_recent:
                messages.append({"role": "system", "content": "## Recent chat log tail (Drive: logs/chat.jsonl)\n\n" + chat_log_recent})
            if narration_context:
                messages.append({"role": "system", "content": "## Recent narration tail (Drive: logs/narration.jsonl)\n\n" + narration_context})
            if tools_recent:
                messages.append({"role": "system", "content": "## Recent tools tail (Drive: logs/tools.jsonl)\n\n" + tools_recent})
            if events_recent:
                messages.append({"role": "system", "content": "## Recent events tail (Drive: logs/events.jsonl)\n\n" + events_recent})
            if supervisor_recent:
                messages.append(
                    {"role": "system", "content": "## Recent supervisor tail (Drive: logs/supervisor.jsonl)\n\n" + supervisor_recent}
                )
            messages.append({"role": "user", "content": task.get("text", "")})

            messages, cap_info = self._apply_message_token_soft_cap(messages, input_soft_cap_tokens)
            if cap_info.get("trimmed_sections"):
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "context_soft_cap_trim",
                        "task_id": task.get("id"),
                        "soft_cap_tokens": cap_info.get("soft_cap_tokens"),
                        "estimated_tokens_before": cap_info.get("estimated_tokens_before"),
                        "estimated_tokens_after": cap_info.get("estimated_tokens_after"),
                        "trimmed_sections": cap_info.get("trimmed_sections"),
                    },
                )

            tools = self._tools_schema()

            usage: Dict[str, Any] = {}
            llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}
            try:
                text, usage, llm_trace = self._llm_with_tools(
                    messages=messages,
                    tools=tools,
                    task_type=str(task.get("type") or ""),
                    task_text=str(task.get("text") or ""),
                )
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
                # Best-effort task_eval event (exception path)
                try:
                    duration_sec = round(time.time() - start_time, 3)
                    tool_calls_count = len(llm_trace.get("tool_calls", [])) if isinstance(llm_trace, dict) else 0
                    tool_errors_count = sum(
                        1 for tc in llm_trace.get("tool_calls", []) if isinstance(tc, dict) and tc.get("is_error")
                    ) if isinstance(llm_trace, dict) else 0
                    response_len = len(text) if isinstance(text, str) else 0
                    response_sha256 = sha256_text(text) if isinstance(text, str) and text else ""
                    append_jsonl(
                        drive_logs / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "task_eval",
                            "ok": False,
                            "task_id": task.get("id"),
                            "task_type": task.get("type"),
                            "duration_sec": duration_sec,
                            "tool_calls": tool_calls_count,
                            "tool_errors": tool_errors_count,
                            "response_len": response_len,
                            "response_sha256": response_sha256,
                            "direct_send_attempted": False,
                            "direct_send_ok": False,
                            "direct_send_parts": 0,
                            "direct_send_status": "",
                        },
                    )
                except Exception:
                    pass  # Never fail on eval emission

            # Detect empty model response (successful call but no text)
            # Also detect "visually empty" responses (Markdown-only / formatting-only)
            visible = self._strip_markdown(text) if isinstance(text, str) else ""
            is_truly_empty = not isinstance(text, str) or not text.strip()
            is_visually_empty = not visible.strip()

            if is_truly_empty or is_visually_empty:
                had_tools = len(llm_trace.get("tool_calls", [])) > 0
                tool_calls_count = len(llm_trace.get("tool_calls", []))

                if is_truly_empty:
                    # Original empty response case
                    append_jsonl(
                        drive_logs / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "empty_model_response",
                            "task_id": task.get("id"),
                            "task_type": task.get("type"),
                            "had_tools": had_tools,
                            "tool_calls": tool_calls_count,
                        },
                    )
                else:
                    # Markdown-only response case (visible content is empty)
                    append_jsonl(
                        drive_logs / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "empty_model_visible_text",
                            "task_id": task.get("id"),
                            "task_type": task.get("type"),
                            "had_tools": had_tools,
                            "tool_calls": tool_calls_count,
                            "text_len": len(text),
                            "visible_len": len(visible),
                        },
                    )

                text = "⚠️ Модель вернула пустой ответ. Попробуй переформулировать запрос или повторить."

            self._pending_events.append(
                {
                    "type": "llm_usage",
                    "task_id": task.get("id"),
                    "provider": "openrouter",
                    "usage": usage,
                    "ts": utc_now_iso(),
                }
            )

            # Memory updates are best-effort and must never block the main answer.
            self._update_memory_after_task(task=task, final_text=text or "", llm_trace=llm_trace)

            # Telegram formatting: render Markdown -> Telegram HTML directly from the worker (best-effort).
            # Rationale: supervisor currently sends plain text; parse_mode is not guaranteed there.
            direct_sent = False
            direct_send_attempted = False
            direct_send_parts = 0
            direct_send_status = ""
            if os.environ.get("OUROBOROS_TG_MARKDOWN", "1").lower() not in ("0", "false", "no", "off", ""):
                try:
                    direct_send_attempted = True
                    chat_id_int = int(task["chat_id"])
                    md_chunks = [
                        c
                        for c in self._chunk_markdown_for_telegram(text or "", max_chars=3200)
                        if isinstance(c, str) and c.strip()
                    ]
                    direct_send_parts = len(md_chunks)
                    all_ok = bool(md_chunks)
                    last_status = "ok" if md_chunks else "empty_chunks"
                    plain_fallback_count = 0

                    for md_part in md_chunks:
                        html_text = self._markdown_to_telegram_html(md_part)
                        ok, status = self._telegram_send_message_html(chat_id_int, html_text)
                        last_status = status
                        if ok:
                            continue

                        plain_text = self._strip_markdown(md_part)
                        if not plain_text.strip():
                            all_ok = False
                            last_status = "plain_empty"
                            break

                        ok_plain, status_plain = self._telegram_send_message_plain(chat_id_int, plain_text)
                        last_status = status_plain
                        if ok_plain:
                            plain_fallback_count += 1
                            continue

                        all_ok = False
                        break

                    direct_sent = all_ok
                    direct_send_status = last_status
                    append_jsonl(
                        drive_logs / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "telegram_send_direct",
                            "task_id": task.get("id"),
                            "chat_id": chat_id_int,
                            "ok": direct_sent,
                            "status": last_status,
                            "parts": direct_send_parts,
                            "plain_fallback_count": plain_fallback_count,
                        },
                    )
                except Exception as e:
                    direct_send_attempted = True
                    direct_send_parts = 0
                    direct_send_status = "exc"
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

            # Ensure text_for_supervisor is never empty (Telegram rejects empty messages)
            if not isinstance(text_for_supervisor, str) or not text_for_supervisor.strip():
                text_for_supervisor = "\u200b"

            self._pending_events.append(
                {
                    "type": "send_message",
                    "chat_id": task["chat_id"],
                    "text": text_for_supervisor,
                    "log_text": text or "",
                    "task_id": task.get("id"),
                    "ts": utc_now_iso(),
                }
            )

            # Success-path task_eval event (best-effort, never raise)
            duration_sec = round(time.time() - start_time, 3)
            try:
                append_jsonl(
                    drive_logs / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "task_eval",
                        "ok": True,
                        "task_id": task.get("id"),
                        "task_type": task.get("type"),
                        "duration_sec": duration_sec,
                        "tool_calls": len(llm_trace.get("tool_calls", [])) if isinstance(llm_trace, dict) else 0,
                        "tool_errors": sum(
                            1 for tc in llm_trace.get("tool_calls", []) if isinstance(tc, dict) and tc.get("is_error")
                        ) if isinstance(llm_trace, dict) else 0,
                        "direct_send_attempted": bool(direct_send_attempted),
                        "direct_send_ok": bool(direct_sent),
                        "direct_send_parts": int(direct_send_parts) if direct_send_attempted else 0,
                        "direct_send_status": str(direct_send_status) if direct_send_attempted else "",
                        "response_len": len(text) if isinstance(text, str) else 0,
                        "response_sha256": sha256_text(text) if isinstance(text, str) and text else "",
                    },
                )
            except Exception:
                pass  # Never fail on eval emission

            self._emit_task_metrics_event(task=task, duration_sec=duration_sec, llm_trace=llm_trace, force_review=False)
            self._pending_events.append({"type": "task_done", "task_id": task.get("id"), "ts": utc_now_iso()})
            append_jsonl(
                drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_done", "task_id": task.get("id")}
            )
            return list(self._pending_events)
        finally:
            if typing_stop is not None:
                typing_stop.set()
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            self._emit_task_heartbeat(task_id=str(task.get("id") or ""), phase="done")
            self._current_task_type = None

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
            # quote=False: no attributes, avoid &#x27;/&quot; entities that Telegram may reject
            code_esc = html.escape(code, quote=False)
            parts.append(f"<pre><code>{code_esc}</code></pre>")
            last = m.end()
        parts.append(md[last:])

        def _render_span(text: str) -> str:
            # Inline code first
            out: list[str] = []
            pos = 0
            for mm in inline_code_re.finditer(text):
                # quote=False: no attributes, avoid &#x27;/&quot; entities that Telegram may reject
                out.append(html.escape(text[pos : mm.start()], quote=False))
                out.append(f"<code>{html.escape(mm.group(1), quote=False)}</code>")
                pos = mm.end()
            out.append(html.escape(text[pos:], quote=False))
            s = "".join(out)
            # Bold
            s = bold_re.sub(r"<b>\1</b>", s)
            return s

        return "".join(_render_span(p) if not p.startswith("<pre><code>") else p for p in parts)

    @staticmethod
    def _tg_utf16_len(text: str) -> int:
        """Compute Telegram character length in UTF-16 code units.

        Telegram measures text length in UTF-16 code units (not Python len()).
        Codepoints > 0xFFFF (astral plane, emoji, etc.) count as 2 units.
        Surrogates count as 1 unit each.
        """
        if not text:
            return 0
        try:
            count = 0
            for c in text:
                cp = ord(c)
                if cp > 0xFFFF:
                    count += 2
                else:
                    count += 1
            return count
        except Exception:
            return 0

    @staticmethod
    def _slice_by_utf16_units(text: str, max_units: int) -> str:
        """Return prefix of text with UTF-16 length <= max_units.

        Slices string by UTF-16 code units without exceeding limit.
        """
        if not text or max_units <= 0:
            return ""
        try:
            count = 0
            idx = 0
            for c in text:
                cp = ord(c)
                units = 2 if cp > 0xFFFF else 1
                if count + units > max_units:
                    break
                count += units
                idx += 1
            return text[:idx]
        except Exception:
            return text

    @staticmethod
    def _chunk_markdown_for_telegram(md: str, max_chars: int = 3500) -> List[str]:
        """Split Markdown into chunks safe for Telegram.

        We chunk the *Markdown* (not HTML) to avoid breaking HTML tags/entities,
        then render each chunk to HTML.

        Behavior:
        - tries to preserve fenced code blocks (```...```) by closing/reopening fences
          when splitting inside a fence.
        - hard-splits very long lines if needed.

        Note: max_chars is measured in UTF-16 code units (Telegram's limit).
        """
        md = md or ""
        try:
            max_chars_i = int(max_chars)
        except Exception:
            max_chars_i = 3500
        max_chars_i = max(256, min(4096, max_chars_i))

        lines = md.splitlines(keepends=True)
        chunks: List[str] = []
        cur = ""
        in_fence = False
        fence_open_line = "```\n"

        def _flush() -> None:
            nonlocal cur
            if cur and cur.strip():
                chunks.append(cur)
            cur = ""

        def _append_piece(piece: str) -> None:
            nonlocal cur
            # When inside a fence, reserve room for closing fence at end of chunk.
            fence_close = "```\n"
            reserve = OuroborosAgent._tg_utf16_len(fence_close) if in_fence else 0
            effective_limit = max_chars_i - reserve

            if OuroborosAgent._tg_utf16_len(cur) + OuroborosAgent._tg_utf16_len(piece) <= effective_limit:
                cur += piece
                return

            # If splitting while in a fence, close the fence before flushing.
            if in_fence and cur:
                # cur was kept <= (max_chars_i - len(fence_close)), so this fits.
                cur += fence_close
                _flush()
                cur = fence_open_line

            # Hard split remaining piece.
            s = piece
            while s:
                reserve2 = OuroborosAgent._tg_utf16_len(fence_close) if in_fence else 0
                effective_limit2 = max_chars_i - reserve2
                space = effective_limit2 - OuroborosAgent._tg_utf16_len(cur)
                if space <= 0:
                    if in_fence and cur and not cur.rstrip().endswith("```"):
                        if OuroborosAgent._tg_utf16_len(cur) <= max_chars_i - OuroborosAgent._tg_utf16_len(fence_close):
                            cur += fence_close
                    _flush()
                    cur = fence_open_line if in_fence else ""
                    reserve3 = OuroborosAgent._tg_utf16_len(fence_close) if in_fence else 0
                    effective_limit3 = max_chars_i - reserve3
                    space = effective_limit3 - OuroborosAgent._tg_utf16_len(cur)
                take = OuroborosAgent._slice_by_utf16_units(s, space)
                cur += take
                s = s[len(take) :]

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                if not in_fence:
                    in_fence = True
                    fence_open_line = line if line.endswith("\n") else (line + "\n")
                else:
                    in_fence = False
            _append_piece(line)

        if in_fence:
            _append_piece("```\n")
            in_fence = False

        _flush()
        return chunks or [md]

    @staticmethod
    def _sanitize_telegram_text(text: str) -> str:
        """Sanitize text for Telegram to avoid HTML parse failures.

        Telegram HTML parse sometimes fails on hidden control characters
        and invalid Unicode surrogates; sanitizing reduces "can't parse
        entities" and encoding errors.
        """
        if text is None:
            return ""
        # Normalize newlines: convert \r\n to \n, drop standalone \r
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # Remove ASCII control chars (codepoints < 32) except \n and \t,
        # and remove invalid Unicode surrogates (U+D800..U+DFFF)
        text = "".join(
            c for c in text
            if (ord(c) >= 32 or c in ("\n", "\t")) and not (0xD800 <= ord(c) <= 0xDFFF)
        )
        return text

    def _telegram_send_message_html(self, chat_id: int, html_text: str) -> tuple[bool, str]:
        """Send formatted message via Telegram sendMessage(parse_mode=HTML)."""
        # Sanitize to avoid HTML parse failures from control characters
        html_text = self._sanitize_telegram_text(html_text)
        return self._telegram_api_post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": html_text,
                "parse_mode": "HTML",
                "disable_web_page_preview": "1",
            },
        )

    def _telegram_send_message_plain(self, chat_id: int, text: str) -> tuple[bool, str]:
        """Send plain text message via Telegram sendMessage (no parse_mode)."""
        # Sanitize to avoid parse failures from control characters
        text = self._sanitize_telegram_text(text)
        return self._telegram_api_post(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": "1",
            },
        )

    @staticmethod
    def _chunk_plain_text(text: str, max_chars: int = 3500) -> List[str]:
        """Split plain text into chunks safe for Telegram.

        Simple chunking that splits on newlines when possible.

        Note: max_chars is measured in UTF-16 code units (Telegram's limit).
        """
        text = text or ""
        try:
            max_chars_i = int(max_chars)
        except Exception:
            max_chars_i = 3500
        max_chars_i = max(256, min(4096, max_chars_i))

        if OuroborosAgent._tg_utf16_len(text) <= max_chars_i:
            return [text] if text else []

        chunks: List[str] = []
        lines = text.splitlines(keepends=True)
        cur = ""

        for line in lines:
            # If single line is too long, hard split it
            line_len = OuroborosAgent._tg_utf16_len(line)
            if line_len > max_chars_i:
                if cur:
                    chunks.append(cur)
                    cur = ""
                # Hard split the long line
                while line:
                    chunk = OuroborosAgent._slice_by_utf16_units(line, max_chars_i)
                    chunks.append(chunk)
                    line = line[len(chunk):]
                continue

            # If adding this line would exceed limit, flush current chunk
            if OuroborosAgent._tg_utf16_len(cur) + line_len > max_chars_i:
                if cur:
                    chunks.append(cur)
                cur = line
            else:
                cur += line

        if cur:
            chunks.append(cur)

        return chunks or [text]

    def _telegram_send_voice(self, chat_id: int, ogg_bytes: bytes, caption: str = "") -> tuple[bool, str]:
        """Send a Telegram voice note (OGG/OPUS) via sendVoice.

        Returns: (ok, status)
          - status: "ok" | "no_token" | "error"
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False, "no_token"

        try:
            import requests  # lazy import
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "telegram_api_error", "method": "sendVoice", "error": f"requests_import: {repr(e)}"},
            )
            return False, "error"

        url = f"https://api.telegram.org/bot{token}/sendVoice"
        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        files = {"voice": ("voice.ogg", ogg_bytes, "audio/ogg")}

        try:
            r = requests.post(url, data=data, files=files, timeout=60)
            try:
                j = r.json()
                ok = bool(j.get("ok"))
            except Exception:
                ok = bool(r.ok)
            return (ok, "ok" if ok else "error")
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "telegram_api_error", "method": "sendVoice", "error": repr(e)},
            )
            return False, "error"

    def _telegram_send_photo(
        self,
        chat_id: int,
        photo_bytes: bytes,
        caption: str = "",
        filename: str = "image.png",
        mime: str = "image/png",
    ) -> tuple[bool, str]:
        """Send a Telegram photo via sendPhoto.

        Returns: (ok, status)
          - status: "ok" | "no_token" | "error"
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False, "no_token"

        try:
            import requests  # lazy import
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "telegram_api_error",
                    "method": "sendPhoto",
                    "error": f"requests_import: {repr(e)}",
                },
            )
            return False, "error"

        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        data: Dict[str, Any] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption
        files = {"photo": (filename or "image.png", photo_bytes, mime or "image/png")}

        try:
            r = requests.post(url, data=data, files=files, timeout=60)
            try:
                j = r.json()
                ok = bool(j.get("ok"))
            except Exception:
                ok = bool(r.ok)
            return (ok, "ok" if ok else "error")
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "telegram_api_error", "method": "sendPhoto", "error": repr(e)},
            )
            return False, "error"

    def _tts_to_ogg_opus(self, text: str, voice: str = "kal") -> bytes:
        """Local TTS: ffmpeg flite -> OGG/OPUS bytes.

        No external APIs. Requires ffmpeg with libflite filter.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("TTS text must be non-empty")

        tmp_dir = pathlib.Path("/tmp")
        tmp_dir.mkdir(parents=True, exist_ok=True)
        h = sha256_text(text)[:10]
        txt_path = tmp_dir / f"tts_{h}.txt"
        ogg_path = tmp_dir / f"tts_{h}.ogg"
        txt_path.write_text(text, encoding="utf-8")

        cmd = [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"flite=textfile={txt_path}:voice={voice}",
            "-ac",
            "1",
            "-ar",
            "48000",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            str(ogg_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0 or not ogg_path.exists():
            raise RuntimeError(
                "TTS synthesis failed via ffmpeg/flite. "
                f"Return code={res.returncode}. STDERR={truncate_for_log(res.stderr, 1500)}"
            )
        return ogg_path.read_bytes()

    def _tts_to_ogg_opus_openai(
        self,
        text: str,
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        format: str = "opus",
    ) -> bytes:
        """Cloud TTS via OpenAI: POST /v1/audio/speech -> audio bytes.

        We return raw bytes (typically OPUS-in-OGG when format='opus').
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("TTS text must be non-empty")

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set")

        try:
            import requests  # lazy import
        except Exception as e:
            raise RuntimeError(f"requests import failed: {repr(e)}")

        url = "https://api.openai.com/v1/audio/speech"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
            "model": model or "gpt-4o-mini-tts",
            "voice": voice or "alloy",
            "input": text,
            "format": format or "opus",
        }
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if not r.ok:
            # Do not log full body (may include internal error details). Keep it short.
            raise RuntimeError(f"OpenAI TTS failed: HTTP {r.status_code}: {truncate_for_log(r.text, 500)}")
        return r.content

    def _telegram_api_post(self, method: str, data: Dict[str, Any]) -> Tuple[bool, str]:
        """Best-effort Telegram Bot API call.

        We intentionally do not log request URLs or payloads verbatim to avoid any chance of leaking secrets.

        Returns: (ok, status)
          - ok: True if request succeeded
          - status: "ok" | "no_token" | "http_<code>: <description>" | "exc_<Type>: <message>"
        """
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            return False, "no_token"

        url = f"https://api.telegram.org/bot{token}/{method}"
        payload = urllib.parse.urlencode({k: str(v) for k, v in data.items()}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read()

            # Telegram may return HTTP 200 with {"ok": false, ...}.
            # If we don't parse it, direct-send will be treated as success and fallback won't trigger.
            try:
                j = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(j, dict) and ("ok" in j):
                    ok = bool(j.get("ok"))
                    if ok:
                        return True, "ok"
                    desc = str(j.get("description", ""))
                    if desc:
                        status_msg = truncate_for_log(f"tg_ok_false: {desc}", 300)
                        error_msg = truncate_for_log(f"ok_false: {desc}", 300)
                    else:
                        status_msg = "tg_ok_false"
                        error_msg = "ok_false"
                    append_jsonl(
                        self.env.drive_path("logs") / "events.jsonl",
                        {"ts": utc_now_iso(), "type": "telegram_api_error", "method": method, "error": error_msg},
                    )
                    return False, status_msg
            except Exception:
                pass

            return True, "ok"
        except urllib.error.HTTPError as e:
            # Parse Telegram error response from HTTP error body
            status_msg = f"http_{e.code}"
            try:
                body = e.read()
                j = json.loads(body.decode("utf-8", errors="replace"))
                if isinstance(j, dict) and "description" in j:
                    desc = str(j["description"])
                    status_msg = truncate_for_log(f"http_{e.code}: {desc}", 300)
            except Exception:
                pass
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "telegram_api_error", "method": method, "error": repr(e)},
            )
            return False, status_msg
        except Exception as e:
            # Generic exception with type and message
            exc_type = type(e).__name__
            exc_msg = str(e)
            status_msg = truncate_for_log(f"exc_{exc_type}: {exc_msg}", 300)
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "telegram_api_error", "method": method, "error": repr(e)},
            )
            return False, status_msg

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

    @staticmethod
    def _extract_base64_image_payload(s: str) -> str:
        """Extract base64 payload from either raw base64 or data URL.

        OpenRouter /responses for image models may return:
          - raw base64 png bytes
          - or a data URL like: data:image/png;base64,AAAA...
        """
        s = (s or "").strip()
        if not s:
            return ""
        if s.startswith("data:"):
            # data:image/png;base64,....
            comma = s.find(",")
            if comma >= 0:
                return s[comma + 1 :].strip()
        return s

    @staticmethod
    def _b64decode_robust(b64: str) -> bytes:
        """Decode base64 with best-effort fixes (whitespace + padding).

        Some providers return data URLs or omit proper padding.
        """
        b64 = re.sub(r"\s+", "", (b64 or ""))
        if not b64:
            return b""
        # normalize padding
        b64 = b64.rstrip("=")
        pad = (-len(b64)) % 4
        b64 = b64 + ("=" * pad)
        return base64.b64decode(b64)

    def _openrouter_generate_image_via_curl(
        self,
        prompt: str,
        model: str = "openai/gpt-5-image",
        image_config: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 180,
    ) -> bytes:
        """Generate an image via OpenRouter /responses using CLI curl.

        Security: token is passed as a subprocess arg; never logged.
        Returns raw image bytes.
        """
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set")

        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt must be non-empty")

        if image_config is None:
            image_config = {"size": "1024x1024"}

        payload = {
            "model": model,
            "input": prompt,
            "modalities": ["image"],
            "image_config": image_config,
        }

        cmd = [
            "curl",
            "-sS",
            "-L",
            "--max-time",
            str(int(timeout_sec)),
            "-H",
            "Accept: application/json",
            "-H",
            "Content-Type: application/json",
            "-H",
            f"Authorization: Bearer {api_key}",
            "-H",
            "HTTP-Referer: https://colab.research.google.com/",
            "-H",
            "X-Title: Ouroboros",
            "https://openrouter.ai/api/v1/responses",
            "--data-binary",
            json.dumps(payload, ensure_ascii=False),
        ]

        # shell=False, capture_output: do not print token
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                "OpenRouter image curl failed. "
                f"Return code={res.returncode}. STDERR={truncate_for_log(res.stderr, 1500)}"
            )

        raw = (res.stdout or "").strip()
        try:
            d = json.loads(raw)
        except Exception as e:
            raise RuntimeError(
                "OpenRouter returned non-JSON for /responses. "
                f"Error={type(e).__name__}: {e}. Body head={truncate_for_log(raw, 300)}"
            )

        output = d.get("output") or []
        img_item = None
        for it in output:
            if isinstance(it, dict) and it.get("type") == "image_generation_call":
                img_item = it
                if (it.get("status") or "").lower() == "completed":
                    break

        if not img_item:
            raise RuntimeError("OpenRouter /responses did not return image_generation_call")

        result = img_item.get("result")
        if not isinstance(result, str) or not result.strip():
            raise RuntimeError(f"Image result is missing. status={img_item.get('status')}")

        b64 = self._extract_base64_image_payload(result)
        img_bytes = self._b64decode_robust(b64)
        if not img_bytes:
            raise RuntimeError("Decoded image bytes are empty")

        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {"ts": utc_now_iso(), "type": "openrouter_image_generated", "model": model, "bytes": len(img_bytes)},
        )
        return img_bytes

    def _llm_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        task_type: str = "",
        task_text: str = "",
    ) -> Tuple[str, Dict[str, Any], Dict[str, Any]]:
        client = self._openrouter_client()
        drive_logs = self.env.drive_path("logs")

        base_profile = self._select_task_profile(task_type=task_type, task_text=task_text)
        active_profile = base_profile
        profile_cfg = self._model_profile(active_profile)
        active_model = profile_cfg["model"]
        active_effort = profile_cfg["effort"]

        tool_name_to_fn = {
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
            "telegram_send_voice": self._tool_telegram_send_voice,
        }
        code_tools = {"repo_write_commit", "repo_commit_push", "git_status", "git_diff", "run_shell", "claude_code_edit"}

        soft_check_interval = 15  # LLM soft-check каждые N раундов (по BIBLE: LLM-first)
        llm_max_retries = int(os.environ.get("OUROBOROS_LLM_MAX_RETRIES", "3"))
        last_usage: Dict[str, Any] = {}
        llm_trace: Dict[str, Any] = {"assistant_notes": [], "tool_calls": []}

        def _safe_args(v: Any) -> Any:
            try:
                return json.loads(json.dumps(v, ensure_ascii=False, default=str))
            except Exception:
                return {"_repr": repr(v)}

        def _maybe_raise_effort(target: str, reason: str) -> None:
            nonlocal active_effort
            target_effort = self._normalize_reasoning_effort(target, default=active_effort)
            if self._reasoning_rank(target_effort) <= self._reasoning_rank(active_effort):
                return
            prev_effort = active_effort
            active_effort = target_effort

        def _switch_to_code_profile(reason: str) -> None:
            nonlocal active_model, active_effort, active_profile
            code_cfg = self._model_profile("code_task")
            new_model = code_cfg["model"]
            new_effort = code_cfg["effort"]
            if new_model == active_model and self._reasoning_rank(new_effort) <= self._reasoning_rank(active_effort):
                return
            active_profile = "code_task"
            active_model = new_model
            active_effort = new_effort if self._reasoning_rank(new_effort) >= self._reasoning_rank(active_effort) else active_effort

        round_idx = 0
        while True:
            round_idx += 1

            # Soft LLM-check каждые N раундов (Принцип 1: LLM-first)
            if round_idx > 1 and round_idx % soft_check_interval == 0:
                messages.append({
                    "role": "system",
                    "content": (
                        f"[Self-check] Ты выполнил {round_idx} раундов. "
                        "Оцени свой прогресс: движешься ли к цели? "
                        "Если застрял — смени подход. Если нерешаемо — сообщи владельцу."
                    ),
                })

            # Эскалация reasoning effort при длинных задачах
            if round_idx >= 5:
                _maybe_raise_effort("high", reason="задача оказалась длиннее обычной")
            if round_idx >= 10:
                _maybe_raise_effort("xhigh", reason="длительный multi-round reasoning")

            # ---- LLM call with retry on transient errors ----
            resp_dict = None
            last_llm_error: Optional[Exception] = None

            for attempt in range(llm_max_retries):
                try:
                    resp = client.chat.completions.create(
                        model=active_model,
                        messages=messages,
                        tools=tools,
                        tool_choice="auto",
                        extra_body={"reasoning": {"effort": active_effort, "exclude": True}},
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
                            "model": active_model,
                            "reasoning_effort": active_effort,
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
                ), last_usage, llm_trace

            last_usage = resp_dict.get("usage", {}) or {}
            append_jsonl(
                drive_logs / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "llm_profile_call",
                    "round": round_idx,
                    "profile": active_profile,
                    "model": active_model,
                    "reasoning_effort": active_effort,
                    "usage_cost": (last_usage.get("cost") if isinstance(last_usage, dict) else None),
                },
            )

            choice = (resp_dict.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if tool_calls:
                messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})

                # Emit the LLM's reasoning/plan as a progress message (human-readable narration)
                has_model_progress = bool(content and content.strip())
                if has_model_progress:
                    self._emit_progress(str(content).strip())
                    llm_trace["assistant_notes"] = self._dedupe_keep_order(
                        list(llm_trace.get("assistant_notes") or []) + [str(content).strip()[:320]],
                        max_items=20,
                    )

                saw_code_tool = False
                error_count = 0

                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    if fn_name in code_tools:
                        saw_code_tool = True

                    # ---- Парсим аргументы ----
                    try:
                        args = json.loads(tc["function"]["arguments"] or "{}")
                    except (json.JSONDecodeError, ValueError) as e:
                        result = (
                            f"⚠️ TOOL_ARG_ERROR: Could not parse arguments for '{fn_name}': {e}\n"
                            f"Raw: {truncate_for_log(tc['function'].get('arguments', ''), 500)}\n"
                            f"Retry with valid JSON arguments."
                        )
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                        llm_trace["tool_calls"].append({"tool": fn_name, "args": {}, "result": truncate_for_log(result, 600), "is_error": True})
                        error_count += 1
                        continue

                    args_for_log = _sanitize_tool_args_for_log(
                        fn_name, args if isinstance(args, dict) else {}, drive_logs, tool_call_id=str(tc.get('id', ''))
                    )

                    # ---- Проверяем существование ----
                    if fn_name not in tool_name_to_fn:
                        result = f"⚠️ UNKNOWN_TOOL: '{fn_name}'. Available: {', '.join(sorted(tool_name_to_fn.keys()))}"
                        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                        llm_trace["tool_calls"].append({"tool": fn_name, "args": _safe_args(args_for_log), "result": result, "is_error": True})
                        error_count += 1
                        continue

                    # ---- Выполняем ----
                    tool_ok = True
                    try:
                        result = tool_name_to_fn[fn_name](**args)
                    except Exception as e:
                        tool_ok = False
                        tb = traceback.format_exc()
                        result = (
                            f"⚠️ TOOL_ERROR ({fn_name}): {type(e).__name__}: {e}\n\n"
                            f"Traceback:\n{truncate_for_log(tb, 2000)}\n\n"
                            f"Analyze the error and try a different approach."
                        )
                        append_jsonl(drive_logs / "events.jsonl", {
                            "ts": utc_now_iso(), "type": "tool_error", "tool": fn_name,
                            "args": args_for_log, "error": repr(e),
                        })

                    append_jsonl(drive_logs / "tools.jsonl", {
                        "ts": utc_now_iso(), "tool": fn_name, "args": args_for_log,
                        "result_preview": truncate_for_log(result, 2000),
                    })
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                    is_error = (not tool_ok) or str(result).startswith("⚠️")
                    llm_trace["tool_calls"].append({
                        "tool": fn_name, "args": _safe_args(args_for_log),
                        "result": truncate_for_log(result, 700), "is_error": is_error,
                    })
                    if is_error:
                        error_count += 1

                # LLM сама опишет ошибки в следующем content (LLM-first, без механических сообщений)
                if saw_code_tool and active_profile not in ("code_task", "evolution_task", "deep_review"):
                    _switch_to_code_profile(reason="обнаружены кодовые действия в ходе решения")
                if error_count >= 2:
                    _maybe_raise_effort("high", reason="много ошибок")
                if error_count >= 4:
                    _maybe_raise_effort("xhigh", reason="повторные ошибки")

                continue

            if content and content.strip():
                llm_trace["assistant_notes"] = self._dedupe_keep_order(
                    list(llm_trace.get("assistant_notes") or []) + [content.strip()[:320]],
                    max_items=20,
                )
            return (content or ""), last_usage, llm_trace

        # Цикл завершён (LLM вернула ответ без tool calls)
        return "", last_usage, llm_trace

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
                    "description": "Fallback path: write one deterministic UTF-8 file, then git add/commit/push to ouroboros. Prefer claude_code_edit for most code changes.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "commit_message": {"type": "string"}},
                        "required": ["path", "content", "commit_message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "repo_commit_push",
                    "description": "Commit and push already-made repo changes to ouroboros branch (without rewriting files). Required before request_restart in evolution mode.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "commit_message": {"type": "string"},
                            "paths": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["commit_message"],
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
                    "name": "claude_code_edit",
                    "description": "Preferred/default code editing engine when available: delegate edits to Anthropic Claude Code CLI (headless). Especially for multi-file changes, refactors, and uncertain edit scope. Always follow with repo_commit_push before reporting success.",
                    "parameters": {
                        "type": "object",
                        "properties": {"instruction": {"type": "string"}, "max_turns": {"type": "integer"}},
                        "required": ["instruction"],
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
                "function": {"name": "request_restart", "description": "Ask supervisor to restart Ouroboros runtime (apply new code). In evolution mode this is allowed only after successful push.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
            {
                "type": "function",
                "function": {"name": "promote_to_stable", "description": "Промоутить ouroboros → ouroboros-stable. Вызывай когда считаешь код стабильным.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
            {
                "type": "function",
                "function": {"name": "schedule_task", "description": "Запланировать фоновую задачу.", "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
            },
            {
                "type": "function",
                "function": {"name": "cancel_task", "description": "Отменить задачу по ID.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
            },
            {
                "type": "function",
                "function": {
                    "name": "chat_history",
                    "description": "Подтянуть произвольное количество сообщений из истории чата. Поддерживает поиск.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "count": {"type": "integer", "default": 100, "description": "Сколько сообщений"},
                            "offset": {"type": "integer", "default": 0, "description": "Пропустить N от конца"},
                            "search": {"type": "string", "default": "", "description": "Фильтр по тексту"},
                        },
                        "required": [],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "telegram_send_voice",
                    "description": "Send voice message via TTS (local ffmpeg/flite or OpenAI).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "chat_id": {"type": "integer"},
                            "text": {"type": "string"},
                            "caption": {"type": "string"},
                            "tts": {"type": "string", "enum": ["local", "openai"]},
                            "voice": {"type": "string"},
                            "openai_model": {"type": "string"},
                            "openai_voice": {"type": "string"},
                            "openai_format": {"type": "string"},
                        },
                        "required": ["chat_id", "text"],
                    },
                },
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
        stale_sec = int(os.environ.get("OUROBOROS_GIT_LOCK_STALE_SEC", "600"))

        while True:
            # Check for stale lock
            if lock_path.exists():
                try:
                    stat = lock_path.stat()
                    age_sec = time.time() - stat.st_mtime
                    if age_sec > stale_sec:
                        # Remove stale lock and log event
                        lock_path.unlink()
                        drive_logs = self.env.drive_path("logs")
                        append_jsonl(
                            drive_logs / "events.jsonl",
                            {
                                "ts": utc_now_iso(),
                                "type": "git_lock_stale_removed",
                                "age_sec": round(age_sec, 2),
                            },
                        )
                        continue
                except (FileNotFoundError, OSError):
                    # Lock was removed by another process, retry
                    pass

            # Atomic lock acquisition with O_CREAT | O_EXCL
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                try:
                    os.write(fd, f"locked_at={utc_now_iso()}\n".encode("utf-8"))
                finally:
                    os.close(fd)
                return lock_path
            except FileExistsError:
                # Lock held by another process, wait and retry
                time.sleep(0.5)

    def _release_git_lock(self, lock_path: pathlib.Path) -> None:
        if lock_path.exists():
            lock_path.unlink()

    def _tool_repo_write_commit(self, path: str, content: str, commit_message: str) -> str:
        self._last_push_succeeded = False
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

        self._last_push_succeeded = True
        return f"OK: committed and pushed to {self.env.branch_dev}: {commit_message}"

    def _tool_repo_commit_push(self, commit_message: str, paths: Optional[List[str]] = None) -> str:
        self._last_push_succeeded = False
        if not commit_message.strip():
            return "⚠️ ERROR: commit_message must be non-empty."

        lock = self._acquire_git_lock()
        try:
            try:
                run(["git", "checkout", self.env.branch_dev], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout {self.env.branch_dev}): {e}"

            add_cmd: List[str]
            if paths:
                try:
                    safe_paths = [safe_relpath(p) for p in paths if str(p).strip()]
                except ValueError as e:
                    return f"⚠️ PATH_ERROR: {e}"
                if not safe_paths:
                    return "⚠️ ERROR: paths is empty after validation."
                add_cmd = ["git", "add"] + safe_paths
            else:
                add_cmd = ["git", "add", "-A"]

            try:
                run(add_cmd, cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (add): {e}"

            try:
                status = run(["git", "status", "--porcelain"], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (status): {e}"
            if not status.strip():
                return "⚠️ GIT_NO_CHANGES: nothing to commit."

            try:
                run(["git", "commit", "-m", commit_message], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (commit): {e}"

            # Pull --rebase перед push (предотвращает конфликты между воркерами)
            try:
                run(["git", "pull", "--rebase", "origin", self.env.branch_dev], cwd=self.env.repo_dir)
            except Exception:
                pass  # Если не удалось — попробуем push всё равно

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

        self._last_push_succeeded = True
        return f"OK: committed existing changes and pushed to {self.env.branch_dev}: {commit_message}"

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
        if str(self._current_task_type or "") == "evolution":
            if isinstance(cmd, list) and cmd and str(cmd[0]).lower() == "git":
                return (
                    "⚠️ EVOLUTION_GIT_RESTRICTED: git shell commands are blocked in evolution mode. "
                    "Use repo_write_commit/repo_commit_push (they are pinned to branch ouroboros)."
                )

        def _is_within_repo(p: pathlib.Path) -> bool:
            try:
                p.resolve().relative_to(self.env.repo_dir.resolve())
                return True
            except Exception:
                return False

        def _normalize_cwd(raw: str) -> pathlib.Path:
            raw = (raw or "").strip()
            if not raw or raw in (".", "./"):
                return self.env.repo_dir

            # If user passed an absolute path (common LLM mistake), accept it only if it is inside repo_dir.
            if raw.startswith("/"):
                ap = pathlib.Path(raw).resolve()
                if _is_within_repo(ap) and ap.exists() and ap.is_dir():
                    return ap
                append_jsonl(
                    self.env.drive_path("logs") / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "run_shell_cwd_ignored",
                        "cwd": raw,
                        "reason": "absolute_not_within_repo_or_missing",
                    },
                )
                return self.env.repo_dir

            # Otherwise treat as repo-relative.
            try:
                rel = safe_relpath(raw)
            except Exception as e:
                append_jsonl(
                    self.env.drive_path("logs") / "events.jsonl",
                    {"ts": utc_now_iso(), "type": "run_shell_cwd_ignored", "cwd": raw, "reason": f"invalid:{type(e).__name__}"},
                )
                return self.env.repo_dir

            wd2 = (self.env.repo_dir / rel).resolve()
            if not _is_within_repo(wd2) or not wd2.exists() or not wd2.is_dir():
                append_jsonl(
                    self.env.drive_path("logs") / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "run_shell_cwd_fallback",
                        "cwd": raw,
                        "resolved": str(wd2),
                        "reason": "not_found_or_not_dir_or_escape",
                    },
                )
                return self.env.repo_dir

            return wd2

        wd = _normalize_cwd(cwd)

        try:
            res = subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return f"⚠️ Command timed out after 120s: {' '.join(cmd)}"
        except FileNotFoundError as e:
            # Some environments occasionally surface a cwd-related FileNotFoundError.
            # Retry once from repo_dir to avoid flakiness.
            if str(wd) != str(self.env.repo_dir):
                append_jsonl(
                    self.env.drive_path("logs") / "events.jsonl",
                    {
                        "ts": utc_now_iso(),
                        "type": "run_shell_retry_no_cwd",
                        "cwd": str(wd),
                        "error": truncate_for_log(repr(e), 300),
                    },
                )
                try:
                    res = subprocess.run(cmd, cwd=str(self.env.repo_dir), capture_output=True, text=True, timeout=120)
                except subprocess.TimeoutExpired:
                    return f"⚠️ Command timed out after 120s: {' '.join(cmd)}"
                except Exception as e2:
                    return f"⚠️ Failed to execute command: {type(e2).__name__}: {e2}"
            else:
                return f"⚠️ Failed to execute command: {type(e).__name__}: {e}"
        except Exception as e:
            return f"⚠️ Failed to execute command: {type(e).__name__}: {e}"
        output = (res.stdout + "\n" + res.stderr).strip()
        if res.returncode != 0:
            return (
                f"⚠️ Command exited with code {res.returncode}: {' '.join(cmd)}\n\n"
                f"STDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
            )
        return output

    def _tool_claude_code_edit(self, instruction: str, max_turns: int = 12) -> str:
        prompt = (instruction or "").strip()
        if not prompt:
            return "⚠️ ERROR: instruction must be non-empty."

        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            return "⚠️ CLAUDE_CODE_UNAVAILABLE: ANTHROPIC_API_KEY is not set."

        claude_bin = shutil.which("claude")
        if not claude_bin:
            return "⚠️ CLAUDE_CODE_UNAVAILABLE: claude CLI is not installed or not in PATH."

        try:
            turns = int(max_turns)
        except Exception:
            turns = 12
        turns = max(1, min(turns, 30))

        # NOTE: In Colab we typically run as root. Some Claude Code CLI versions
        # refuse the most permissive "skip permissions" mode under root/sudo.
        # We rely on `--permission-mode bypassPermissions` (configurable) and
        # mark the subprocess as sandboxed.
        perm_mode = os.environ.get("OUROBOROS_CLAUDE_CODE_PERMISSION_MODE", "bypassPermissions").strip() or "bypassPermissions"

        base_cmd: List[str] = [
            claude_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(turns),
            "--tools",
            "Read,Edit,Grep,Glob",
        ]

        default_model = os.environ.get("OUROBOROS_CLAUDE_CODE_MODEL", "").strip()
        task_type = str(self._current_task_type or "").strip().lower()
        model = default_model
        if task_type == "evolution":
            model = os.environ.get("OUROBOROS_CLAUDE_CODE_MODEL_EVOLUTION", default_model).strip()
        elif task_type == "review":
            model = os.environ.get("OUROBOROS_CLAUDE_CODE_MODEL_REVIEW", default_model).strip()
        elif task_type in ("task", "idle"):
            model = os.environ.get("OUROBOROS_CLAUDE_CODE_MODEL_TASK", default_model).strip()

        if not model:
            model = os.environ.get("OUROBOROS_CLAUDE_CODE_MODEL_CODE", "").strip()

        note_seed = (
            "Выбираю модель для Claude Code CLI. "
            f"task_type={task_type or '-'}, selected_model={model or 'cli_default'}."
        )
        note_text, note_usage = self._compose_natural_notice(note_seed)
        self._emit_progress(note_text)
        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "claude_code_model_selected",
                "task_type": task_type,
                "model": model or "cli_default",
                "text": truncate_for_log(note_text, 400),
            },
        )
        if note_usage:
            self._pending_events.append(
                {
                    "type": "llm_usage",
                    "task_id": None,
                    "provider": "openrouter",
                    "usage": note_usage,
                    "source": "claude_code_model_notice",
                    "ts": utc_now_iso(),
                }
            )
        if model:
            base_cmd.extend(["--model", model])

        max_budget = os.environ.get("OUROBOROS_CLAUDE_CODE_MAX_BUDGET_USD", "").strip()
        if max_budget:
            base_cmd.extend(["--max-budget-usd", max_budget])

        env = os.environ.copy()

        # Workaround for root/sudo environments (e.g. Colab):
        # many Claude Code versions refuse certain permission bypass flags unless
        # the environment is explicitly marked sandboxed.
        try:
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                env.setdefault("IS_SANDBOX", "1")
        except Exception:
            pass
        local_bin = str(pathlib.Path.home() / ".local" / "bin")
        if local_bin not in env.get("PATH", ""):
            env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

        primary_cmd = base_cmd + ["--permission-mode", perm_mode]
        legacy_cmd = base_cmd + ["--dangerously-skip-permissions"]

        lock = self._acquire_git_lock()
        try:
            try:
                run(["git", "checkout", self.env.branch_dev], cwd=self.env.repo_dir)
            except Exception as e:
                return f"⚠️ GIT_ERROR (checkout {self.env.branch_dev}): {e}"

            def _run_claude(cmd_args: List[str]) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    cmd_args,
                    cwd=str(self.env.repo_dir),
                    capture_output=True,
                    text=True,
                    timeout=600,
                    env=env,
                )

            used_mode = "permission_mode"
            res = _run_claude(primary_cmd)

            if res.returncode != 0:
                combined = ((res.stdout or "") + "\n" + (res.stderr or "")).lower()
                unsupported_permission_flag = ("--permission-mode" in combined) and any(
                    marker in combined
                    for marker in (
                        "unknown option",
                        "unknown argument",
                        "unrecognized option",
                        "unexpected argument",
                    )
                )
                if unsupported_permission_flag:
                    used_mode = "dangerously_skip_permissions"
                    append_jsonl(
                        self.env.drive_path("logs") / "events.jsonl",
                        {
                            "ts": utc_now_iso(),
                            "type": "claude_code_permission_fallback",
                            "from": "permission_mode",
                            "to": "dangerously_skip_permissions",
                            "reason": truncate_for_log((res.stderr or res.stdout or ""), 800),
                        },
                    )
                    res = _run_claude(legacy_cmd)

            stdout = (res.stdout or "").strip()
            stderr = (res.stderr or "").strip()
            if res.returncode != 0:
                return (
                    f"⚠️ CLAUDE_CODE_ERROR ({used_mode}): exit={res.returncode}\n\n"
                    f"STDOUT:\n{stdout}\n\nSTDERR:\n{stderr}"
                )

            if not stdout:
                return "OK: Claude Code completed with empty output."

        except subprocess.TimeoutExpired:
            return "⚠️ CLAUDE_CODE_TIMEOUT: command timed out after 600s."
        except Exception as e:
            return f"⚠️ CLAUDE_CODE_FAILED: {type(e).__name__}: {e}"
        finally:
            self._release_git_lock(lock)

        try:
            payload = json.loads(stdout)
        except Exception:
            return stdout

        out: Dict[str, Any] = {
            "result": payload.get("result", ""),
            "session_id": payload.get("session_id"),
            "usage": payload.get("usage", {}),
        }
        if "total_cost_usd" in payload:
            out["total_cost_usd"] = payload.get("total_cost_usd")

        # Account Claude Code CLI cost in shared supervisor budget.
        try:
            def _to_float_maybe(v: Any) -> Optional[float]:
                try:
                    return float(v)
                except Exception:
                    return None

            def _to_int_maybe(v: Any) -> Optional[int]:
                try:
                    return int(v)
                except Exception:
                    return None

            usage_obj = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            raw_cost = payload.get("total_cost_usd", None)
            cost_val = _to_float_maybe(raw_cost) if raw_cost is not None else None
            usage_event: Dict[str, Any] = {}
            if cost_val is not None:
                usage_event["cost"] = cost_val

            if isinstance(usage_obj, dict):
                p_tok = usage_obj.get("prompt_tokens", usage_obj.get("input_tokens"))
                c_tok = usage_obj.get("completion_tokens", usage_obj.get("output_tokens"))
                if p_tok is not None:
                    p_tok_i = _to_int_maybe(p_tok)
                    if p_tok_i is not None:
                        usage_event["prompt_tokens"] = p_tok_i
                if c_tok is not None:
                    c_tok_i = _to_int_maybe(c_tok)
                    if c_tok_i is not None:
                        usage_event["completion_tokens"] = c_tok_i

            if usage_event:
                self._pending_events.append(
                    {
                        "type": "llm_usage",
                        "provider": "claude_code_cli",
                        "usage": usage_event,
                        "source": "claude_code_edit",
                        "ts": utc_now_iso(),
                    }
                )
        except Exception:
            pass
        return json.dumps(out, ensure_ascii=False, indent=2)

    def _tool_request_restart(self, reason: str) -> str:
        if str(self._current_task_type or "") == "evolution" and not self._last_push_succeeded:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {
                    "ts": utc_now_iso(),
                    "type": "restart_blocked_no_push",
                    "reason": reason,
                },
            )
            return (
                "⚠️ RESTART_BLOCKED: in evolution mode call repo_commit_push/repo_write_commit and "
                "ensure push succeeds before request_restart."
            )

        # Persist expected git info for post-restart verification (best-effort)
        try:
            expected_sha = ""
            expected_branch = ""
            try:
                expected_sha = self._git_head().strip()
            except Exception:
                pass
            try:
                expected_branch = self._git_branch().strip()
            except Exception:
                pass

            pending_path = self.env.drive_path("state") / "pending_restart_verify.json"
            write_text(
                pending_path,
                json.dumps({
                    "ts": utc_now_iso(),
                    "expected_sha": expected_sha,
                    "expected_branch": expected_branch,
                    "reason": reason,
                }, ensure_ascii=False, indent=2)
            )
        except Exception:
            pass  # Never raise; verification is best-effort

        self._pending_events.append({"type": "restart_request", "reason": reason, "ts": utc_now_iso()})
        self._last_push_succeeded = False
        return f"Restart requested: {reason}"

    def _tool_promote_to_stable(self, reason: str) -> str:
        """Промоут ouroboros → ouroboros-stable. Уроборос сам решает когда (LLM-first)."""
        self._pending_events.append({"type": "promote_to_stable", "reason": reason, "ts": utc_now_iso()})
        return f"Promote to stable requested: {reason}"

    def _tool_schedule_task(self, description: str) -> str:
        self._pending_events.append({"type": "schedule_task", "description": description, "ts": utc_now_iso()})
        return f"Scheduled task request: {description}"

    def _tool_cancel_task(self, task_id: str) -> str:
        self._pending_events.append({"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()})
        return f"Cancel requested for task_id={task_id}"

    def _tool_chat_history(self, count: int = 100, offset: int = 0, search: str = "") -> str:
        """Подтянуть произвольное количество сообщений из chat.jsonl."""
        from ouroboros.memory import Memory
        mem = Memory(drive_root=self.env.drive_root, repo_dir=self.env.repo_dir)
        return mem.chat_history(count=count, offset=offset, search=search)

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

        # Extract answer robustly
        answer = self._extract_responses_output_text(resp, d)

        # Extract sources from web_search_call action
        sources: List[Dict[str, Any]] = []
        for item in d.get("output", []) or []:
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                sources = action.get("sources") or []

        # Fallback: if answer is empty but sources exist, build minimal answer from sources
        if not answer and sources:
            fallback_lines: List[str] = []
            for src in sources[:5]:  # Up to 5 sources
                parts: List[str] = []
                title = src.get("title", "").strip()
                url = src.get("url", "").strip()
                snippet = src.get("snippet", "").strip()

                # Use title or url as identifier
                identifier = title if title else url
                if identifier:
                    if snippet:
                        parts.append(f"- {identifier}: {snippet}")
                    else:
                        parts.append(f"- {identifier}")

                if parts:
                    fallback_lines.append(parts[0])

            if fallback_lines:
                answer = "\n".join(fallback_lines)

        out = {"answer": answer, "sources": sources}
        return json.dumps(out, ensure_ascii=False, indent=2)

    def _tool_telegram_send_voice(
        self,
        chat_id: int,
        text: str,
        caption: str = "",
        voice: str = "kal",
        tts: str = "local",
        openai_model: str = "gpt-4o-mini-tts",
        openai_voice: str = "alloy",
        openai_format: str = "opus",
    ) -> str:
        """Tool: synthesize text -> OGG/OPUS voice note and send to Telegram.

        Args:
          - tts: "local" (ffmpeg+flite) or "openai" (OpenAI /v1/audio/speech)
          - voice: for local flite voice (default 'kal')
          - openai_*: for OpenAI TTS
        """
        method = ""
        try:
            if (tts or "").lower() == "openai":
                ogg = self._tts_to_ogg_opus_openai(
                    text=text,
                    model=openai_model,
                    voice=openai_voice,
                    format=openai_format,
                )
                method = f"openai:{openai_model}:{openai_voice}:{openai_format}"
            else:
                ogg = self._tts_to_ogg_opus(text=text, voice=(voice or "kal"))
                method = f"ffmpeg_flite:{voice or 'kal'}"
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "tts_error", "tts": tts, "error": repr(e)},
            )
            return f"⚠️ TTS_ERROR: {type(e).__name__}: {e}"

        ok, status = self._telegram_send_voice(int(chat_id), ogg, caption=caption or "")
        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "telegram_send_voice",
                "chat_id": int(chat_id),
                "method": method,
                "ok": bool(ok),
                "status": status,
                "bytes": len(ogg),
            },
        )
        return "OK: voice sent" if ok else f"⚠️ TELEGRAM_SEND_VOICE_FAILED: {status}"

    def _tool_telegram_send_photo(self, chat_id: int, path: str, caption: str = "") -> str:
        """Tool: send a local image file to Telegram as a photo."""
        p = pathlib.Path(str(path or "").strip())
        if not p.exists() or not p.is_file():
            return f"⚠️ FILE_NOT_FOUND: {p}"
        data = p.read_bytes()
        # Best-effort mime by extension
        ext = p.suffix.lower().lstrip(".")
        mime = "image/png" if ext in ("png",) else ("image/jpeg" if ext in ("jpg", "jpeg") else "application/octet-stream")

        ok, status = self._telegram_send_photo(int(chat_id), data, caption=caption or "", filename=p.name, mime=mime)
        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "telegram_send_photo",
                "chat_id": int(chat_id),
                "ok": bool(ok),
                "status": status,
                "bytes": len(data),
                "path": str(p),
            },
        )
        return "OK: photo sent" if ok else f"⚠️ TELEGRAM_SEND_PHOTO_FAILED: {status}"

    def _tool_telegram_generate_and_send_image(
        self,
        chat_id: int,
        prompt: str,
        caption: str = "",
        model: str = "openai/gpt-5-image",
        size: str = "1024x1024",
    ) -> str:
        """Tool: generate image via OpenRouter (curl /responses) and send it to Telegram."""
        try:
            img_bytes = self._openrouter_generate_image_via_curl(
                prompt=prompt,
                model=model or "openai/gpt-5-image",
                image_config={"size": (size or "1024x1024")},
                timeout_sec=180,
            )
        except Exception as e:
            append_jsonl(
                self.env.drive_path("logs") / "events.jsonl",
                {"ts": utc_now_iso(), "type": "openrouter_image_error", "model": model, "error": repr(e)},
            )
            return f"⚠️ OPENROUTER_IMAGE_ERROR: {type(e).__name__}: {e}"

        ok, status = self._telegram_send_photo(
            int(chat_id),
            img_bytes,
            caption=caption or "",
            filename="ouroboros.png",
            mime="image/png",
        )
        append_jsonl(
            self.env.drive_path("logs") / "events.jsonl",
            {
                "ts": utc_now_iso(),
                "type": "telegram_generate_and_send_image",
                "chat_id": int(chat_id),
                "ok": bool(ok),
                "status": status,
                "bytes": len(img_bytes),
                "model": model,
                "size": size,
            },
        )
        return "OK: image generated and sent" if ok else f"⚠️ TELEGRAM_SEND_PHOTO_FAILED: {status}"

def make_agent(repo_dir: str, drive_root: str, event_queue: Any = None) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env, event_queue=event_queue)


def smoke_test() -> str:
    required = ["prompts/BASE.md", "prompts/SCRATCHPAD_SUMMARY.md", "README.md", "WORLD.md"]
    return "OK: " + ", ".join(required)
