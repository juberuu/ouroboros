"""Git command classifiers for shell-tool safety guards."""

from __future__ import annotations

import pathlib
from typing import Any

from ouroboros.shell_parse import (
    shell_argv,
    shell_command_string,
    strip_leading_env_assignments,
    unwrap_env_argv,
)
from ouroboros.utils import safe_relpath

GIT_READONLY_SUBCOMMANDS = frozenset([
    "status", "diff", "log", "show", "ls-files", "describe", "rev-parse",
    "cat-file", "shortlog", "version", "help", "blame", "grep", "reflog",
    "for-each-ref", "rev-list", "show-ref",
])
_BRANCH_MUTATING_FLAGS = frozenset({
    "-d", "-D", "-m", "-M", "-c", "-C", "-f", "-u",
    "--delete", "--move", "--copy", "--force", "--set-upstream-to",
    "--unset-upstream", "--edit-description", "--track", "--no-track",
})
_BRANCH_READONLY_FLAGS = frozenset({
    "-l", "--list", "-a", "--all", "-r", "--remotes", "-v", "-vv",
    "--verbose", "--show-current", "--contains", "--merged", "--no-merged",
    "--points-at", "--format", "--sort", "--color", "--no-color",
    "--column", "--no-column", "--abbrev", "--no-abbrev", "--ignore-case",
})
_TAG_MUTATING_FLAGS = frozenset({
    "-a", "-s", "-u", "-d", "-v", "-f", "-m", "-F",
    "--annotate", "--sign", "--local-user", "--delete", "--verify",
    "--force", "--message", "--file", "--cleanup", "--create-reflog",
})
_TAG_READONLY_FLAGS = frozenset({
    "-l", "--list", "-n", "--sort", "--format", "--points-at",
    "--contains", "--merged", "--no-merged", "--column", "--no-column",
    "--ignore-case", "--color", "--no-color",
})


def _git_subcommand_and_args(cmd_parts: list[str]) -> tuple[str, list[str]]:
    parts = strip_leading_env_assignments([str(p) for p in cmd_parts])
    if not parts or pathlib.PurePath(parts[0]).name.lower() != "git":
        return "", []
    i = 1
    while i < len(parts):
        part = parts[i]
        if part.startswith("-"):
            i += 2 if part in ("-C", "-c", "--git-dir", "--work-tree") else 1
            continue
        return part.lower(), parts[i + 1:]
    return "", []


def _git_option_value_flags(args: list[str]) -> set[int]:
    value_taking_flags = {
        "--contains", "--merged", "--no-merged", "--points-at", "--format",
        "--sort", "--color", "--column", "--abbrev", "-n", "-m", "-F", "-u",
        "--message", "--file", "--local-user", "--set-upstream-to",
    }
    return {idx + 1 for idx, arg in enumerate(args[:-1]) if arg in value_taking_flags}


def _short_flag_chars(arg: str) -> set[str]:
    text = str(arg or "")
    return set(text[1:]) if text.startswith("-") and not text.startswith("--") else set()


def _git_branch_readonly(args: list[str]) -> bool:
    value_indexes = _git_option_value_flags(args)
    read_hint = not args
    explicit_list = False
    positionals = []
    for idx, arg in enumerate(args):
        if idx in value_indexes:
            continue
        if arg in _BRANCH_MUTATING_FLAGS or _short_flag_chars(arg) & set("dDmMcCfFu"):
            return False
        if arg.startswith("--") and "=" in arg:
            flag = arg.split("=", 1)[0]
            if flag in _BRANCH_MUTATING_FLAGS:
                return False
            explicit_list = explicit_list or flag == "--list"
            read_hint = read_hint or flag in _BRANCH_READONLY_FLAGS
            continue
        if arg.startswith("-"):
            chars = _short_flag_chars(arg)
            if arg == "--list" or "l" in chars:
                explicit_list = True
            if arg in _BRANCH_READONLY_FLAGS or chars <= set("alrv"):
                read_hint = True
                continue
            return False
        positionals.append(arg)
    return bool(read_hint and (not positionals or explicit_list))


