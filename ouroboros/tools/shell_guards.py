"""Shared shell guard helpers for process tools."""

from __future__ import annotations

import ast
import pathlib
import re
from typing import Any, Dict, List

from ouroboros.runtime_mode_policy import FROZEN_CONTRACT_PATH_PREFIXES, PROTECTED_RUNTIME_PATHS
from ouroboros.shell_parse import (
    EMBEDDED_WINDOWS_ABSOLUTE_PATH_RE,
    embedded_absolute_path_tokens,
    shell_argv,
    shell_argv_with_inline,
    shell_command_string,
    strip_leading_env_assignments,
    unwrap_env_argv,
)

PROTECTED_RUNTIME_PATHS_LOWER = frozenset(
    p.lower() for p in PROTECTED_RUNTIME_PATHS
) | frozenset(prefix.lower() for prefix in FROZEN_CONTRACT_PATH_PREFIXES)

SHELL_WRITE_INDICATORS = (
    "rm ", "rm\t", ">", "sed -i", "tee ", "truncate",
    "mv ", "cp ", "chmod ", "chown ", "unlink ", "delete", "trash",
    "rsync ", "write_text", "open(", ".write(", ".writelines(",
    "os.remove(", "os.unlink(", "os.mkdir(", "os.makedirs(", "sort -o",
    "writefilesync", "appendfilesync", "createwritestream",
)
_SAFE_STDIO_REDIRECT_TOKENS = frozenset({
    ">/dev/null",
    "1>/dev/null",
    "2>/dev/null",
    "2>&1",
    "1>&2",
    "2>&-",
})

LIGHT_SHELL_WRITER_COMMANDS = frozenset({
    "chmod", "chown", "cp", "gunzip", "gzip", "ln", "mkdir", "mv",
    "perl", "rm", "ruby", "sed", "sort", "tar", "touch", "truncate", "uniq", "unzip",
})

INTERPRETER_WRITE_RE = re.compile(
    r"""(?is)(?:\.write\(|write_text\(|write_bytes\(|fs\.write|fs\.append|"""
    r"""createwritestream|unlink\(|rename\(|mkdir\(|rmtree\(|remove\(|"""
    r"""open\s*\([^)]*,\s*['"][^'"]*[wax+])"""
)
EMBEDDED_RELATIVE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:\.\.?/)+[^\s'\"\\),;\]]+")
_REDIRECT_TARGET_TOKENS = frozenset({">", ">>", "1>", "1>>", "2>", "2>>", "&>", "&>>"})
_SCRIPT_INTERPRETERS = frozenset({"python", "python3", "node", "ruby", "perl", "php"})
_SCRIPT_LITERAL_WRITE_RE = {
    "node": re.compile(
        r"""(?is)(?:fs\.|require\(['"]fs['"]\)\.)"""
        r"""(?:writeFileSync|appendFileSync|createWriteStream|mkdirSync|rmSync|rmdirSync|unlinkSync)\s*\(\s*(['"])(.*?)\1"""
    ),
    "ruby": re.compile(
        r"""(?is)(?:File\.write|File\.open|FileUtils\.(?:touch|mkdir_p|rm|rm_rf|remove|copy|cp|mv))\s*\(\s*(['"])(.*?)\1"""
    ),
}


def _python_literal_path(node: ast.AST, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Path" and node.args:
        return _python_literal_path(node.args[0], names)
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Path"
        and node.args
    ):
        return _python_literal_path(node.args[0], names)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "cwd":
        base = node.func.value
        if isinstance(base, ast.Name) and base.id in {"Path", "pathlib"}:
            return "."
        if isinstance(base, ast.Attribute) and base.attr == "Path":
            return "."
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "getcwd"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    ):
        return "."
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _python_literal_path(node.left, names)
        right = _python_literal_path(node.right, names)
        if left is not None and right is not None:
            return str(pathlib.PurePosixPath(left) / right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _python_literal_path(node.left, names)
        right = _python_literal_path(node.right, names)
        if left is not None and right is not None:
            return left + right
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        base = _python_literal_path(node.value, names)
        if base is not None:
            return str(pathlib.PurePosixPath(base).parent)
    return None


def _python_write_mode_from_open_call(node: ast.Call) -> str:
    mode = ""
    if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
        mode = str(node.args[1].value or "")
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = str(keyword.value.value or "")
    return mode


def _python_path_open_target(node: ast.AST, names: dict[str, str]) -> tuple[str | None, bool]:
    if not isinstance(node, ast.Call):
        return None, False
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr == "open"):
        return None, False
    mode = ""
    if node.args and isinstance(node.args[0], ast.Constant):
        mode = str(node.args[0].value or "")
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = str(keyword.value.value or "")
    if not any(flag in mode for flag in ("w", "a", "x", "+")):
        return None, False
    return _python_literal_path(func.value, names), True


