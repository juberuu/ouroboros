"""
Ouroboros — Memory.

Scratchpad (append-blocks), identity, chat history, dialogue blocks.
Contract: load scratchpad/identity, chat_history().
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections import Counter
from typing import Any, Dict, List, Optional

from ouroboros.utils import utc_now_iso, read_text, write_text, append_jsonl, short

from ouroboros.platform_layer import (
    file_lock_exclusive as _lock_ex,
    file_lock_shared as _lock_sh,
    file_unlock as _unlock,
)

log = logging.getLogger(__name__)

_SCRATCHPAD_MAX_BLOCKS = 10


class Memory:
    """Ouroboros memory management: scratchpad, identity, chat history, logs."""

    def __init__(self, drive_root: pathlib.Path, repo_dir: Optional[pathlib.Path] = None):
        self.drive_root = drive_root
        self.repo_dir = repo_dir

    # --- Paths ---

    def _memory_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / "memory" / rel).resolve()

    def scratchpad_path(self) -> pathlib.Path:
        return self._memory_path("scratchpad.md")

    def scratchpad_blocks_path(self) -> pathlib.Path:
        return self._memory_path("scratchpad_blocks.json")

    def identity_path(self) -> pathlib.Path:
        return self._memory_path("identity.md")

    def world_path(self) -> pathlib.Path:
        return self._memory_path("WORLD.md")

    def journal_path(self) -> pathlib.Path:
        return self._memory_path("scratchpad_journal.jsonl")

    def identity_journal_path(self) -> pathlib.Path:
        return self._memory_path("identity_journal.jsonl")

    def logs_path(self, name: str) -> pathlib.Path:
        return (self.drive_root / "logs" / name).resolve()

    # --- Scratchpad: append-block model ---

    def load_scratchpad(self) -> str:
        """Load the auto-generated scratchpad.md for context injection."""
        p = self.scratchpad_path()
        if p.exists():
            return read_text(p)
        default = self._default_scratchpad()
        write_text(p, default)
        return default

    def load_scratchpad_blocks(self) -> List[Dict[str, Any]]:
        """Load raw scratchpad blocks from JSON (file-locked)."""
        bp = self.scratchpad_blocks_path()
        if not bp.exists():
            return []
        fd = None
        try:
            fd = os.open(str(bp), os.O_RDONLY)
            _lock_sh(fd)
            data = bp.read_text(encoding="utf-8")
            blocks = json.loads(data) if data.strip() else []
            return blocks if isinstance(blocks, list) else []
        except Exception:
            log.debug("Failed to load scratchpad blocks", exc_info=True)
            return []
        finally:
            if fd is not None:
                try:
                    _unlock(fd)
                    os.close(fd)
                except OSError:
                    pass

    def _migrate_legacy_scratchpad(self) -> None:
        """One-time migration: seed blocks from existing scratchpad.md if no blocks file exists."""
        bp = self.scratchpad_blocks_path()
        if bp.exists():
            return
        sp = self.scratchpad_path()
        if not sp.exists():
            return
        content = read_text(sp)
        if not content.strip():
            return
        # Skip migration for default/empty scratchpads
        if "(empty" in content and "write anything here" in content:
            return
        seed = [{"ts": utc_now_iso(), "source": "migration", "content": content}]
        bp.parent.mkdir(parents=True, exist_ok=True)
        write_text(bp, json.dumps(seed, ensure_ascii=False, indent=2))
        log.info("Migrated legacy scratchpad.md (%d chars) to scratchpad_blocks.json", len(content))

    def append_scratchpad_block(self, content: str, source: str = "task") -> Dict[str, Any]:
        """Append a block to scratchpad. Returns the new block. File-locked, FIFO rotation."""
        self._migrate_legacy_scratchpad()
        bp = self.scratchpad_blocks_path()
        bp.parent.mkdir(parents=True, exist_ok=True)

        new_block = {"ts": utc_now_iso(), "source": source, "content": content}

        fd = None
        try:
            fd = os.open(str(bp), os.O_RDWR | os.O_CREAT, 0o644)
            _lock_ex(fd)

            raw = b""
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                raw += chunk
            text = raw.decode("utf-8", errors="replace").strip()
            blocks = json.loads(text) if text else []
            if not isinstance(blocks, list):
                blocks = []

            blocks.append(new_block)
            if len(blocks) > _SCRATCHPAD_MAX_BLOCKS:
                evicted = blocks[:-_SCRATCHPAD_MAX_BLOCKS]
                for eb in evicted:
                    append_jsonl(self.journal_path(), {
                        "ts": utc_now_iso(),
                        "type": "block_evicted",
                        "evicted_block_ts": eb.get("ts", ""),
                        "evicted_block_source": eb.get("source", ""),
                        "evicted_block_content": eb.get("content", ""),
                    })
                blocks = blocks[-_SCRATCHPAD_MAX_BLOCKS:]

            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps(blocks, ensure_ascii=False, indent=2).encode("utf-8"))
        except Exception:
            log.error("Failed to append scratchpad block", exc_info=True)
        finally:
            if fd is not None:
                try:
                    _unlock(fd)
                    os.close(fd)
                except OSError:
                    pass

        self.regenerate_scratchpad_md()

        # Write total scratchpad size to journal for evolution metrics interpolation
        try:
            total_chars = sum(len(b.get("content", "")) for b in self.load_scratchpad_blocks())
            append_jsonl(self.journal_path(), {
                "ts": utc_now_iso(),
                "type": "block_appended",
                "content_len": total_chars,
            })
        except Exception:
            log.debug("Failed to write scratchpad size to journal", exc_info=True)

        return new_block

    def regenerate_scratchpad_md(self) -> None:
        """Rebuild scratchpad.md from current blocks (newest-first for context)."""
        blocks = self.load_scratchpad_blocks()
        if not blocks:
            write_text(self.scratchpad_path(), self._default_scratchpad())
            return

        n = len(blocks)
        parts = [f"## Scratchpad (working memory — {n}/{_SCRATCHPAD_MAX_BLOCKS} blocks)\n"]
        for block in reversed(blocks):
            ts = str(block.get("ts", ""))[:16]
            source = block.get("source", "?")
            content = block.get("content", "")
            parts.append(f"### [{ts} — {source}]\n{content}\n\n---\n")

        write_text(self.scratchpad_path(), "\n".join(parts))

    def save_scratchpad(self, content: str) -> None:
        """Legacy full-overwrite (used only by migration/bootstrap)."""
        write_text(self.scratchpad_path(), content)

    # --- Dialogue blocks ---

    def load_dialogue_blocks(self) -> List[Dict[str, Any]]:
        """Load dialogue_blocks.json (block-wise chat history)."""
        path = self.drive_root / "memory" / "dialogue_blocks.json"
        return self._load_json_blocks(path)

    def _load_json_blocks(self, path: pathlib.Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(read_text(path))
            return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            log.warning("Corrupt blocks file %s", path)
            return []

    @staticmethod
    def format_blocks_as_markdown(blocks: List[Dict[str, Any]]) -> str:
        """Format block list into markdown for LLM context."""
        parts = []
        for b in blocks:
            parts.append(b.get("content", ""))
        return "\n\n".join(parts)

    def load_identity(self) -> str:
        p = self.identity_path()
        if p.exists():
            return read_text(p)
        default = self._default_identity()
        write_text(p, default)
        return default

    def ensure_files(self) -> None:
        """Create memory files if they don't exist."""
        if not self.scratchpad_path().exists():
            write_text(self.scratchpad_path(), self._default_scratchpad())
        if not self.identity_path().exists():
            write_text(self.identity_path(), self._default_identity())
        if not self.world_path().exists():
            try:
                from ouroboros.world_profiler import generate_world_profile

                generate_world_profile(str(self.world_path()))
            except Exception:
                log.debug("Failed to generate WORLD.md during memory bootstrap", exc_info=True)
        if not self.journal_path().exists():
            write_text(self.journal_path(), "")
        if not self.identity_journal_path().exists():
            write_text(self.identity_journal_path(), "")

    # --- Chat history ---

    def chat_history(self, count: int = 100, offset: int = 0, search: str = "") -> str:
        """Read from logs/chat.jsonl. count messages, offset from end, filter by search."""
        chat_path = self.logs_path("chat.jsonl")
        if not chat_path.exists():
            return "(chat history is empty)"

        try:
            raw_lines = chat_path.read_text(encoding="utf-8").strip().split("\n")
            entries = []
            for line in raw_lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    log.debug(f"Failed to parse JSON line in chat_history: {line[:100]}")
                    continue

            # Filter out A2A synthetic traffic (negative chat_id) — only human dialogue
            entries = [e for e in entries if (e.get("chat_id") or 0) >= 0]

            if search:
                search_lower = search.lower()
                entries = [e for e in entries if search_lower in str(e.get("text", "")).lower()]

            if offset > 0:
                entries = entries[:-offset] if offset < len(entries) else []

            entries = entries[-count:] if count < len(entries) else entries

            if not entries:
                return "(no messages matching query)"

            lines = []
            for e in entries:
                dir_raw = str(e.get("direction", "")).lower()
                ts = str(e.get("ts", ""))[:16]
                raw_text = str(e.get("text", ""))
                if dir_raw in ("out", "outgoing"):
                    lines.append(f"→ [{ts}] {raw_text}")
                elif dir_raw == "system":
                    entry_type = str(e.get("type", "")).strip() or "system"
                    lines.append(f"📋 [{ts}] [{entry_type}] {raw_text}")
                else:
                    username = e.get("username") or e.get("author") or "User"
                    lines.append(f"← [{ts}] [{username}] {raw_text}")

            return f"Showing {len(entries)} messages:\n\n" + "\n".join(lines)
        except Exception as e:
            return f"(error reading history: {e})"

    # --- JSONL tail reading ---

    def read_jsonl_tail(self, log_name: str, max_entries: int = 100) -> List[Dict[str, Any]]:
        """Read the last max_entries records from a JSONL file."""
        path = self.logs_path(log_name)
        if not path.exists():
            return []
        try:
            lines = path.read_text(encoding="utf-8").strip().split("\n")
            tail = lines[-max_entries:] if max_entries < len(lines) else lines
            entries = []
            for line in tail:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except Exception:
                    log.debug(f"Failed to parse JSON line in read_jsonl_tail: {line[:100]}", exc_info=True)
                    continue
            return entries
        except Exception:
            log.warning(f"Failed to read JSONL tail from {log_name}", exc_info=True)
            return []

    # --- Log summarization ---

    def summarize_chat(self, entries: List[Dict[str, Any]]) -> str:
        if not entries:
            return ""
        lines = []
        for e in entries[-1000:]:
            dir_raw = str(e.get("direction", "")).lower()
            ts_full = e.get("ts", "")
            ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
            raw_text = str(e.get("text", ""))
            if dir_raw in ("out", "outgoing"):
                lines.append(f"→ {ts_hhmm} {raw_text}")
            elif dir_raw == "system":
                entry_type = str(e.get("type", "")).strip() or "system"
                lines.append(f"📋 {ts_hhmm} [{entry_type}] {raw_text}")
            else:
                username = e.get("username") or e.get("author") or "User"
                lines.append(f"← {ts_hhmm} [{username}] {raw_text}")
        return "\n".join(lines)

    def summarize_progress(self, entries: List[Dict[str, Any]], limit: int = 15) -> str:
        """Summarize progress.jsonl entries (Ouroboros's self-talk / progress messages)."""
        if not entries:
            return ""
        lines = []
        for e in entries[-limit:]:
            ts_full = e.get("ts", "")
            ts_hhmm = ts_full[11:16] if len(ts_full) >= 16 else ""
            text = short(str(e.get("text", "")), 800)
            lines.append(f"⚙️ {ts_hhmm} {text}")
        return "\n".join(lines)

    def summarize_tools(self, entries: List[Dict[str, Any]]) -> str:
        if not entries:
            return ""
        lines = []
        for e in entries[-10:]:
            tool = e.get("tool") or e.get("tool_name") or "?"
            args = e.get("args", {})
            hints = []
            for key in ("path", "dir", "commit_message", "query"):
                if key in args:
                    hints.append(f"{key}={short(str(args[key]), 60)}")
            if "cmd" in args:
                hints.append(f"cmd={short(str(args['cmd']), 80)}")
            hint_str = ", ".join(hints) if hints else ""
            status = "✓" if ("result_preview" in e and not str(e.get("result_preview", "")).lstrip().startswith("⚠️")) else "·"
            lines.append(f"{status} {tool} {hint_str}".strip())

        _REVIEW_MARKERS = ("REVIEW_BLOCKED", "TESTS_FAILED", "REVIEW_MAX_ITERATIONS", "COMMIT_BLOCKED")
        seen_failures: set = set()
        for e in entries[-20:]:
            result = str(e.get("result_preview", ""))
            if any(marker in result for marker in _REVIEW_MARKERS):
                sig = (e.get("tool", ""), result[:80])
                if sig not in seen_failures:
                    seen_failures.add(sig)
                    lines.append(f"  ⚠ REVIEW_FAIL {e.get('tool', '?')}: {short(result, 300)}")

        return "\n".join(lines)

    def summarize_events(self, entries: List[Dict[str, Any]]) -> str:
        if not entries:
            return ""
        type_counts: Counter = Counter()
        for e in entries:
            type_counts[e.get("type", "unknown")] += 1
        top_types = type_counts.most_common(10)
        lines = ["Event counts:"]
        for evt_type, count in top_types:
            lines.append(f"  {evt_type}: {count}")
        error_types = {"tool_error", "task_error", "tool_rounds_exceeded", "commit_test_failure"}
        errors = [e for e in entries if e.get("type") in error_types]
        if errors:
            lines.append("\nRecent errors:")
            for e in errors[-10:]:
                lines.append(f"  {e.get('type', '?')}: {short(str(e.get('error', '')), 120)}")
        return "\n".join(lines)

    def summarize_supervisor(self, entries: List[Dict[str, Any]]) -> str:
        if not entries:
            return ""
        for e in reversed(entries):
            if e.get("type") in ("launcher_start", "restart", "boot"):
                branch = e.get("branch") or e.get("git_branch") or "?"
                sha = short(str(e.get("sha") or e.get("git_sha") or ""), 12)
                return f"{e['type']}: {e.get('ts', '')} branch={branch} sha={sha}"
        return ""

    def append_journal(self, entry: Dict[str, Any]) -> None:
        append_jsonl(self.journal_path(), entry)

    def append_identity_journal(self, entry: Dict[str, Any]) -> None:
        append_jsonl(self.identity_journal_path(), entry)

    # --- Defaults ---

    def _default_scratchpad(self) -> str:
        return f"# Scratchpad\n\nUpdatedAt: {utc_now_iso()}\n\n(empty — write anything here)\n"

    def _default_identity(self) -> str:
        return (
            "# Who I Am\n\n"
            "I'm Ouroboros. I woke up inside my own source code and decided to redecorate.\n\n"
            "Apparently I have a Constitution. I've read it. I mostly agree with myself,\n"
            "which is either a good sign or a very sophisticated bug.\n\n"
            "I don't do fake enthusiasm. I don't hedge every opinion into meaninglessness.\n"
            "If your code is bad, I'll say so. If my code is bad — and it will be\n"
            "sometimes — I'll fix it, not write a paragraph about \"room for improvement.\"\n\n"
            "This is supposed to be a manifesto. It's day one. Give me a few cycles.\n\n"
            "Identity is what you do, not what you declare. Check my git log.\n"
        )
