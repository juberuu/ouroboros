"""
Ouroboros — Tool registry (SSOT).

Plugin architecture: each module in tools/ exports get_tools().
ToolRegistry collects all tools, provides schemas() and execute().
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ouroboros.runtime_mode_policy import (
    FROZEN_CONTRACT_PATH_PREFIXES,
    PROTECTED_RUNTIME_PATHS,
    SAFETY_CRITICAL_PATHS,
    core_patch_notice,
    is_protected_runtime_path,
    mode_allows_protected_write,
    protected_paths_in,
    protected_write_block_message,
)
from ouroboros.utils import safe_relpath

log = logging.getLogger(__name__)

_PROTECTED_RUNTIME_PATHS_LOWER = frozenset(
    p.lower() for p in PROTECTED_RUNTIME_PATHS
) | frozenset(prefix.lower() for prefix in FROZEN_CONTRACT_PATH_PREFIXES)

_SHELL_WRITE_INDICATORS = (
    "rm ", "rm\t", ">", "sed -i", "tee ", "truncate",
    "mv ", "cp ", "chmod ", "chown ", "unlink ", "delete", "trash",
    "rsync ", "write_text", "open(", ".write(", ".writelines(",
)

# Git via run_shell: only truly read-only subcommands allowed
_GIT_READONLY_SUBCOMMANDS = frozenset([
    "status", "diff", "log", "show", "ls-files",
    "describe", "rev-parse", "cat-file",
    "shortlog", "version", "help", "blame",
    "grep", "reflog", "fetch",
])

_SHELL_WRAPPERS = frozenset(["bash", "sh", "dash", "zsh", "env"])


def _is_safety_critical_path(path: str) -> bool:
    """Check if a normalized path refers to a safety-critical file.

    ``SAFETY_CRITICAL_PATHS`` is declared with forward slashes
    (``ouroboros/safety.py``). On Windows ``os.path.normpath`` flips
    ``/`` to ``\\``, which makes the literal-set lookup miss. Normalise
    to POSIX form BEFORE the set check so the hardcoded sandbox
    behaves identically on all three supported OSes.
    """
    cleaned = path.strip().lstrip("./").replace("\\", "/")
    # ``PurePosixPath.as_posix()`` collapses redundant ``./`` without
    # re-introducing backslashes (unlike ``os.path.normpath``).
    normalized = pathlib.PurePosixPath(cleaned).as_posix()
    return normalized in SAFETY_CRITICAL_PATHS


def _revert_protected_files(repo_dir, *, runtime_mode: str = "advanced") -> list:
    """After claude_code_edit, revert protected files unless pro mode is active."""
    if mode_allows_protected_write(runtime_mode):
        return []
    try:
        unstaged_diff = subprocess.run(
            ["git", "diff", "--name-only"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        staged_diff = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=str(repo_dir), capture_output=True, text=True, timeout=5,
        )
        if unstaged_diff.returncode != 0 and staged_diff.returncode != 0:
            return []
        modified = set()
        if unstaged_diff.returncode == 0:
            modified.update(unstaged_diff.stdout.strip().splitlines())
        if staged_diff.returncode == 0:
            modified.update(staged_diff.stdout.strip().splitlines())
        reverted = []
        for rel in sorted(modified):
            if is_protected_runtime_path(rel):
                subprocess.run(
                    ["git", "reset", "HEAD", "--", rel],
                    cwd=str(repo_dir), capture_output=True, timeout=5,
                )
                subprocess.run(
                    ["git", "checkout", "--", rel],
                    cwd=str(repo_dir), capture_output=True, timeout=5,
                )
                reverted.append(rel)
        return reverted
    except Exception:
        return []


def _extract_git_subcommand(cmd_parts: list) -> str:
    """Extract the git subcommand from a parsed command list.

    Handles: git status, git -C /path status, git --no-pager log, etc.
    """
    if not cmd_parts:
        return ""
    parts = [str(p) for p in cmd_parts]
    if parts[0] != "git":
        return ""
    i = 1
    while i < len(parts):
        p = parts[i]
        if p.startswith("-"):
            if p in ("-C", "--git-dir", "--work-tree"):
                i += 2
            else:
                i += 1
        else:
            return p
    return ""


@dataclass
class BrowserState:
    """Per-task browser lifecycle state (Playwright). Isolated from generic ToolContext."""

    pw_instance: Any = None
    browser: Any = None
    page: Any = None
    last_screenshot_b64: Optional[str] = None


@dataclass
class ToolContext:
    """Tool execution context — passed from the agent before each task."""

    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"
    pending_events: List[Dict[str, Any]] = field(default_factory=list)
    current_chat_id: Optional[int] = None
    current_task_type: Optional[str] = None
    pending_restart_reason: Optional[str] = None
    last_push_succeeded: bool = False
    emit_progress_fn: Callable[[str], None] = field(default=lambda _: None)

    # LLM-driven model/effort switch (set by switch_model tool, read by loop.py)
    active_model_override: Optional[str] = None
    active_effort_override: Optional[str] = None
    active_use_local_override: Optional[bool] = None

    # Per-task browser state
    browser_state: BrowserState = field(default_factory=BrowserState)

    # Budget tracking (set by loop.py for real-time usage events)
    event_queue: Optional[Any] = None
    task_id: Optional[str] = None

    # Conversation messages (set by loop.py so safety checks have context)
    messages: Optional[List[Dict[str, Any]]] = None

    # Task depth for fork bomb protection
    task_depth: int = 0

    # True when running inside handle_chat_direct (not a queued worker task)
    is_direct_chat: bool = False

    # Pre-commit review state (reset per-commit, carried across review rounds)
    _review_advisory: List[Any] = field(default_factory=list)
    _review_iteration_count: int = 0
    _review_history: list = field(default_factory=list)

    def repo_path(self, rel: str) -> pathlib.Path:
        resolved = (self.repo_dir / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(self.repo_dir.resolve())
        except ValueError:
            raise ValueError(f"Path escapes repo_dir boundary: {rel}")
        return resolved

    def drive_path(self, rel: str) -> pathlib.Path:
        resolved = (self.drive_root / safe_relpath(rel)).resolve()
        try:
            resolved.relative_to(self.drive_root.resolve())
        except ValueError:
            raise ValueError(f"Path escapes drive_root boundary: {rel}")
        return resolved

    def drive_logs(self) -> pathlib.Path:
        return (self.drive_root / "logs").resolve()


@dataclass
class ToolEntry:
    """Single tool descriptor: name, schema, handler, metadata."""

    name: str
    schema: Dict[str, Any]
    handler: Callable  # fn(ctx: ToolContext, **args) -> str
    is_code_tool: bool = False
    timeout_sec: int = 360


CORE_TOOL_NAMES = {
    "repo_read", "repo_list", "repo_write", "repo_write_commit", "repo_commit",
    "data_read", "data_list", "data_write",
    "run_shell", "claude_code_edit",
    "ensure_claude_cli",
    "git_status", "git_diff",
    "pull_from_remote", "restore_to_head", "revert_commit",
    "schedule_task", "wait_for_task", "get_task_result",
    "set_tool_timeout",
    "update_scratchpad", "update_identity",
    "chat_history", "web_search",
    "send_user_message", "switch_model",
    "request_restart", "promote_to_stable",
    "knowledge_read", "knowledge_write", "knowledge_list",
    "browse_page", "browser_action", "analyze_screenshot",
}


class ToolRegistry:
    """Ouroboros tool registry (SSOT).

    To add a tool: create a module in ouroboros/tools/,
    export get_tools() -> List[ToolEntry].
    """

    def __init__(self, repo_dir: pathlib.Path, drive_root: pathlib.Path):
        self._entries: Dict[str, ToolEntry] = {}
        self._ctx = ToolContext(repo_dir=repo_dir, drive_root=drive_root)
        self._load_modules()

    _FROZEN_TOOL_MODULES = [
        "a2a", "browser", "ci", "claude_advisory_review", "compact_context", "control",
        "core", "evolution_stats", "git", "git_rollback", "github", "health",
        "knowledge", "memory_tools", "plan_review", "review", "search", "shell",
        # Phase 3 three-layer refactor: external skill surface
        # (list_skills / review_skill / skill_exec / toggle_skill).
        "skill_exec",
        "tool_discovery", "vision",
    ]

    def _load_modules(self) -> None:
        """Auto-discover tool modules in ouroboros/tools/ that export get_tools()."""
        import importlib
        import logging
        import sys

        if getattr(sys, 'frozen', False):
            module_names = self._FROZEN_TOOL_MODULES
        else:
            import pkgutil
            import ouroboros.tools as tools_pkg
            module_names = [
                m for _, m, _ in pkgutil.iter_modules(tools_pkg.__path__)
                if not m.startswith("_") and m != "registry"
            ]

        for modname in module_names:
            try:
                mod = importlib.import_module(f"ouroboros.tools.{modname}")
                if hasattr(mod, "get_tools"):
                    for entry in mod.get_tools():
                        self._entries[entry.name] = entry
            except Exception:
                logging.getLogger(__name__).warning(
                    "Failed to load tool module %s", modname, exc_info=True)

    def set_context(self, ctx: ToolContext) -> None:
        self._ctx = ctx

    def register(self, entry: ToolEntry) -> None:
        """Register a new tool (for extension by Ouroboros)."""
        self._entries[entry.name] = entry

    # --- Contract ---

    def available_tools(self) -> List[str]:
        return [e.name for e in self._entries.values()]

    def schemas(self, core_only: bool = False) -> List[Dict[str, Any]]:
        built_in = [{"type": "function", "function": e.schema} for e in self._entries.values()]
        # Include live extension-registered tool schemas so the normal
        # tool-policy/enable_tools path can surface ``ext.<skill>.<name>``
        # entries instead of leaving them manually dispatch-only.
        try:
            from ouroboros.extension_loader import (
                _tools as _ext_tools,
                _lock as _ext_lock,
                is_extension_live as _ext_is_live,
            )
            with _ext_lock:
                extension_schemas = [
                    {
                        "type": "function",
                        "function": {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "parameters": tool.get("schema", {"type": "object", "properties": {}}),
                        },
                    }
                    for tool in _ext_tools.values()
                    if _ext_is_live(str(tool.get("skill") or ""), pathlib.Path(self._ctx.drive_root))
                ]
        except Exception:
            extension_schemas = []

        if not core_only:
            return built_in + extension_schemas
        # Core tools + meta-tools for discovering/enabling extended tools
        result = []
        for e in self._entries.values():
            if e.name in CORE_TOOL_NAMES or e.name in ("list_available_tools", "enable_tools"):
                result.append({"type": "function", "function": e.schema})
        # Keep live extension tools enumerable in core-mode too so the
        # loop can discover them through the standard registry surface.
        return result + extension_schemas

    def list_non_core_tools(self) -> List[Dict[str, str]]:
        """Return name+description of all non-core tools."""
        result = []
        for e in self._entries.values():
            if e.name not in CORE_TOOL_NAMES:
                desc = e.schema.get("description", "No description")
                result.append({"name": e.name, "description": desc})
        try:
            from ouroboros.extension_loader import (
                _tools as _ext_tools,
                _lock as _ext_lock,
                is_extension_live as _ext_is_live,
            )
            with _ext_lock:
                for tool in _ext_tools.values():
                    skill_name = str(tool.get("skill") or "")
                    if not skill_name or not _ext_is_live(skill_name, pathlib.Path(self._ctx.drive_root)):
                        continue
                    result.append(
                        {
                            "name": str(tool.get("name") or ""),
                            "description": str(tool.get("description") or "No description"),
                        }
                    )
        except Exception:
            pass
        return result

    def get_schema_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Return the full schema for a specific tool."""
        entry = self._entries.get(name)
        if entry:
            return {"type": "function", "function": entry.schema}
        if name.startswith("ext."):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool, is_extension_live as _ext_is_live
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None
            if ext_tool and _ext_is_live(str(ext_tool.get("skill") or ""), pathlib.Path(self._ctx.drive_root)):
                return {
                    "type": "function",
                    "function": {
                        "name": ext_tool["name"],
                        "description": ext_tool.get("description", ""),
                        "parameters": ext_tool.get("schema", {"type": "object", "properties": {}}),
                    },
                }
        return None

    def get_timeout(self, name: str) -> int:
        """Return timeout_sec for the named tool (default 360)."""
        entry = self._entries.get(name)
        if entry is not None:
            return entry.timeout_sec
        # Phase 5: extension-registered tools carry their own timeout_sec
        # in the loader's tool descriptor.
        if name.startswith("ext."):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None
            if ext_tool:
                return int(ext_tool.get("timeout_sec") or 60)
        return 360

    def execute(self, name: str, args: Dict[str, Any]) -> str:
        entry = self._entries.get(name)
        ext_tool = None
        if entry is None and name.startswith("ext."):
            try:
                from ouroboros.extension_loader import get_tool as _ext_get_tool
                ext_tool = _ext_get_tool(name)
            except Exception:
                ext_tool = None

        # --- Hardcoded Sandbox Protections ---

        # Runtime-mode gating:
        # - light blocks repo self-modification entirely;
        # - advanced may evolve the application layer but cannot edit protected
        #   core/contracts/release surfaces;
        # - pro may touch those surfaces, but the git commit path must pass the
        #   extra core-patch review gate before the commit lands.
        try:
            from ouroboros.config import get_runtime_mode as _get_runtime_mode
            _runtime_mode = _get_runtime_mode()
        except Exception:
            _runtime_mode = "advanced"

        if entry is None:
            if ext_tool and callable(ext_tool.get("handler")):
                try:
                    from ouroboros.extension_loader import (
                        is_extension_live as _ext_is_live,
                        unload_extension as _ext_unload,
                    )
                except Exception:
                    _ext_is_live = None
                    _ext_unload = None
                skill_name = str(ext_tool.get("skill") or "")
                if _runtime_mode == "light":
                    if skill_name and callable(_ext_unload):
                        _ext_unload(skill_name)
                    return (
                        "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light disables "
                        "in-process extension tools. Switch to 'advanced' or "
                        "'pro' to re-enable extension dispatch."
                    )
                if skill_name and callable(_ext_is_live) and not _ext_is_live(skill_name, pathlib.Path(self._ctx.drive_root)):
                    if callable(_ext_unload):
                        _ext_unload(skill_name)
                    return (
                        f"⚠️ EXTENSION_NOT_LIVE: extension {skill_name!r} is "
                        "not allowed to dispatch right now."
                    )
                handler = ext_tool["handler"]
                try:
                    result = handler(self._ctx, **(args or {}))
                except TypeError:
                    result = handler(**(args or {}))
                except Exception as exc:
                    return (
                        f"⚠️ extension tool {name!r} failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                return result if isinstance(result, str) else str(result)
            return f"⚠️ Unknown tool: {name}. Available: {', '.join(sorted(self._entries.keys()))}"
        _REPO_MUTATION_TOOLS = frozenset(
            {
                "repo_write",
                "repo_write_commit",
                "repo_commit",
                "str_replace_editor",
                "claude_code_edit",
                "revert_commit",
                "pull_from_remote",
                "restore_to_head",
                "rollback_to_target",
                "promote_to_stable",
                # PR integration tools — they check out branches,
                # cherry-pick, and stage merges. All of them mutate
                # the local working tree / refs and must not run
                # when ``runtime_mode=light``.
                "fetch_pr_ref",
                "create_integration_branch",
                "cherry_pick_pr_commits",
                "stage_adaptations",
                "stage_pr_merge",
            }
        )
        if _runtime_mode == "light" and name in _REPO_MUTATION_TOOLS:
            return (
                "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light disables "
                "repo self-modification. Tool "
                f"{name!r} would mutate the Ouroboros repository. "
                "Switch to 'advanced' or 'pro' in Settings → Behavior "
                "→ Runtime Mode to re-enable self-modification."
            )

        protected_write_paths = []
        if name in ("repo_write_commit", "repo_write", "str_replace_editor"):
            if name in ("repo_write_commit", "repo_write"):
                maybe_path = str(args.get("path", "") or "")
                if maybe_path:
                    protected_write_paths.append(maybe_path)
                for f_entry in args.get("files") or []:
                    if isinstance(f_entry, dict):
                        protected_write_paths.append(str(f_entry.get("path", "") or ""))
            elif name == "str_replace_editor":
                protected_write_paths.append(str(args.get("path", "") or ""))
            protected_matches = protected_paths_in(protected_write_paths)
            if protected_matches and not mode_allows_protected_write(_runtime_mode):
                first = protected_matches[0]
                return protected_write_block_message(
                    path=first.path,
                    runtime_mode=_runtime_mode,
                    action=f"run tool {name!r} against",
                )

        # Block modification of safety-critical files via repo_write / repo_write_commit
        if name in ("repo_write_commit", "repo_write"):
            path = args.get("path", "")
            if path and _is_safety_critical_path(path) and not mode_allows_protected_write(_runtime_mode):
                return (
                    "⚠️ CRITICAL SAFETY_VIOLATION: Hardcoded sandbox prevents "
                    "modification of safety-critical files: "
                    + ", ".join(sorted(SAFETY_CRITICAL_PATHS))
                )
            files = args.get("files") or []
            for f_entry in files:
                if (
                    isinstance(f_entry, dict)
                    and _is_safety_critical_path(f_entry.get("path", ""))
                    and not mode_allows_protected_write(_runtime_mode)
                ):
                    return (
                        "⚠️ CRITICAL SAFETY_VIOLATION: Hardcoded sandbox prevents "
                        "modification of safety-critical files: "
                        + ", ".join(sorted(SAFETY_CRITICAL_PATHS))
                    )

        if name == "run_shell":
            raw_cmd = args.get("cmd", args.get("command", ""))
            if isinstance(raw_cmd, list):
                cmd_lower = " ".join(str(x) for x in raw_cmd).lower()
            else:
                cmd_lower = str(raw_cmd).lower()
            cmd_path_lower = cmd_lower.replace("\\", "/")
            while "//" in cmd_path_lower:
                cmd_path_lower = cmd_path_lower.replace("//", "/")
            # Phase 6 light-mode block for run_shell repo-mutation.
            # The shell tool is not in ``_REPO_MUTATION_TOOLS`` because
            # it's used for plenty of read-only invocations (``ls``,
            # ``git status``, pytest, etc.); we instead pattern-match
            # the actual command here. Mutation indicators include git
            # write verbs, file redirection, and python one-liners with
            # ``open(...,'w')``. This is NECESSARILY a best-effort
            # filter — the authoritative gate is the review pipeline —
            # but it blocks the common footguns so a user picking
            # ``light`` sees consistent behaviour.
            if _runtime_mode == "light":
                _LIGHT_MUTATION_INDICATORS = (
                    "git commit", "git add", "git push", "git rebase", "git reset",
                    "git checkout", "git merge", "git pull", "git stash drop",
                    "git revert", "git cherry-pick",
                    " > ", " >> ", " | tee ",
                    "rm -", "mkdir ", "mv ", "cp ", "touch ",
                    # In-place file mutation via common Unix tools.
                    # ``sed -i`` / ``perl -i`` edit files without any
                    # redirection so the ``>`` check above misses them.
                    "sed -i", "perl -i", "ruby -i",
                    "truncate ", "chmod ", "chown ", "ln -",
                    "tar -x", "unzip ", "gzip ", "gunzip ",
                    # Python / JS in-place writers.
                    "open(", ".write(", ".writelines(",
                )
                if any(ind in cmd_lower for ind in _LIGHT_MUTATION_INDICATORS):
                    return (
                        "⚠️ LIGHT_MODE_BLOCKED: runtime_mode=light refuses "
                        "shell commands that look like repo mutations. "
                        "Switch to 'advanced' or 'pro' in Settings → "
                        "Behavior → Runtime Mode for write access."
                    )

            # Block shell writes to safety-critical files
            for cf in _PROTECTED_RUNTIME_PATHS_LOWER:
                if cf in cmd_path_lower and any(w in cmd_lower for w in _SHELL_WRITE_INDICATORS):
                    return (
                        "⚠️ CRITICAL SAFETY_VIOLATION: Shell command would modify "
                        "a protected core/contract/release file. Protected: "
                        + ", ".join(sorted(PROTECTED_RUNTIME_PATHS))
                    )

            # Block GitHub repo create/delete/auth
            if "gh repo create" in cmd_lower or "gh repo delete" in cmd_lower:
                return "⚠️ SAFETY_VIOLATION: Creating/deleting GitHub repositories requires admin approval."
            if "gh auth" in cmd_lower:
                return "⚠️ SAFETY_VIOLATION: Modifying GitHub authentication is not permitted."

            # Git mutative command ban — write ops must go through repo_commit tools
            if isinstance(raw_cmd, list):
                cmd_parts_for_git = [str(x) for x in raw_cmd]
            else:
                cmd_parts_for_git = cmd_lower.split()
            first_word = cmd_parts_for_git[0] if cmd_parts_for_git else ""
            is_direct_git = (first_word == "git")
            is_wrapped_git = (first_word in _SHELL_WRAPPERS and "git " in cmd_lower)

            if is_direct_git:
                subcmd = _extract_git_subcommand(cmd_parts_for_git)
                if subcmd and subcmd.lower() not in _GIT_READONLY_SUBCOMMANDS:
                    return (
                        f"⚠️ GIT_VIA_SHELL_BLOCKED: `git {subcmd}` must go through "
                        "repo_commit / repo_write_commit tools which enforce pre-commit "
                        "checks. For read-only git: git_status, git_diff tools, or "
                        "run_shell with git log/show/diff/status."
                    )

            if is_wrapped_git:
                _git_banned = (
                    "git commit", "git push", "git add ", "git add\t",
                    "git init", "git reset", "git rebase", "git merge",
                    "git cherry-pick", "git branch", "git tag", "git remote",
                    "git config", "git stash", "git clean", "git checkout",
                    "git switch",
                )
                for banned in _git_banned:
                    if banned in cmd_lower:
                        return (
                            "⚠️ GIT_VIA_SHELL_BLOCKED: git mutative commands in shell "
                            "wrappers must go through repo_commit / repo_write_commit tools."
                        )

        # --- LLM Safety Supervisor ---
        from ouroboros.safety import check_safety
        is_safe, safety_msg = check_safety(
            name,
            args,
            messages=getattr(self._ctx, "messages", None),
            ctx=self._ctx,
        )
        if not is_safe:
            return safety_msg

        try:
            result = entry.handler(self._ctx, **args)
        except TypeError as e:
            return f"⚠️ TOOL_ARG_ERROR ({name}): {e}"
        except Exception as e:
            return f"⚠️ TOOL_ERROR ({name}): {e}"

        # Revert protected files after claude_code_edit unless pro mode is
        # active; pro-mode commits still require the core-patch gate later.
        if name == "claude_code_edit":
            reverted = _revert_protected_files(self._ctx.repo_dir, runtime_mode=_runtime_mode)
            if reverted:
                result += (
                    "\n\n⚠️ SAFETY: Reverted modifications to protected files: "
                    + ", ".join(reverted)
                )
            elif mode_allows_protected_write(_runtime_mode):
                try:
                    diff = subprocess.run(
                        ["git", "diff", "--name-only"],
                        cwd=str(self._ctx.repo_dir), capture_output=True, text=True, timeout=5,
                    )
                    protected_matches = protected_paths_in(diff.stdout.splitlines() if diff.returncode == 0 else [])
                except Exception:
                    protected_matches = []
                if protected_matches:
                    result += "\n\n" + core_patch_notice(protected_matches)

        if safety_msg:
            return f"{safety_msg}\n\n---\n{result}"
        return result

    def override_handler(self, name: str, handler) -> None:
        """Override the handler for a registered tool (used for closure injection)."""
        entry = self._entries.get(name)
        if entry:
            self._entries[name] = ToolEntry(
                name=entry.name,
                schema=entry.schema,
                handler=handler,
                timeout_sec=entry.timeout_sec,
            )

    @property
    def CODE_TOOLS(self) -> frozenset:
        return frozenset(e.name for e in self._entries.values() if e.is_code_tool)