def _python_write_targets_and_unknown(inline_code: str) -> tuple[list[str], bool]:
    try:
        tree = ast.parse(inline_code)
    except Exception:
        return [], True
    names: dict[str, str] = {}
    write_handles: dict[str, str] = {}
    targets: list[str] = []
    unknown = False
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    target = None
                    if isinstance(item.context_expr, ast.Call):
                        if isinstance(item.context_expr.func, ast.Name) and item.context_expr.func.id == "open":
                            mode = _python_write_mode_from_open_call(item.context_expr)
                            if any(flag in mode for flag in ("w", "a", "x", "+")) and item.context_expr.args:
                                target = _python_literal_path(item.context_expr.args[0], names)
                        else:
                            maybe_target, is_write_open = _python_path_open_target(item.context_expr, names)
                            if is_write_open:
                                target = maybe_target
                    if target is not None:
                        write_handles[item.optional_vars.id] = target
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            literal = _python_literal_path(node.value, names)
            if literal is not None:
                names[node.targets[0].id] = literal
            if isinstance(node.value, ast.Call):
                handle_target: str | None = None
                if isinstance(node.value.func, ast.Name) and node.value.func.id == "open":
                    mode = _python_write_mode_from_open_call(node.value)
                    if any(flag in mode for flag in ("w", "a", "x", "+")) and node.value.args:
                        handle_target = _python_literal_path(node.value.args[0], names)
                else:
                    target, is_write_open = _python_path_open_target(node.value, names)
                    if is_write_open:
                        handle_target = target
                if handle_target is not None:
                    write_handles[node.targets[0].id] = handle_target
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Attribute):
            if (
                isinstance(func.value.value, ast.Name)
                and func.value.value.id == "sys"
                and func.value.attr in {"stdout", "stderr"}
                and func.attr in {"write", "writelines"}
            ):
                continue
        if isinstance(func, ast.Name) and func.id == "open":
            mode = ""
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                mode = str(node.args[1].value or "")
            for keyword in node.keywords:
                if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                    mode = str(keyword.value.value or "")
            if any(flag in mode for flag in ("w", "a", "x", "+")):
                target = _python_literal_path(node.args[0], names) if node.args else None
                if target is None:
                    unknown = True
                else:
                    targets.append(target)
        elif isinstance(func, ast.Attribute) and func.attr in {
            "write_text", "write_bytes", "unlink", "rename", "replace", "mkdir", "rmdir",
        }:
            target = _python_literal_path(func.value, names)
            if target is None:
                unknown = True
            else:
                targets.append(target)
        elif isinstance(func, ast.Attribute) and func.attr in {"write", "writelines"}:
            if isinstance(func.value, ast.Name) and func.value.id in write_handles:
                targets.append(write_handles[func.value.id])
                continue
            target, is_write_open = _python_path_open_target(func.value, names)
            if is_write_open and target is not None:
                targets.append(target)
        elif isinstance(func, ast.Attribute) and func.attr == "open":
            target, is_write_open = _python_path_open_target(node, names)
            if is_write_open and target is None:
                unknown = True
            elif is_write_open:
                targets.append(target)
        elif isinstance(func, ast.Attribute) and func.attr in {
            "remove", "unlink", "makedirs", "mkdir", "rmdir", "removedirs", "rmtree",
        }:
            target = _python_literal_path(node.args[0], names) if node.args else None
            if target is None:
                unknown = True
            else:
                targets.append(target)
    return list(dict.fromkeys(targets)), unknown


# Same resolve(strict=False) containment semantics on all platforms (SSOT).
from ouroboros.tool_access import path_is_relative_to as _path_inside


