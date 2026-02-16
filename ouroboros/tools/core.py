"""Файловые инструменты: repo_read, repo_list, drive_read, drive_list, drive_write, codebase_digest."""

from __future__ import annotations

import ast
import json
import os
import pathlib
from typing import Any, Dict, List, Tuple

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


# ---------------------------------------------------------------------------
# Send photo to owner
# ---------------------------------------------------------------------------

def _send_photo(ctx: ToolContext, image_base64: str, caption: str = "") -> str:
    """Send a base64-encoded image to the owner's Telegram chat."""
    if not ctx.current_chat_id:
        return "⚠️ No active chat — cannot send photo."

    # Resolve screenshot reference from stash
    actual_b64 = image_base64
    if image_base64 == "__last_screenshot__":
        if not ctx._last_screenshot_b64:
            return "⚠️ No screenshot stored. Take one first with browse_page(output='screenshot')."
        actual_b64 = ctx._last_screenshot_b64

    if not actual_b64 or len(actual_b64) < 100:
        return "⚠️ image_base64 is empty or too short. Take a screenshot first with browse_page(output='screenshot')."

    ctx.pending_events.append({
        "type": "send_photo",
        "chat_id": ctx.current_chat_id,
        "image_base64": actual_b64,
        "caption": caption or "",
    })
    return "OK: photo queued for delivery to owner."


# ---------------------------------------------------------------------------
# Codebase digest
# ---------------------------------------------------------------------------

_SKIP_DIRS = frozenset({
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".tox", "build", "dist",
})


def _extract_python_symbols(file_path: pathlib.Path) -> Tuple[List[str], List[str]]:
    """Extract class and function names from a Python file using AST."""
    try:
        code = file_path.read_text(encoding="utf-8")
        tree = ast.parse(code, filename=str(file_path))
        classes = []
        functions = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                classes.append(node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(node.name)
        return list(dict.fromkeys(classes)), list(dict.fromkeys(functions))
    except Exception:
        return [], []


def _codebase_digest(ctx: ToolContext) -> str:
    """Generate a compact digest of the codebase: files, sizes, classes, functions."""
    repo_dir = ctx.repo_dir
    py_files: List[pathlib.Path] = []
    md_files: List[pathlib.Path] = []
    other_files: List[pathlib.Path] = []

    for dirpath, dirnames, filenames in os.walk(str(repo_dir)):
        # Skip excluded directories
        dirnames[:] = [d for d in sorted(dirnames) if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            p = pathlib.Path(dirpath) / fn
            if not p.is_file():
                continue
            if p.suffix == ".py":
                py_files.append(p)
            elif p.suffix == ".md":
                md_files.append(p)
            elif p.suffix in (".txt", ".cfg", ".toml", ".yml", ".yaml", ".json"):
                other_files.append(p)

    total_lines = 0
    total_functions = 0
    sections: List[str] = []

    # Python files
    for pf in py_files:
        try:
            lines = pf.read_text(encoding="utf-8").splitlines()
            line_count = len(lines)
            total_lines += line_count
            classes, functions = _extract_python_symbols(pf)
            total_functions += len(functions)
            rel = pf.relative_to(repo_dir).as_posix()
            parts = [f"\n== {rel} ({line_count} lines) =="]
            if classes:
                cl = ", ".join(classes[:10])
                if len(classes) > 10:
                    cl += f", ... ({len(classes)} total)"
                parts.append(f"  Classes: {cl}")
            if functions:
                fn = ", ".join(functions[:20])
                if len(functions) > 20:
                    fn += f", ... ({len(functions)} total)"
                parts.append(f"  Functions: {fn}")
            sections.append("\n".join(parts))
        except Exception:
            pass

    # Markdown files
    for mf in md_files:
        try:
            line_count = len(mf.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = mf.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            pass

    # Other config files (just names + sizes)
    for of in other_files:
        try:
            line_count = len(of.read_text(encoding="utf-8").splitlines())
            total_lines += line_count
            rel = of.relative_to(repo_dir).as_posix()
            sections.append(f"\n== {rel} ({line_count} lines) ==")
        except Exception:
            pass

    total_files = len(py_files) + len(md_files) + len(other_files)
    header = f"Codebase Digest ({total_files} files, {total_lines} lines, {total_functions} functions)"
    return header + "\n" + "\n".join(sections)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

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
        ToolEntry("send_photo", {
            "name": "send_photo",
            "description": (
                "Send a base64-encoded image (PNG) to the owner's Telegram chat. "
                "Use after browse_page(output='screenshot') or browser_action(action='screenshot'). "
                "Pass the base64 string from the screenshot result as image_base64."
            ),
            "parameters": {"type": "object", "properties": {
                "image_base64": {"type": "string", "description": "Base64-encoded PNG image data"},
                "caption": {"type": "string", "description": "Optional caption for the photo"},
            }, "required": ["image_base64"]},
        }, _send_photo),
        ToolEntry("codebase_digest", {
            "name": "codebase_digest",
            "description": "Get a compact digest of the entire codebase: files, sizes, classes, functions. One call instead of many repo_read calls.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _codebase_digest),
    ]