def _git_tag_readonly(args: list[str]) -> bool:
    value_indexes = _git_option_value_flags(args)
    read_hint = not args
    positionals = []
    for idx, arg in enumerate(args):
        if idx in value_indexes:
            continue
        if arg in _TAG_MUTATING_FLAGS or _short_flag_chars(arg) & set("asudvfmF"):
            return False
        if arg.startswith("--") and "=" in arg:
            flag = arg.split("=", 1)[0]
            if flag in _TAG_MUTATING_FLAGS:
                return False
            read_hint = read_hint or flag in _TAG_READONLY_FLAGS
            continue
        if arg.startswith("-"):
            chars = _short_flag_chars(arg)
            if arg in _TAG_READONLY_FLAGS or chars <= set("ln"):
                read_hint = True
                continue
            return False
        positionals.append(arg)
    return read_hint or not positionals


def _git_invocation_block_reason(parts: list[str], *, allow_network: bool = True) -> str:
    subcmd, args = _git_subcommand_and_args(parts)
    if not subcmd or subcmd in GIT_READONLY_SUBCOMMANDS:
        return ""
    if subcmd == "branch" and _git_branch_readonly(args):
        return ""
    if subcmd == "tag" and _git_tag_readonly(args):
        return ""
    if subcmd == "ls-remote":
        return "" if allow_network else "task_contract.allowed_resources.network=false blocks git ls-remote"
    return f"git {subcmd}"


def run_shell_git_block_reason(raw_cmd: Any, *, allow_network: bool = True) -> str:
    argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
    if not argv:
        return ""
    first = pathlib.PurePath(argv[0]).name.lower()
    if first in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        return run_shell_git_block_reason(inline, allow_network=allow_network) if inline else ""
    for idx, token in enumerate(argv):
        if pathlib.PurePath(str(token)).name.lower() == "git":
            reason = _git_invocation_block_reason(argv[idx:], allow_network=allow_network)
            if reason:
                return reason
    return ""


def _resolve_workspace_shell_cwd(active_root: pathlib.Path, cwd: str = "") -> pathlib.Path:
    root = pathlib.Path(active_root).resolve(strict=False)
    if cwd and str(cwd).strip() not in ("", ".", "./"):
        raw = pathlib.Path(str(cwd)).expanduser()
        return raw.resolve(strict=False) if raw.is_absolute() else (root / safe_relpath(str(cwd))).resolve(strict=False)
    return root


def workspace_git_safety_violation(
    raw_cmd: Any,
    *,
    active_root: pathlib.Path,
    cwd: str = "",
    allow_network: bool = True,
) -> str:
    root = pathlib.Path(active_root).resolve(strict=False)
    base = _resolve_workspace_shell_cwd(root, cwd)
    try:
        base.relative_to(root)
        base_inside_root = True
    except Exception:
        base_inside_root = False
    argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
    if not argv:
        return ""
    first = pathlib.PurePath(argv[0]).name.lower()
    if first in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        return workspace_git_safety_violation(
            inline,
            active_root=root,
            cwd=str(base) if inline else "",
            allow_network=allow_network,
        ) if inline else ""
    for idx, token in enumerate(argv):
        if pathlib.PurePath(str(token)).name.lower() != "git":
            continue
        parts = argv[idx:]
        saw_root_selector = False
        j = 1
        while j < len(parts):
            part = parts[j]
            if part in {"-C", "--git-dir", "--work-tree"} and j + 1 < len(parts):
                saw_root_selector = True
                try:
                    target = pathlib.Path(parts[j + 1])
                    if not target.is_absolute():
                        target = base / target
                    target.resolve(strict=False).relative_to(root)
                except Exception:
                    return f"git {part} escapes the active workspace"
                j += 2
                continue
            if part.startswith("--git-dir=") or part.startswith("--work-tree="):
                saw_root_selector = True
                value = part.split("=", 1)[1]
                try:
                    target = pathlib.Path(value)
                    if not target.is_absolute():
                        target = base / target
                    target.resolve(strict=False).relative_to(root)
                except Exception:
                    return "git root selector escapes the active workspace"
                j += 1
                continue
            if part == "-c":
                j += 2
                continue
            if part.startswith("-"):
                j += 1
                continue
            break
        if not base_inside_root and not saw_root_selector:
            return "git cwd escapes the active workspace"
        reason = _git_invocation_block_reason(parts, allow_network=allow_network)
        if reason:
            return reason
    return ""