def runtime_data_write_targets(
    raw_cmd: Any,
    *,
    drive_root: pathlib.Path,
    work_dir: pathlib.Path,
    allowed_roots: List[pathlib.Path],
) -> List[str]:
    """Find write-like path mentions under runtime data but outside task artifact roots."""

    try:
        drive = pathlib.Path(drive_root).resolve(strict=False)
        cwd = pathlib.Path(work_dir).resolve(strict=False)
    except Exception:
        return []
    allowed = [pathlib.Path(root).resolve(strict=False) for root in allowed_roots]
    try:
        home = pathlib.Path.home().resolve(strict=False)
    except Exception:
        home = pathlib.Path("~").expanduser()
    blocked: List[str] = []
    for token in shell_argv_with_inline(raw_cmd):
        text = str(token or "")
        expanded_texts = {
            text,
            text.replace("$OUROBOROS_DATA_DIR", str(drive))
            .replace("${OUROBOROS_DATA_DIR}", str(drive))
            .replace("%OUROBOROS_DATA_DIR%", str(drive)),
            text.replace("$HOME", str(home)).replace("${HOME}", str(home)).replace("%USERPROFILE%", str(home)),
            text.replace("~/", f"{home}/"),
        }
        candidates: List[str] = []
        for expanded in expanded_texts:
            if expanded.startswith(("/", "~")) or re.match(r"^[A-Za-z]:[\\/]", expanded):
                candidates.append(expanded)
            candidates.extend(embedded_absolute_path_tokens(expanded))
            candidates.extend(EMBEDDED_WINDOWS_ABSOLUTE_PATH_RE.findall(expanded))
            candidates.extend(EMBEDDED_RELATIVE_PATH_RE.findall(expanded))
        for candidate in candidates:
            candidate_variants = {candidate}
            if "\\\\" in candidate:
                candidate_variants.add(candidate.replace("\\\\", "\\"))
            for candidate_text in candidate_variants:
                try:
                    raw_path = pathlib.Path(candidate_text).expanduser()
                    path = raw_path.resolve(strict=False) if raw_path.is_absolute() else (cwd / raw_path).resolve(strict=False)
                except Exception:
                    continue
                if not _path_inside(path, drive) or any(_path_inside(path, root) for root in allowed):
                    continue
                rendered = str(path)
                if rendered not in blocked:
                    blocked.append(rendered)
    return blocked


def shell_has_write_indicator(raw_cmd: Any) -> bool:
    if isinstance(raw_cmd, list):
        text = " ".join(str(x) for x in raw_cmd).lower()
    else:
        text = str(raw_cmd).lower()
    tokens = [str(token).lower() for token in shell_argv_with_inline(raw_cmd)]
    filtered_tokens: List[str] = []
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in _SAFE_STDIO_REDIRECT_TOKENS:
            i += 1
            continue
        if token in {">", "1>", "2>"} and i + 1 < len(tokens) and tokens[i + 1] == "/dev/null":
            i += 2
            continue
        filtered_tokens.append(token)
        i += 1
    filtered_text = " ".join(filtered_tokens)
    for token in _SAFE_STDIO_REDIRECT_TOKENS:
        text = text.replace(token, " ")
    return any(indicator in filtered_text for indicator in SHELL_WRITE_INDICATORS) or any(
        indicator in text for indicator in SHELL_WRITE_INDICATORS if indicator != ">"
    )


def process_shell_guard_args(name: str, args: Dict[str, Any], *, ctx: Any = None, runtime_mode: str = "") -> Dict[str, Any]:
    """Normalize process-tool arguments into the command shape inspected by shell guards."""

    if name == "verify_and_record":
        # The verification `check` is run like run_command, so its resolved argv must
        # pass the SAME shell guards (subagent-secret read, protected-artifact, sudo).
        check = args.get("check")
        if isinstance(check, str):
            cmd = ["sh", "-lc", check.strip()] if check.strip() else []
        elif isinstance(check, (list, tuple)):
            cmd = [str(part) for part in check if str(part or "").strip()]
        else:
            cmd = []
        return {"cmd": cmd, "cwd": args.get("cwd", ""), "__tool_name": name}
    if name == "run_script":
        interpreter = str(args.get("interpreter") or "python3").strip() or "python3"
        script = str(args.get("script") or "")
        cwd = args.get("cwd", "")
        if (
            not str(cwd or "").strip()
            and ctx is not None
            and str(runtime_mode or "").strip() == "light"
            and not bool(getattr(ctx, "is_workspace_mode", lambda: False)())
        ):
            try:
                cwd = str(ctx.task_drive_root())
            except Exception:
                cwd = ""
        return {
            "cmd": [interpreter, "-c", script],
            "cwd": cwd,
            "__tool_name": name,
        }
    return {**args, "__tool_name": name}


def parse_porcelain_paths(output: str) -> list[str]:
    paths: list[str] = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        path_text = line[3:].strip()
        if " -> " in path_text:
            old_path, new_path = path_text.rsplit(" -> ", 1)
            paths.extend([old_path.strip(), new_path.strip()])
        else:
            paths.append(path_text)
    return sorted({p for p in paths if p})


