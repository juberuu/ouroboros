"""Knowledge base tools: persistent structured memory on Google Drive.

Provides read/write/list operations for topic-based knowledge files
stored in memory/knowledge/ on Drive. Auto-maintains an index file.
"""

import re
from pathlib import Path
from typing import List

from ouroboros.tools.registry import ToolEntry, ToolContext

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


def _safe_path(ctx: ToolContext, topic: str) -> Path:
    """Build and verify path is within knowledge directory."""
    topic = _sanitize_topic(topic)
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    path = kdir / f"{topic}.md"

    # Resolve and verify containment
    resolved = path.resolve()
    kdir_resolved = kdir.resolve()
    if not str(resolved).startswith(str(kdir_resolved) + "/") and resolved != kdir_resolved:
        raise ValueError(f"Path escape detected: {topic}")

    return path


# --- Helpers ---

def _ensure_dir(ctx: ToolContext):
    """Create knowledge directory if it doesn't exist."""
    ctx.drive_path(KNOWLEDGE_DIR).mkdir(parents=True, exist_ok=True)


def _update_index(ctx: ToolContext):
    """Rebuild the knowledge index from all .md files."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    if not kdir.exists():
        return

    entries = []
    for f in sorted(kdir.glob("*.md")):
        if f.name == INDEX_FILE:
            continue
        topic = f.stem
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
            entries.append(f"- **{topic}**: (unreadable)")

    index_content = "# Knowledge Base Index\n\n"
    if entries:
        index_content += "\n".join(entries) + "\n"
    else:
        index_content += "(empty)\n"

    (kdir / INDEX_FILE).write_text(index_content, encoding="utf-8")


# --- Tool handlers ---

def _knowledge_read(ctx: ToolContext, topic: str) -> str:
    """Read a knowledge file by topic name."""
    try:
        path = _safe_path(ctx, topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    if not path.exists():
        return f"Topic '{topic}' not found. Use knowledge_list to see available topics."
    return path.read_text(encoding="utf-8")


def _knowledge_write(ctx: ToolContext, topic: str, content: str, mode: str = "overwrite") -> str:
    """Write or append to a knowledge file."""
    try:
        path = _safe_path(ctx, topic)
    except ValueError as e:
        return f"⚠️ Invalid topic: {e}"

    # Validate mode explicitly
    if mode not in ("overwrite", "append"):
        return f"⚠️ Invalid mode '{mode}'. Use 'overwrite' or 'append'."

    _ensure_dir(ctx)

    if mode == "append" and path.exists():
        existing = path.read_text(encoding="utf-8")
        # Ensure clean separation
        if existing and not existing.endswith("\n"):
            existing += "\n"
        path.write_text(existing + content, encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")

    _update_index(ctx)
    return f"✅ Knowledge '{topic}' saved ({mode})."


def _knowledge_list(ctx: ToolContext) -> str:
    """List all knowledge topics with summaries."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    index_path = kdir / INDEX_FILE

    if index_path.exists():
        return index_path.read_text(encoding="utf-8")

    # No index yet — build it
    if kdir.exists():
        _update_index(ctx)
        if index_path.exists():
            return index_path.read_text(encoding="utf-8")

    return "Knowledge base is empty. Use knowledge_write to add topics."


# --- Tool registration ---

def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="knowledge_read",
            description="Read a topic from the persistent knowledge base on Drive.",
            parameters={
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "Topic name (alphanumeric, hyphens, underscores). E.g. 'browser-automation', 'joi_gotchas'"
                    }
                },
                "required": ["topic"]
            },
            handler=_knowledge_read
        ),
        ToolEntry(
            name="knowledge_write",
            description="Write or append to a knowledge topic. Use for recipes, gotchas, patterns learned from experience.",
            parameters={
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
            handler=_knowledge_write
        ),
        ToolEntry(
            name="knowledge_list",
            description="List all topics in the knowledge base with summaries.",
            parameters={
                "type": "object",
                "properties": {},
                "required": []
            },
            handler=_knowledge_list
        ),
    ]
