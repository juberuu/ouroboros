"""Ripgrep-backed search helper for the search_code tool.

This module is intentionally policy-agnostic: callers must post-filter every
returned path through Ouroboros's protected/secret gates before surfacing it.
"""

from __future__ import annotations

import json
import fnmatch
import os
import pathlib
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class RgMatch:
    path: pathlib.Path
    line: int
    text: str


def _rg_binary() -> str:
    try:
        from ouroboros.platform_layer import resolve_bundled_ripgrep

        candidate = resolve_bundled_ripgrep()
        if candidate:
            return candidate
    except Exception:
        pass
    candidate = shutil.which("rg")
    return candidate or ""


def search_with_rg(
    search_targets: pathlib.Path | list[pathlib.Path],
    query: str,
    *,
    regex: bool,
    include: str = "",
    max_results: int = 200,
    path_allowed: Callable[[pathlib.Path], bool] | None = None,
) -> tuple[list[RgMatch], bool]:
    """Return matches and whether rg had more output past max_results."""
    rg = _rg_binary()
    if not rg:
        raise FileNotFoundError("rg not found")
    cmd = [rg, "--json", "--line-number", "--color", "never"]
    if not regex:
        cmd.append("--fixed-strings")
    if include:
        cmd.extend(["--glob", include])
    if isinstance(search_targets, list):
        targets = search_targets
    elif search_targets.is_dir():
        try:
            from ouroboros.code_intelligence import SKIP_DIRS
        except Exception:
            SKIP_DIRS = frozenset()
        targets = []
        for dirpath, dirnames, filenames in os.walk(str(search_targets)):
            dirnames[:] = [name for name in sorted(dirnames) if name not in SKIP_DIRS]
            for fname in sorted(filenames):
                path = pathlib.Path(dirpath) / fname
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                if path_allowed is None or path_allowed(path):
                    targets.append(path)
    else:
        targets = [search_targets] if path_allowed is None or path_allowed(search_targets) else []
    if not targets:
        return [], False
    cmd.extend(["--", query])
    cmd.extend(str(path) for path in targets)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout or "").strip()[:500]
        raise RuntimeError(detail or f"rg exited {proc.returncode}")
    matches: list[RgMatch] = []
    truncated = False
    for raw in proc.stdout.splitlines():
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "match":
            continue
        data = event.get("data") or {}
        path_text = ((data.get("path") or {}).get("text") or "").strip()
        if not path_text:
            continue
        path = pathlib.Path(path_text)
        if path_allowed is not None and not path_allowed(path):
            continue
        lines = data.get("lines") or {}
        text = str(lines.get("text") or "").rstrip("\n")
        line = int(data.get("line_number") or 0)
        if len(matches) >= max_results:
            truncated = True
            break
        matches.append(RgMatch(path=path, line=line, text=text.rstrip()))
    return matches, truncated


def format_search_result(
    *,
    display_path: str,
    root_name: str,
    root_path: pathlib.Path,
    query: str,
    regex: bool,
    matches: list[RgMatch],
    truncated: bool,
    max_results: int,
) -> str:
    rendered = [f"{root_name}:{m.path.relative_to(root_path).as_posix()}:{m.line}: {m.text}" for m in matches]
    if not rendered:
        return f"No matches found for {'regex' if regex else 'literal'} `{query}` in {display_path} (ripgrep)."
    header = f"Found {len(rendered)} match{'es' if len(rendered) != 1 else ''} in {display_path} (ripgrep)"
    if truncated:
        header += f" — truncated at {max_results} results"
    return header + "\n\n" + "\n".join(rendered)