def _candidate_path_inside(root: pathlib.Path, work_dir: pathlib.Path, path_text: str) -> bool:
    text = str(path_text or "").strip()
    if not text or text in {"-", "--"}:
        return False
    if text.startswith(("-", "$")) or text in {"|", "&&", "||", ";", ">", ">>"}:
        return False
    try:
        root_resolved = pathlib.Path(root).resolve()
        base = pathlib.Path(text)
        if not base.is_absolute():
            base = work_dir / base
        candidate = base.expanduser().resolve(strict=False)
        candidate.relative_to(root_resolved)
        return True
    except (OSError, ValueError):
        return False


def repo_target_mentioned(argv: List[str], *, repo_dir: pathlib.Path, cwd: str = "") -> bool:
    work_dir = pathlib.Path(repo_dir)
    if cwd and str(cwd).strip() not in ("", ".", "./"):
        try:
            work_dir = (pathlib.Path(repo_dir) / str(cwd)).resolve(strict=False)
        except OSError:
            pass
    return any(_candidate_path_inside(pathlib.Path(repo_dir), work_dir, token) for token in argv[1:])


def writer_target_tokens(argv: List[str]) -> List[str]:
    if not argv:
        return []
    cmd = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")
    operands = [arg for arg in argv[1:] if arg and not arg.startswith("-")]
    targets: List[str] = []
    if cmd == "cp":
        targets.extend(operands[-1:] if len(operands) >= 2 else [])
    elif cmd in {"chmod", "chown"}:
        targets.extend(operands[1:] if len(operands) >= 2 else [])
    elif cmd == "sed":
        targets.extend(operands[1:] if len(operands) >= 2 else operands)
    elif cmd == "sort":
        for idx, arg in enumerate(argv[1:], start=1):
            if arg == "-o" and idx + 1 < len(argv):
                targets.append(argv[idx + 1])
            if arg.startswith("--output="):
                targets.append(arg.split("=", 1)[1])
    elif cmd == "uniq":
        targets.extend(operands[1:2] if len(operands) >= 2 else [])
    elif cmd in LIGHT_SHELL_WRITER_COMMANDS:
        targets.extend(operands)

    if (cmd in _SCRIPT_INTERPRETERS or cmd.startswith("python")) and "-c" in argv:
        try:
            inline_code = str(argv[argv.index("-c") + 1])
        except Exception:
            inline_code = ""
        if cmd.startswith("python"):
            try:
                tree = ast.parse(inline_code)
            except Exception:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    if (
                        isinstance(node.func, ast.Name)
                        and node.func.id == "open"
                        and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        mode = ""
                        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                            mode = str(node.args[1].value or "")
                        for keyword in node.keywords:
                            if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
                                mode = str(keyword.value.value or "")
                        if any(flag in mode for flag in ("w", "a", "x", "+")):
                            targets.append(node.args[0].value)
                    if (
                        isinstance(node.func, ast.Attribute)
                        and node.func.attr in {"write_text", "write_bytes"}
                        and isinstance(node.func.value, ast.Call)
                        and node.func.value.args
                        and isinstance(node.func.value.args[0], ast.Constant)
                        and isinstance(node.func.value.args[0].value, str)
                    ):
                        targets.append(node.func.value.args[0].value)
        else:
            pattern = _SCRIPT_LITERAL_WRITE_RE.get(cmd)
            if pattern:
                targets.extend(match.group(2) for match in pattern.finditer(inline_code) if match.group(2))

    for index, token in enumerate(argv):
        token_text = str(token)
        token_name = pathlib.PurePath(token_text).name.lower().removesuffix(".exe")
        if token_text in _SAFE_STDIO_REDIRECT_TOKENS:
            continue
        if token_text in _REDIRECT_TARGET_TOKENS and index + 1 < len(argv):
            if str(argv[index + 1]) == "/dev/null":
                continue
            targets.append(str(argv[index + 1]))
            continue
        redirect_match = re.match(r"^(?:[12]|&)?(?:>|>>)(.+)$", token_text)
        if redirect_match and redirect_match.group(1) not in {"/dev/null", "&1", "&2", "&-"}:
            targets.append(redirect_match.group(1))
        if token_name == "tee":
            for tee_target in argv[index + 1 :]:
                tee_target_text = str(tee_target)
                if tee_target_text in {"|", "&&", "||", ";"}:
                    break
                if tee_target_text.startswith("-"):
                    continue
                targets.append(tee_target_text)

    return list(dict.fromkeys(target for target in targets if str(target or "").strip()))


def shell_writer_targets_protected(raw_cmd: Any) -> bool:
    argv = strip_leading_env_assignments(unwrap_env_argv(shell_argv(raw_cmd)))
    if not argv:
        return False
    executable = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")
    if executable in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        return bool(inline and shell_writer_targets_protected(inline))
    if executable not in LIGHT_SHELL_WRITER_COMMANDS:
        return False
    target_text = " ".join(writer_target_tokens(argv)).replace("\\", "/").lower()
    return bool(target_text and any(cf in target_text for cf in PROTECTED_RUNTIME_PATHS_LOWER))


def _workspace_executor_state_target(path: pathlib.Path, drive_root: pathlib.Path) -> bool:
    try:
        rel_parts = pathlib.Path(path).resolve(strict=False).relative_to(
            pathlib.Path(drive_root).resolve(strict=False)
        ).parts
    except (OSError, ValueError):
        return False
    lowered = [str(part).casefold() for part in rel_parts]
    return "state" in lowered and "workspace_executor_processes" in lowered


def workspace_executor_state_write_block(
    raw_cmd: Any,
    *,
    drive_root: pathlib.Path,
    cwd: str = "",
    default_cwd: pathlib.Path | None = None,
) -> str:
    try:
        drive = pathlib.Path(drive_root).resolve(strict=False)
        work_dir = pathlib.Path(cwd).expanduser() if str(cwd or "").strip() else pathlib.Path(default_cwd or ".")
        if not work_dir.is_absolute():
            work_dir = pathlib.Path(default_cwd or ".") / work_dir
        work_dir = work_dir.resolve(strict=False)
    except Exception:
        return ""
    targets = [
        target for target in runtime_data_write_targets(raw_cmd, drive_root=drive, work_dir=work_dir, allowed_roots=[])
        if _workspace_executor_state_target(pathlib.Path(target), drive)
    ]
    if not targets:
        return ""
    return (
        "⚠️ WORKSPACE_EXECUTOR_STATE_WRITE_BLOCKED: workspace executor process records "
        "are owner/runtime control-plane state. Use process/service lifecycle tools "
        "instead of shell-writing state/workspace_executor_processes. Paths: "
        + ", ".join(targets[:5])
    )


def light_shell_repo_mutation(
    raw_cmd: Any,
    *,
    repo_dir: pathlib.Path,
    cwd: str = "",
    detect_interpreter_inline: bool = False,
) -> bool:
    """Detect simple shell writer commands that target the repo in light mode."""
    argv = shell_argv(raw_cmd)
    if not argv:
        return False
    cmd_lower = " ".join(argv).lower()

    unwrapped = unwrap_env_argv(argv)
    if unwrapped != argv:
        return light_shell_repo_mutation(
            unwrapped,
            repo_dir=repo_dir,
            cwd=cwd,
            detect_interpreter_inline=detect_interpreter_inline,
        )
    argv = strip_leading_env_assignments(argv)
    if not argv:
        return False
    executable = pathlib.PurePath(argv[0]).name.lower().removesuffix(".exe")

    if executable in {"bash", "sh", "zsh"}:
        inline = shell_command_string(argv)
        if inline:
            return light_shell_repo_mutation(
                inline,
                repo_dir=repo_dir,
                cwd=cwd,
                detect_interpreter_inline=detect_interpreter_inline,
            )

    if executable in LIGHT_SHELL_WRITER_COMMANDS and repo_target_mentioned([argv[0], *writer_target_tokens(argv)], repo_dir=repo_dir, cwd=cwd):
        return True

    if detect_interpreter_inline and executable in {"python", "python3", "node", "ruby", "perl", "php"}:
        inline = shell_command_string(argv) or " ".join(argv[1:])
        if INTERPRETER_WRITE_RE.search(inline):
            if executable in {"python", "python3"} or executable.startswith("python"):
                targets, unknown = _python_write_targets_and_unknown(inline)
                if targets and repo_target_mentioned([argv[0], *targets], repo_dir=repo_dir, cwd=cwd):
                    return True
                if unknown:
                    return True
                return False
            targets = writer_target_tokens(argv)
            if targets:
                return repo_target_mentioned([argv[0], *targets], repo_dir=repo_dir, cwd=cwd)
            # Non-Python interpreters with write indicators but no literal target
            # stay fail-closed: a dynamic path may still target the repo.
            return True
        return False

    if any(ind in cmd_lower for ind in (" > ", " >> ", " | tee ")):
        return repo_target_mentioned(argv, repo_dir=repo_dir, cwd=cwd)
    return False
