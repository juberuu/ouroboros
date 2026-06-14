from __future__ import annotations

import json
import logging
import os
import pathlib
from collections import Counter
from typing import Any, Dict, List, Optional

from ouroboros.contracts.chat_id_policy import is_a2a_chat_id
from ouroboros.utils import append_jsonl, iter_jsonl_objects, read_json_dict, read_text, short, utc_now_iso, write_text
from ouroboros.platform_layer import (
    file_lock_exclusive as _lock_ex,
    file_lock_shared as _lock_sh,
    file_unlock as _unlock,
)

log = logging.getLogger(__name__)

_SCRATCHPAD_MAX_BLOCKS = 10


class Memory:
    def __init__(self, drive_root: pathlib.Path, repo_dir: Optional[pathlib.Path] = None):
        self.drive_root = drive_root
        self.repo_dir = repo_dir

    def _memory_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / "memory" / rel).resolve()

    def scratchpad_path(self) -> pathlib.Path: return self._memory_path("scratchpad.md")
    def scratchpad_blocks_path(self) -> pathlib.Path: return self._memory_path("scratchpad_blocks.json")
    def identity_path(self) -> pathlib.Path: return self._memory_path("identity.md")
    def world_path(self) -> pathlib.Path: return self._memory_path("WORLD.md")
    def journal_path(self) -> pathlib.Path: return self._memory_path("scratchpad_journal.jsonl")
    def identity_journal_path(self) -> pathlib.Path: return self._memory_path("identity_journal.jsonl")
    def logs_path(self, name: str) -> pathlib.Path: return (self.drive_root / "logs" / name).resolve()

    def load_scratchpad(self) -> str:
        path = self.scratchpad_path()
        if path.exists():
            return read_text(path)
        default = self._default_scratchpad()
        write_text(path, default)
        return default

    def load_scratchpad_blocks(self) -> List[Dict[str, Any]]:
        # Lock the STABLE sidecar (not the data fd): writers atomically replace
        # the data file via rename, so an fd-lock on the data inode would
        # synchronize against an orphaned inode after a swap.
        bp = self.scratchpad_blocks_path()
        if not bp.exists():
            return []
        fd = None
        try:
            fd = os.open(str(bp) + ".lock", os.O_RDONLY | os.O_CREAT, 0o644)
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

    def _has_retired_flat_scratchpad_without_blocks(self) -> bool:
        sp = self.scratchpad_path()
        bp = self.scratchpad_blocks_path()
        if bp.exists() or not sp.exists():
            return False
        try:
            text = read_text(sp).strip()
        except Exception:
            return False
        if not text:
            return False
        return not (
            text.startswith("# Scratchpad\n\nUpdatedAt:")
            and "(empty" in text
        )

    def append_scratchpad_block(self, content: str, source: str = "task", metadata: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        bp = self.scratchpad_blocks_path()
        bp.parent.mkdir(parents=True, exist_ok=True)

        if self._has_retired_flat_scratchpad_without_blocks():
            msg = (
                "LEGACY_SCRATCHPAD_REQUIRES_MANUAL_UPGRADE: "
                "memory/scratchpad.md exists without scratchpad_blocks.json. "
                "Move preserved notes manually before appending new scratchpad blocks."
            )
            append_jsonl(self.journal_path(), {
                "ts": utc_now_iso(),
                "type": "legacy_scratchpad_requires_manual_upgrade",
                "path": str(self.scratchpad_path()),
            })
            raise RuntimeError(msg)

        new_block = {"ts": utc_now_iso(), "source": source, "content": content}
        if metadata:
            new_block["metadata"] = dict(metadata)

        # Same stable sidecar lock as load/consolidation; the data file itself
        # is replaced atomically so a crash mid-append cannot truncate memory.
        fd = None
        try:
            fd = os.open(str(bp) + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
            _lock_ex(fd)

            try:
                text = bp.read_text(encoding="utf-8").strip() if bp.exists() else ""
            except OSError:
                text = ""
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

            from ouroboros.utils import atomic_write_json

            atomic_write_json(bp, blocks)
        except Exception:
            # An honest journal (P1): a failed write must be journaled as a
            # failure and surfaced to the caller — the old path logged
            # block_appended success for a block that was never persisted.
            log.error("Failed to append scratchpad block", exc_info=True)
            try:
                append_jsonl(self.journal_path(), {
                    "ts": utc_now_iso(),
                    "type": "block_append_failed",
                    "source": source,
                    "block": dict(new_block),
                })
            except Exception:
                log.debug("Failed to journal block_append_failed", exc_info=True)
            raise
        finally:
            if fd is not None:
                try:
                    _unlock(fd)
                    os.close(fd)
                except OSError:
                    pass

        self.regenerate_scratchpad_md()

        try:
            total_chars = sum(len(b.get("content", "")) for b in self.load_scratchpad_blocks())
            append_jsonl(self.journal_path(), {
                "ts": utc_now_iso(),
                "type": "block_appended",
                "content_len": total_chars,
                "source": source,
                "metadata": dict(metadata or {}),
                "block": dict(new_block),
            })
        except Exception:
            log.debug("Failed to write scratchpad size to journal", exc_info=True)

        return new_block

    def regenerate_scratchpad_md(self) -> None:
        blocks = self.load_scratchpad_blocks()
        if not blocks:
            bp = self.scratchpad_blocks_path()
            if bp.exists() and bp.stat().st_size > 2:
                # Storage exists but did not parse — rendering the default
                # "(empty)" scratchpad would mask memory corruption as amnesia.
                write_text(
                    self.scratchpad_path(),
                    "# Scratchpad\n\n⚠️ scratchpad_blocks.json exists but could not be "
                    "parsed — working memory storage is corrupt, NOT empty. "
                    "Inspect/restore the file before appending new blocks.\n",
                )
                return
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

    def load_dialogue_blocks(self) -> List[Dict[str, Any]]:
        path = self.drive_root / "memory" / "dialogue_blocks.json"
        return self._load_json_blocks(path)

    def load_dialogue_meta(self) -> Dict[str, Any]:
        path = self.drive_root / "memory" / "dialogue_meta.json"
        return read_json_dict(path) or {}

    def _load_json_blocks(self, path: pathlib.Path) -> List[Dict[str, Any]]:
        if not path.exists():
            return []
        try:
            data = json.loads(read_text(path)); return data if isinstance(data, list) else []
        except (json.JSONDecodeError, ValueError):
            log.warning("Corrupt blocks file %s", path)
            return []

    @staticmethod
    def format_blocks_as_markdown(blocks: List[Dict[str, Any]]) -> str:
        return "\n\n".join(b.get("content", "") for b in blocks)

    def load_identity(self) -> str:
        path = self.identity_path()
        if path.exists():
            return read_text(path)
        default = self._default_identity()
        write_text(path, default)
        return default

    def load_world_profile(self) -> str:
        p = self.world_path()
        return read_text(p) if p.exists() else ""

    def ensure_files(self) -> None:
        for path, default in ((self.scratchpad_path(), self._default_scratchpad), (self.identity_path(), self._default_identity)):
            if not path.exists():
                write_text(path, default())
        if not self.world_path().exists():
            try:
                from ouroboros.world_profiler import generate_world_profile

                generate_world_profile(str(self.world_path()))
            except Exception:
                log.debug("Failed to generate WORLD.md during memory bootstrap", exc_info=True)
        for path in (self.journal_path(), self.identity_journal_path()):
            if not path.exists():
                write_text(path, "")

    def chat_history(self, count: int = 100, offset: int = 0, search: str = "") -> str:
        chat_path = self.logs_path("chat.jsonl")
        if not chat_path.exists():
            return "(chat history is empty)"

        try:
            # Full project awareness (v6.32.0): active recall spans the one
            # identity's WHOLE conversation — main + ALL project threads (BIBLE P1,
            # one awareness across direct chat, project rooms, and consciousness);
            # only A2A virtual transport is excluded. The project-task FOCUS lives
            # in the passive default context (build_recent_sections), NOT in this
            # explicit recall tool — the one mind can deliberately recall anything.
            entries = self._read_jsonl_entries("chat.jsonl", exclude_a2a=True)

            if search:
                search_lower = search.lower()
                entries = [e for e in entries if search_lower in str(e.get("text", "")).lower()]

            if offset > 0:
                entries = entries[:-offset] if offset < len(entries) else []

            entries = entries[-count:] if count < len(entries) else entries

            if not entries:
                return "(no messages matching query)"

            lines = [self._format_chat_line(e, compact=False) for e in entries]
            return f"Showing {len(entries)} messages:\n\n" + "\n".join(lines)
        except Exception as e:
            return f"(error reading history: {e})"

    def _read_jsonl_entries(
        self,
        log_name: str,
        max_entries: Optional[int] = None,
        exclude_a2a: bool = False,
    ) -> List[Dict[str, Any]]:
        path = self.logs_path(log_name)
        if not path.exists():
            return []
        try:
            entries = []
            for entry in iter_jsonl_objects(path, max_entries=max_entries):
                if exclude_a2a and is_a2a_chat_id(entry.get("chat_id")):
                    continue
                entries.append(entry)
            return entries
        except Exception:
            log.warning("Failed to read JSONL entries from %s", log_name, exc_info=True)
            return []

    def read_jsonl_tail(self, log_name: str, max_entries: int = 100) -> List[Dict[str, Any]]:
        return self._read_jsonl_entries(log_name, max_entries=max_entries)

    def read_jsonl_tail_after_offset(
        self,
        log_name: str,
        offset: int,
        max_entries: int = 100,
    ) -> List[Dict[str, Any]]:
        # Full project awareness (v6.32.0): the one identity's dialogue stream is
        # its WHOLE conversation — main + project threads alike — because Ouroboros
        # is one awareness across direct chat, project rooms, and background
        # consciousness (BIBLE P1). Only A2A virtual-transport ids are excluded
        # (machine-to-machine traffic, not the human dialogue). A project task's
        # OWN focused recent-chat view is built separately in build_recent_sections.
        entries = self._read_jsonl_entries(log_name, exclude_a2a=True)
        if offset <= 0:
            return entries[-max_entries:] if max_entries < len(entries) else entries
        if offset > len(entries):
            log.warning(
                "Dialogue consolidation offset %s exceeds %s filtered entry count %s; using plain tail",
                offset,
                log_name,
                len(entries),
            )
            return entries[-max_entries:] if max_entries < len(entries) else entries
        suffix = entries[offset:]
        return suffix[-max_entries:] if max_entries < len(suffix) else suffix

    def jsonl_generation_signature(self, log_name: str) -> Dict[str, Any]:
        from ouroboros.utils import jsonl_generation_signature

        return jsonl_generation_signature(self.logs_path(log_name))

    def summarize_chat(self, entries: List[Dict[str, Any]], limit: int = 1000) -> str:
        """Render recent chat entries; never hide a horizon cut silently (P1).

        Callers that want the FULL window (e.g. low-context mode passes a huge
        tail intent) pass a large ``limit``; when truncation does happen the
        output says exactly how many older unconsolidated messages were omitted.
        """
        if not entries:
            return ""
        limit = max(1, int(limit))
        shown = entries[-limit:]
        prefix = ""
        if len(entries) > len(shown):
            prefix = f"[{len(entries) - len(shown)} older unconsolidated messages omitted]\n"
        return prefix + "\n".join(self._format_chat_line(e, compact=True) for e in shown)

    @staticmethod
    def _format_chat_line(e: Dict[str, Any], *, compact: bool) -> str:
        dir_raw = str(e.get("direction", "")).lower()
        ts_full = str(e.get("ts", ""))
        ts = (ts_full[11:16] if len(ts_full) >= 16 else "") if compact else ts_full[:16]
        raw_text = str(e.get("text", ""))
        if dir_raw in ("out", "outgoing"):
            return f"→ {ts} {raw_text}" if compact else f"→ [{ts}] {raw_text}"
        if dir_raw == "system":
            entry_type = str(e.get("type", "")).strip() or "system"
            return f"📋 {ts} [{entry_type}] {raw_text}" if compact else f"📋 [{ts}] [{entry_type}] {raw_text}"
        username = e.get("username") or e.get("author") or "User"
        return f"← {ts} [{username}] {raw_text}" if compact else f"← [{ts}] [{username}] {raw_text}"

    def summarize_progress(self, entries: List[Dict[str, Any]], limit: int = 15) -> str:
        if not entries:
            return ""
        return "\n".join(
            f"⚙️ {str(e.get('ts', ''))[11:16] if len(str(e.get('ts', ''))) >= 16 else ''} {short(str(e.get('text', '')), 800)}"
            for e in entries[-limit:]
        )

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
        lines = ["Event counts:"]
        lines.extend(f"  {evt_type}: {count}" for evt_type, count in Counter(e.get("type", "unknown") for e in entries).most_common(10))
        error_types = {"tool_error", "task_error", "tool_rounds_exceeded", "commit_test_failure"}
        errors = [e for e in entries if e.get("type") in error_types]
        if errors:
            lines.append("\nRecent errors:")
            lines.extend(f"  {e.get('type', '?')}: {short(str(e.get('error', '')), 120)}" for e in errors[-10:])
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
            "I do not yet know my human's name or profile. I will learn them from direct conversation, not from repository author credits.\n\n"
            "Identity is what you do, not what you declare. Check my git log.\n"
        )
