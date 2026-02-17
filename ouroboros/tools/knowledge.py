"""Knowledge base tools: persistent structured memory on Google Drive.

Provides read/write/list operations for topic-based knowledge files
stored in memory/knowledge/ on Drive. Auto-maintains an index file.
"""

import logging
import re
from pathlib import Path
from typing import List

from ouroboros.tools.registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)

KNOWLEDGE_DIR = "memory/knowledge"
INDEX_FILE = "_index.md"

# --- Sanitization ---

_VALID_TOPIC = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,98}[a-zA-Z0-9]$|^[a-zA-Z0-9]$')
_RESERVED = frozenset({"_index", "con", "prn", "aux", "nul"})


def _sanitize_topic(topic: str) -> str:
    """Validate and sanitize topic name. Raises ValueError on bad input."""
    if not topic or not isinstance(topic, str):
        raise ValueError("Topic must be a non-empty string")

    # Strip whitespace
    topic = topic.strip()

    # Reject path separators and traversal
    if '/' in topic or '\\' in topic or '..' in topic:
        raise ValueError(f"Invalid characters in topic: {topic}")

    # Check against pattern
    if not _VALID_TOPIC.match(topic):
        raise ValueError(f"Invalid topic name: {topic}. Use alphanumeric, underscore, hyphen, dot.")

    # Reject reserved names
    if topic.lower() in _RESERVED:
        raise ValueError(f"Reserved topic name: {topic}")

    return topic


def _safe_path(ctx: ToolContext, topic: str) -> tuple[Path, str]:
    """Build and verify path is within knowledge directory.

    Returns:
        tuple[Path, str]: (path, sanitized_topic)
    """
    sanitized_topic = _sanitize_topic(topic)
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    path = kdir / f"{sanitized_topic}.md"

    # Resolve and verify containment
    resolved = path.resolve()
    kdir_resolved = kdir.resolve()

    # Use relative_to for robust path containment check
    try:
        resolved.relative_to(kdir_resolved)
    except ValueError:
        raise ValueError(f"Path escape detected: {topic}")

    return path, sanitized_topic


# --- Helpers ---

def _ensure_dir(ctx: ToolContext):
    """Create knowledge directory if it doesn't exist."""
    ctx.drive_path(KNOWLEDGE_DIR).mkdir(parents=True, exist_ok=True)


def _rebuild_index(ctx: ToolContext):
    """Rebuild the knowledge index from all .md files (full scan)."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    if not kdir.exists():
        return

    entries = []
    for f in sorted(kdir.glob("*.md")):
        if f.name == INDEX_FILE:
            continue
        # Sanitize topic from filename to protect against hand-crafted filenames
        try:
            topic = _sanitize_topic(f.stem)
        except ValueError:
            # Skip files with invalid names
            continue

        # Read first non-empty line as summary
        try:
            text = f.read_text(encoding="utf-8").strip()
            first_line = ""
            for line in text.split("\n"):
                line = line.strip().lstrip("#").strip()
                if line:
                    first_line = line[:120]
                    break
            entries.append(f"- **{topic}**: {first_line}")
        except Exception:
            log.debug(f"Failed to read knowledge file for index rebuild: {topic}", exc_info=True)
            entries.append(f"- **{topic}**: (unreadable)")

    index_content = "# Knowledge Base Index\n\n"
    if entries:
        index_content += "\n".join(entries) + "\n"
    else:
        index_content += "(empty)\n"

    (kdir / INDEX_FILE).write_text(index_content, encoding="utf-8")


def _update_index_entry(ctx: ToolContext, topic: str):
    """Incrementally update the index for a single topic."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    index_path = kdir / INDEX_FILE
    topic_path = kdir / f"{topic}.md"

    _ensure_dir(ctx)

    # Read existing index or create header
    if index_path.exists():
        index_content = index_path.read_text(encoding="utf-8")
    else:
        index_content = "# Knowledge Base Index\n\n"

    # Split into lines, preserving header
    lines = index_content.split("\n")
    header_end = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            header_end = i + 1
            if i + 1 < len(lines) and lines[i + 1].strip() == "":
                header_end = i + 2
            break

    header = "\n".join(lines[:header_end])
    entries = [line for line in lines[header_end:] if line.strip() and line.strip() != "(empty)"]

    # Remove old entry for this topic (if exists)
    pattern = f"- **{topic}**:"
    entries = [e for e in entries if not e.strip().startswith(pattern)]

    # Add new entry if topic file exists
    if topic_path.exists():
        try:
            text = topic_path.read_text(encoding="utf-8").strip()
            first_line = ""
            for line in text.split("\n"):
                line = line.strip().lstrip("#").strip()
                if line:
                    first_line = line[:120]
                    break
            new_entry = f"- **{topic}**: {first_line}"
        except Exception:
            log.debug(f"Failed to read knowledge file for index update: {topic}", exc_info=True)
            new_entry = f"- **{topic}**: (unreadable)"

        # Insert in sorted position
        entries.append(new_entry)
        entries.sort(key=lambda e: e.lower())

    # Rebuild index content
    if entries:
        new_index = header.rstrip("\n") + "\n\n" + "\n".join(entries) + "\n"
    else:
        new_index = header.rstrip("\n") + "\n\n(empty)\n"

    # Atomic write: temp file + replace (works on Windows even if target exists)
    temp_path = index_path.with_suffix(".tmp")
    temp_path.write_text(new_index, encoding="utf-8")
    temp_path.replace(index_path)


