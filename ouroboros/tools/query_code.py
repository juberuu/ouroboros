"""Read-only structured code queries over the deterministic code inventory."""

from __future__ import annotations

import json
import pathlib
import re
from typing import Any, List

from ouroboros.protected_artifacts import block_reason_for_path
from ouroboros.tools.registry import ToolContext, ToolEntry, active_repo_dir_for, system_repo_dir_for


_OPS = (
    "symbols",
    "definition",
    "references",
    "callers",
    "callees",
    "impact",
    "structural",
    "relevant_files",
)
_MAX_LIMIT = 200


def _safe_path(repo_root: pathlib.Path, path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    if not text or text == ".":
        return ""
    target = (repo_root / text).resolve(strict=False)
    try:
        return target.relative_to(repo_root.resolve(strict=False)).as_posix()
    except ValueError as exc:
        raise ValueError(f"path escapes root: {path}") from exc


def _visible_file(ctx: ToolContext, repo_root: pathlib.Path, rel_path: str) -> bool:
    try:
        target = (repo_root / rel_path).resolve(strict=False)
    except Exception:
        return False
    try:
        from ouroboros.tools.core import _is_local_readonly_subagent, _is_subagent_secret_repo_target

        if _is_local_readonly_subagent(ctx) and _is_subagent_secret_repo_target(target, repo_root):
            return False
    except Exception:
        pass
    return not (
        block_reason_for_path(ctx, target, "read_bytes")
        or block_reason_for_path(ctx, target, "static_introspection")
    )


def _inventory_rows(ctx: ToolContext, inventory: Any, repo_root: pathlib.Path, opts: dict[str, Any]) -> list[str]:
    from ouroboros.code_intelligence import (
        impact_files,
        relevant_files,
        symbol_callees,
        symbol_callers,
        symbol_definitions,
        symbol_references,
    )

    op = str(opts.get("op") or "")
    query = str(opts.get("query") or "")
    path = str(opts.get("path") or "")
    kind = str(opts.get("kind") or "any")
    depth = int(opts.get("depth") or 1)
    limit = int(opts.get("limit") or 40)
    offset = int(opts.get("offset") or 0)
    rows: list[str] = []
    if op in {"symbols", "definition"}:
        for file, symbol in symbol_definitions(inventory, query, path=path, kind=kind or "any"):
            if _visible_file(ctx, repo_root, file.path):
                rows.append(f"{file.path}:{symbol.line_start} {symbol.kind} {symbol.signature or symbol.name}")
    elif op == "references":
        for file, ref in symbol_references(inventory, query, path=path):
            if _visible_file(ctx, repo_root, file.path):
                rows.append(f"{file.path}:{ref.line} {query}{' in ' + ref.enclosing if ref.enclosing else ''}")
    elif op in {"callers", "callees"}:
        iterator = symbol_callers(inventory, query, path=path) if op == "callers" else symbol_callees(inventory, query, path=path)
        for file, call in iterator:
            if _visible_file(ctx, repo_root, file.path):
                rows.append(f"{file.path}:{call.line} {call.enclosing + ' -> ' if call.enclosing else ''}{call.name}")
    elif op == "impact":
        for file, reason in impact_files(inventory, path or query, depth=depth):
            if _visible_file(ctx, repo_root, file.path):
                rows.append(f"{file.path}  {reason}")
    elif op == "relevant_files":
        for idx, (file, score, reason) in enumerate(relevant_files(inventory, query, limit=min(_MAX_LIMIT, offset + limit)), 1):
            if _visible_file(ctx, repo_root, file.path):
                top_symbols = ", ".join(symbol.name for symbol in file.symbols[:5])
                rows.append(f"{idx}. {file.path} score={score:.2f} reason={reason}{' symbols=' + top_symbols if top_symbols else ''}")
    return rows


def _structural(ctx: ToolContext, repo_root: pathlib.Path, query: str, path: str, lang: str, limit: int) -> list[str]:
    # Conservative first step: use tree-sitter when available, otherwise a Python
    # ast fallback plus literal matching. Query may be a tree-sitter S-expression
    # like "(function_definition)" or a node type such as "FunctionDef".
    import ast

    def _query_node_type(raw: str) -> str:
        text = str(raw or "").strip()
        if text.startswith("("):
            match = re.match(r"\(\s*([A-Za-z_][\w-]*)", text)
            return match.group(1) if match else ""
        return text

    ts_node_type = _query_node_type(query)

    def _tree_sitter_rows(fp: pathlib.Path, rel: str, text: str) -> list[str]:
        if not ts_node_type:
            return []
        language = {
            ".py": "python", ".js": "javascript", ".jsx": "javascript",
            ".ts": "typescript", ".tsx": "typescript",
        }.get(fp.suffix, "")
        if not language:
            return []
        try:
            from tree_sitter_language_pack import get_parser  # type: ignore

            parser = get_parser(language)
            tree = parser.parse(text.encode("utf-8"))
        except Exception:
            return []
        found: list[str] = []
        stack = [tree.root_node]
        while stack:
            node = stack.pop()
            if node.type == ts_node_type:
                found.append(f"{rel}:{int(node.start_point[0]) + 1} {node.type}")
            stack.extend(reversed(list(node.children)))
        return found

    scope = (repo_root / (path or ".")).resolve(strict=False)
    candidates = [scope] if scope.is_file() else sorted(scope.rglob("*"))
    rows: list[str] = []
    needle = str(query or "").strip()
    for fp in candidates:
        if len(rows) >= min(max(1, limit), _MAX_LIMIT):
            break
        if not fp.is_file() or fp.suffix not in {".py", ".js", ".jsx", ".ts", ".tsx"}:
            continue
        try:
            rel = fp.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        if not _visible_file(ctx, repo_root, rel):
            continue
        if lang not in {"", "any"} and lang and {
            ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript", ".tsx": "typescript"
        }.get(fp.suffix, "") != lang:
            continue
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        ts_rows = _tree_sitter_rows(fp, rel, text)
        if ts_rows:
            rows.extend(ts_rows)
            continue
        if fp.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                continue
            # Minimal structural fallback: if the query names an AST class, match it;
            # otherwise match exact source lines without storing source in the index.
            for node in ast.walk(tree):
                if node.__class__.__name__.casefold() == needle.casefold():
                    rows.append(f"{rel}:{int(getattr(node, 'lineno', 0) or 0)} {node.__class__.__name__}")
        for lineno, line in enumerate(text.splitlines(), 1):
            if needle and needle in line:
                rows.append(f"{rel}:{lineno} {line.strip()}")
                if len(rows) >= min(max(1, limit), _MAX_LIMIT):
                    break
    if not rows and lang in {"javascript", "typescript"}:
        rows.append("note: structural tree-sitter query for JS/TS requires tree-sitter; fallback found no literal matches")
    return rows


def _query_code(ctx: ToolContext, op: str, **options: Any) -> str:
    query = str(options.get("query") or "")
    path = str(options.get("path") or "")
    lang = str(options.get("lang") or "any")
    kind = str(options.get("kind") or "any")
    depth = int(options.get("depth") or 1)
    root = str(options.get("root") or "active_workspace")
    limit = int(options.get("limit") or 40)
    offset = int(options.get("offset") or 0)
    op = str(op or "").strip()
    if op not in _OPS:
        return f"⚠️ TOOL_ARG_ERROR (query_code): op must be one of {', '.join(_OPS)}."
    if op != "symbols" and not str(query or "").strip():
        return f"⚠️ TOOL_ARG_ERROR (query_code): op '{op}' requires query."
    try:
        normalized_root = str(root or "active_workspace").strip() or "active_workspace"
        if normalized_root == "system_repo":
            try:
                from ouroboros.tool_access import active_tool_profile

                if active_tool_profile(ctx) == "acting_subagent":
                    return "⚠️ TOOL_ACCESS_BLOCKED: query_code root=system_repo is not available to acting subagents."
            except Exception:
                pass
            repo_root = pathlib.Path(system_repo_dir_for(ctx)).resolve(strict=False)
        elif normalized_root == "active_workspace":
            repo_root = pathlib.Path(active_repo_dir_for(ctx)).resolve(strict=False)
        else:
            raise ValueError("root must be active_workspace or system_repo")
        scoped_path = _safe_path(repo_root, path)
    except ValueError as exc:
        return f"⚠️ TOOL_ARG_ERROR (query_code): {exc}"

    limit = min(max(1, int(limit or 40)), _MAX_LIMIT)
    offset = max(0, int(offset or 0))

    try:
        if op == "structural":
            rows = _structural(ctx, repo_root, query, scoped_path, str(lang or "any"), limit)
        else:
            from ouroboros.code_intelligence import build_code_inventory
            from ouroboros.protected_artifacts import protected_artifact_paths

            exclude_paths: list[pathlib.Path] = list(protected_artifact_paths(ctx))
            persist = True
            if exclude_paths:
                persist = False
            try:
                from ouroboros.tools.core import _is_local_readonly_subagent, _is_subagent_secret_repo_target

                if _is_local_readonly_subagent(ctx):
                    persist = False
                    exclude_paths = [
                        p for p in repo_root.rglob("*")
                        if _is_subagent_secret_repo_target(p, repo_root)
                    ]
            except Exception:
                pass
            inventory = build_code_inventory(repo_root, drive_root=pathlib.Path(ctx.drive_root), persist=persist, exclude_paths=exclude_paths)
            inventory.files = [file for file in inventory.files if _visible_file(ctx, repo_root, file.path)]
            rows = _inventory_rows(ctx, inventory, repo_root, {
                "op": op, "query": query, "path": scoped_path, "kind": kind,
                "depth": depth, "limit": limit, "offset": offset,
            })
    except Exception as exc:
        return f"⚠️ QUERY_CODE_ERROR: {type(exc).__name__}: {exc}"

    total = len(rows)
    shown = rows[offset:offset + limit]
    next_offset = offset + limit
    label = query or scoped_path or "."
    if not shown:
        return f"No results for op `{op}` `{label}`. Hint: check the symbol/path or use search_code for plain text."
    header = f"{op} `{label}` — {len(shown)} of {total}"
    if next_offset < total:
        header += f" — next offset={next_offset}"
    return header + "\n\n" + "\n".join(shown)


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry("query_code", {
            "name": "query_code",
            "description": (
                "Read-only structured code intelligence over the active workspace. "
                "Use search_code for plain text/regex search. Ops: symbols, definition, "
                "references, callers, callees, impact, structural, relevant_files. "
                "Returns compact file:line anchors and signatures/snippets, never full bodies."
            ),
            "parameters": {"type": "object", "properties": {
                "op": {"type": "string", "enum": list(_OPS), "description": "Query operation."},
                "query": {"type": "string", "default": "", "description": "Symbol, structural pattern, or task text."},
                "path": {"type": "string", "default": "", "description": "Optional file/dir scope or definition disambiguator."},
                "lang": {"type": "string", "enum": ["python", "javascript", "typescript", "any"], "default": "any"},
                "kind": {"type": "string", "enum": ["function", "async_function", "class", "constant", "any"], "default": "any"},
                "depth": {"type": "integer", "default": 1, "description": "Graph depth for impact."},
                "root": {"type": "string", "enum": ["active_workspace", "system_repo"], "default": "active_workspace"},
                "limit": {"type": "integer", "default": 40},
                "offset": {"type": "integer", "default": 0},
            }, "required": ["op"]},
        }, _query_code, timeout_sec=120),
    ]
