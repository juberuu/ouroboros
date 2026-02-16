"""Knowledge base tools — persistent structured learnings on Drive.

Knowledge lives at memory/knowledge/ on Drive. Each topic is a .md file.
_index.md is auto-rebuilt as a table of contents.
"""
from __future__ import annotations

from typing import List

from ouroboros.tools.registry import ToolContext, ToolEntry

KNOWLEDGE_DIR = "memory/knowledge"


def _ensure_dir(ctx: ToolContext):
    """Ensure knowledge directory exists on Drive."""
    d = ctx.drive_path(KNOWLEDGE_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _knowledge_read(ctx: ToolContext, topic: str) -> str:
    """Read a knowledge file by topic name."""
    path = ctx.drive_path(f"{KNOWLEDGE_DIR}/{topic}.md")
    if not path.exists():
        kdir = ctx.drive_path(KNOWLEDGE_DIR)
        if kdir.exists():
            topics = [f.stem for f in kdir.iterdir()
                      if f.suffix == ".md" and f.stem != "_index"]
            available = ", ".join(sorted(topics)) or "none"
            return f"Topic '{topic}' not found. Available: {available}"
        return f"Topic '{topic}' not found. Knowledge base is empty."
    return path.read_text(encoding="utf-8")


def _knowledge_write(ctx: ToolContext, topic: str, content: str,
                     mode: str = "overwrite") -> str:
    """Write or append to a knowledge file, then rebuild index."""
    _ensure_dir(ctx)
    path = ctx.drive_path(f"{KNOWLEDGE_DIR}/{topic}.md")

    if mode == "append" and path.exists():
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing + "\n" + content, encoding="utf-8")
    else:
        path.write_text(content, encoding="utf-8")

    _update_index(ctx)
    return f"✅ Knowledge '{topic}' written ({mode}). {path.stat().st_size} bytes."


def _knowledge_list(ctx: ToolContext) -> str:
    """List all topics in the knowledge base."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    if not kdir.exists():
        return "Knowledge base is empty."
    entries = []
    for f in sorted(kdir.iterdir()):
        if f.suffix == ".md" and f.stem != "_index":
            entries.append(f"- {f.stem} ({f.stat().st_size} bytes)")
    if not entries:
        return "Knowledge base is empty."
    return "Knowledge topics:\n" + "\n".join(entries)


def _update_index(ctx: ToolContext):
    """Rebuild _index.md from current knowledge files."""
    kdir = ctx.drive_path(KNOWLEDGE_DIR)
    if not kdir.exists():
        return
    entries = []
    for f in sorted(kdir.iterdir()):
        if f.suffix == ".md" and f.stem != "_index":
            first_line = ""
            try:
                text = f.read_text(encoding="utf-8").strip()
                first_line = text.split("\n")[0][:100] if text else ""
            except Exception:
                pass
            entries.append(f"- **{f.stem}**: {first_line}")
    index_text = "# Knowledge Base Index\n\n"
    index_text += "\n".join(entries) if entries else "_Empty_"
    (kdir / "_index.md").write_text(index_text, encoding="utf-8")


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("knowledge_read", {
            "name": "knowledge_read",
            "description": (
                "Read a topic from the knowledge base "
                "(persistent structured learnings on Drive)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": (
                            "Topic name (e.g. 'browser_automation', "
                            "'joi_com', 'budget_tracking')"
                        ),
                    },
                },
                "required": ["topic"],
            },
        }, _knowledge_read),
        ToolEntry("knowledge_write", {
            "name": "knowledge_write",
            "description": (
                "Write learnings to a knowledge base topic. "
                "Use after tasks to persist insights and recipes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "Topic name"},
                    "content": {
                        "type": "string",
                        "description": "Content to write (markdown)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "default": "overwrite",
                    },
                },
                "required": ["topic", "content"],
            },
        }, _knowledge_write),
        ToolEntry("knowledge_list", {
            "name": "knowledge_list",
            "description": "List all topics in the knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        }, _knowledge_list),
    ]