# --- Tool handlers ---

def _knowledge_read(ctx: ToolContext, topic: str) -> str:
    """Read a knowledge file by topic name."""
    try:
        path, sanitized_topic = _safe_path(ctx, topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    if not path.exists():
        return f"Topic '{sanitized_topic}' not found. Use knowledge_list to see available topics."
    return path.read_text(encoding="utf-8")


def _knowledge_write(ctx: ToolContext, topic: str, content: str, mode: str = "overwrite") -> str:
    """Write or append to a knowledge file."""
    try:
        path, sanitized_topic = _safe_path(ctx, topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    # Validate mode explicitly
    if mode not in ("overwrite", "append"):
        return f"⚠️ Invalid mode '{mode}'. Use 'overwrite' or 'append'."

    _ensure_dir(ctx)

    if mode == "append":
        # Check if we need a leading newline by reading last byte first
        needs_newline = False
        if path.exists() and path.stat().st_size > 0:
            with open(path, "rb") as rf:
                rf.seek(-1, 2)  # Seek to last byte
                if rf.read(1) != b"\n":
                    needs_newline = True

        # Now open for append and write
        with open(path, "a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write(content)
    else:
        path.write_text(content, encoding="utf-8")

    _update_index_entry(ctx, sanitized_topic)
    return f"✅ Knowledge '{sanitized_topic}' saved ({mode})."


def _knowledge_list(ctx: ToolContext) -> str:
    """List all knowledge topics with summaries."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    index_path = kdir / INDEX_FILE

    if index_path.exists():
        return index_path.read_text(encoding="utf-8")

    # No index yet — build it
    if kdir.exists():
        _rebuild_index(ctx)
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")

    return "Knowledge base is empty. Use knowledge_write to add topics."


# --- Tool registration ---

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("knowledge_read", {
            "name": "knowledge_read",
            "description": "Read a topic from the persistent knowledge base on Drive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (alphanumeric, hyphens, underscores). E.g. 'browser-automation', 'joi_gotchas'"
                    }
                },
                "required": ["topic"]
            },
        }, _knowledge_read),
        ToolEntry("knowledge_write", {
            "name": "knowledge_write",
            "description": "Write or append to a knowledge topic. Use for recipes, gotchas, patterns learned from experience.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (alphanumeric, hyphens, underscores)"
                    },
                    "content": {
                        "type": "string",
                        "description": "Content to write (markdown)"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "Write mode: 'overwrite' (default) or 'append'"
                    }
                },
                "required": ["topic", "content"]
            },
        }, _knowledge_write),
        ToolEntry("knowledge_list", {
            "name": "knowledge_list",
            "description": "List all topics in the knowledge base with summaries.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            },
        }, _knowledge_list),
    ]
