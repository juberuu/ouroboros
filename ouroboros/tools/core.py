"""Файловые инструменты: repo_read, repo_list, drive_read, drive_list, drive_write."""

from __future__ import annotations

import json
import pathlib
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import read_text, safe_relpath


def _list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> List[str]:
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


def _repo_read(ctx: ToolContext, path: str) -> str:
    return read_text(ctx.repo_path(path))


def _repo_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(_list_dir(ctx.repo_dir, dir, max_entries), ensure_ascii=False, indent=2)


def _drive_read(ctx: ToolContext, path: str) -> str:
    return read_text(ctx.drive_path(path))


def _drive_list(ctx: ToolContext, dir: str = ".", max_entries: int = 500) -> str:
    return json.dumps(_list_dir(ctx.drive_root, dir, max_entries), ensure_ascii=False, indent=2)


def _drive_write(ctx: ToolContext, path: str, content: str, mode: str = "overwrite") -> str:
    p = ctx.drive_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if mode == "overwrite":
        p.write_text(content, encoding="utf-8")
    else:
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    return f"OK: wrote {mode} {path} ({len(content)} chars)"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("repo_read", {
            "name": "repo_read",
            "description": "Read a UTF-8 text file from the GitHub repo (relative path).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }, _repo_read),
        ToolEntry("repo_list", {
            "name": "repo_list",
            "description": "List files under a repo directory (relative path).",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 500},
            }, "required": []},
        }, _repo_list),
        ToolEntry("drive_read", {
            "name": "drive_read",
            "description": "Read a UTF-8 text file from Google Drive (relative to MyDrive/Ouroboros/).",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        }, _drive_read),
        ToolEntry("drive_list", {
            "name": "drive_list",
            "description": "List files under a Drive directory.",
            "parameters": {"type": "object", "properties": {
                "dir": {"type": "string", "default": "."},
                "max_entries": {"type": "integer", "default": 500},
            }, "required": []},
        }, _drive_list),
        ToolEntry("drive_write", {
            "name": "drive_write",
            "description": "Write a UTF-8 text file on Google Drive.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
            }, "required": ["path", "content"]},
        }, _drive_write),
    ]
