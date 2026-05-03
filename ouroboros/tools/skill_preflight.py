"""Skill payload preflight — read-only validators a heal-mode agent can run.

The Skill Review Checklist (``docs/CHECKLISTS.md`` §"Skill Review Checklist")
is the single enable-gate for skill payloads. Tri-model ``review_skill`` is
the authoritative pass; ``skill_preflight`` is its **cheap, syntax-only**
companion: it runs ``python -m py_compile`` / ``node --check`` / ``bash -n``
on the payload's actual files plus a manifest parse, with no LLM calls and
no review-state mutation. The agent can therefore catch trivial syntax
errors before spending money on a tri-model review.

Heal-mode agents are blocked from ``run_shell``, so prior to v5.7.0 they
had no way to even syntax-check their just-edited payload.
``skill_preflight`` is added to ``_HEAL_MODE_ALLOWED_TOOLS`` so the heal
prompt's "fix payload, then re-review" loop is reachable end-to-end.

Safety contract (mirrors ``skill_exec``'s argv-only invariant):

- argv array, never a shell string. We never set ``shell=True``.
- ``cwd=skill_dir`` so a script can't escape into peer payloads.
- Scrubbed environment via the same ``_scrub_env`` allowlist as
  ``skill_exec``, with no ``env_from_settings`` keys forwarded
  (preflight does NOT need provider tokens to run a syntax check).
- 30s wall-clock timeout per JS/shell file; killed via ``_kill_process_group``
  on overflow (panic-tracked, like ``skill_exec``). Python syntax checks are
  done in-process via ``compile()`` over file text so they are genuinely
  read-only (``py_compile`` writes ``__pycache__``).
- Read-only: no review.json / enabled.json / grants.json mutation.
"""

from __future__ import annotations

import logging
import pathlib
import shutil
import subprocess
from subprocess import Popen
import json
from typing import Any, Dict, List, Optional, Tuple

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.tools.shell import _active_subprocesses, _kill_process_group, _subprocess_lock
from ouroboros.platform_layer import (
    merge_hidden_kwargs,
    subprocess_new_group_kwargs,
)
from ouroboros.contracts.skill_manifest import (
    SkillManifestError,
    parse_skill_manifest_text,
)

log = logging.getLogger(__name__)

_PREFLIGHT_TIMEOUT_SEC = 30
_PREFLIGHT_MAX_OUTPUT_BYTES = 16 * 1024
_PREFLIGHT_HARD_FILE_LIMIT = 60  # mirrors skill_review's _MAX_SKILL_FILES headroom

# Mapping from file extension to (validator argv template, runtime label).
# Each template uses ``{path}`` as a placeholder for the absolute file path
# under cwd=skill_dir; we substitute it explicitly into the argv array so
# there is never a shell string anywhere in the chain.
_VALIDATORS: Dict[str, Tuple[List[str], str]] = {
    ".js": (["node", "--check", "{path}"], "node"),
    ".mjs": (["node", "--check", "{path}"], "node"),
    ".cjs": (["node", "--check", "{path}"], "node"),
    ".sh": (["bash", "-n", "{path}"], "bash"),
    ".bash": (["bash", "-n", "{path}"], "bash"),
}


def _resolve_runtime(runtime: str) -> Optional[str]:
    if runtime == "python3":
        path = shutil.which("python3") or shutil.which("python")
    else:
        path = shutil.which(runtime)
    return path


def _run_check(cmd: List[str], cwd: pathlib.Path) -> Dict[str, Any]:
    """Run a validator argv with the same panic-tracked subprocess machinery
    skill_exec uses. Returns a dict with ``returncode``, ``stdout``, ``stderr``,
    ``timeout`` flags. Never raises."""
    popen_kwargs: Dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.DEVNULL,
        "cwd": str(cwd),
        "env": {
            "PATH": str(__import__("os").environ.get("PATH", "")),
            "HOME": str(__import__("os").environ.get("HOME", "")),
            "LANG": "C.UTF-8",
        },
    }
    popen_kwargs.update(subprocess_new_group_kwargs())
    try:
        proc = Popen(cmd, **merge_hidden_kwargs(popen_kwargs))  # noqa: S603 — argv array
    except FileNotFoundError as exc:
        return {"returncode": -1, "stdout": "", "stderr": f"runtime not found: {exc}", "timeout": False}
    with _subprocess_lock:
        _active_subprocesses.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=_PREFLIGHT_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            try:
                _kill_process_group(proc)
            except Exception:  # pragma: no cover
                log.debug("kill_process_tree failed", exc_info=True)
            try:
                stdout, stderr = proc.communicate(timeout=2)
            except Exception:
                stdout, stderr = b"", b""
            return {
                "returncode": -9,
                "stdout": stdout.decode("utf-8", errors="replace")[:_PREFLIGHT_MAX_OUTPUT_BYTES],
                "stderr": stderr.decode("utf-8", errors="replace")[:_PREFLIGHT_MAX_OUTPUT_BYTES],
                "timeout": True,
            }
    finally:
        with _subprocess_lock:
            _active_subprocesses.discard(proc)
    return {
        "returncode": int(proc.returncode or 0),
        "stdout": (stdout or b"").decode("utf-8", errors="replace")[:_PREFLIGHT_MAX_OUTPUT_BYTES],
        "stderr": (stderr or b"").decode("utf-8", errors="replace")[:_PREFLIGHT_MAX_OUTPUT_BYTES],
        "timeout": False,
    }


def _run_python_syntax_check(path: pathlib.Path) -> Dict[str, Any]:
    """Read-only Python syntax check.

    ``py_compile`` would write ``__pycache__`` under the skill payload, which
    violates the advertised read-only preflight contract. ``compile()`` over
    source text catches the same syntax errors without touching disk.
    """
    try:
        text = path.read_text(encoding="utf-8")
        compile(text, str(path), "exec")
        return {"returncode": 0, "stdout": "", "stderr": "", "timeout": False}
    except Exception as exc:
        return {
            "returncode": 1,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "timeout": False,
        }


def _handle_skill_preflight(
    ctx: ToolContext,
    skill: str = "",
    paths: Optional[List[str]] = None,
    **_kwargs: Any,
) -> str:
    skill_name = str(skill or "").strip()
    if not skill_name:
        return "⚠️ SKILL_PREFLIGHT_ERROR: 'skill' is required."

    from ouroboros.skill_loader import find_skill

    drive_root = pathlib.Path(ctx.drive_root)
    loaded = find_skill(drive_root, skill_name)
    if loaded is None:
        return f"⚠️ SKILL_PREFLIGHT_ERROR: skill {skill_name!r} not found."

    skill_dir = loaded.skill_dir.resolve()

    # Manifest parse first: even if the manifest itself is broken, we still
    # report it as a preflight finding rather than refusing to run other
    # validators. The user expects "tell me what's broken", not "exit on
    # first error".
    manifest_findings: List[Dict[str, Any]] = []
    manifest_path = None
    for candidate in ("SKILL.md", "skill.json"):
        cand = skill_dir / candidate
        if cand.is_file():
            manifest_path = cand
            break
    if manifest_path is None:
        manifest_findings.append({"item": "manifest_present", "ok": False, "detail": "no SKILL.md / skill.json"})
    else:
        try:
            text = manifest_path.read_text(encoding="utf-8")
            manifest = parse_skill_manifest_text(text)
            manifest_findings.append({"item": "manifest_parse", "ok": True, "detail": "ok"})
            if manifest.entry:
                entry = (skill_dir / manifest.entry).resolve()
                ok = entry.is_file()
                try:
                    entry.relative_to(skill_dir)
                except ValueError:
                    ok = False
                manifest_findings.append({
                    "item": "manifest_entry_exists",
                    "ok": ok,
                    "detail": manifest.entry if ok else f"missing or escaping entry: {manifest.entry}",
                })
            for script in manifest.scripts or []:
                name = str(script.get("name") or "").strip()
                if not name:
                    continue
                rel = name if "/" in name or name.startswith(".") else f"scripts/{name}"
                script_path = (skill_dir / rel).resolve()
                ok = script_path.is_file()
                try:
                    script_path.relative_to(skill_dir)
                except ValueError:
                    ok = False
                manifest_findings.append({
                    "item": "manifest_script_exists",
                    "ok": ok,
                    "detail": rel if ok else f"missing or escaping script: {rel}",
                })
        except (OSError, UnicodeDecodeError, SkillManifestError) as exc:
            manifest_findings.append({
                "item": "manifest_parse",
                "ok": False,
                "detail": f"{type(exc).__name__}: {exc}",
            })

    # Resolve which files to validate. ``paths`` lets the caller scope the
    # check to a single file they just edited; otherwise we walk the same
    # surface ``skill_loader._iter_payload_files`` exposes.
    files_to_check: List[pathlib.Path] = []
    path_findings: List[Dict[str, Any]] = []
    if paths:
        for raw in paths:
            rel = str(raw or "").strip()
            if not rel or rel.startswith("/") or rel.startswith("~") or ".." in rel.split("/"):
                path_findings.append({"path": rel, "runtime": "", "ok": False, "stderr": "invalid or escaping path", "stdout": "", "timeout": False})
                continue
            target = (skill_dir / rel).resolve()
            try:
                target.relative_to(skill_dir)
            except ValueError:
                path_findings.append({"path": rel, "runtime": "", "ok": False, "stderr": "path escapes skill directory", "stdout": "", "timeout": False})
                continue
            if target.is_file():
                files_to_check.append(target)
            else:
                path_findings.append({"path": rel, "runtime": "", "ok": False, "stderr": "path not found", "stdout": "", "timeout": False})
    else:
        try:
            from ouroboros.skill_loader import _iter_payload_files  # pylint: disable=W0212
            for path in _iter_payload_files(
                skill_dir,
                manifest_entry=loaded.manifest.entry,
                manifest_scripts=loaded.manifest.scripts,
            ):
                files_to_check.append(path.resolve())
        except Exception as exc:
            log.debug("preflight discovery failed", exc_info=True)
            return f"⚠️ SKILL_PREFLIGHT_ERROR: payload discovery failed: {exc}"

    omitted_count = 0
    omitted_files: List[str] = []
    if len(files_to_check) > _PREFLIGHT_HARD_FILE_LIMIT:
        omitted = files_to_check[_PREFLIGHT_HARD_FILE_LIMIT:]
        omitted_count = len(omitted)
        omitted_files = [str(path.relative_to(skill_dir)) for path in omitted[:20]]
        files_to_check = files_to_check[:_PREFLIGHT_HARD_FILE_LIMIT]

    file_findings: List[Dict[str, Any]] = list(path_findings)
    for path in files_to_check:
        suffix = path.suffix.lower()
        if suffix == ".py":
            result = _run_python_syntax_check(path)
            ok = result["returncode"] == 0 and not result["timeout"]
            file_findings.append({
                "path": str(path.relative_to(skill_dir)),
                "runtime": "python",
                "ok": ok,
                "returncode": result["returncode"],
                "timeout": result["timeout"],
                "stderr": result["stderr"][:2000],
                "stdout": result["stdout"][:2000],
            })
            continue
        validator = _VALIDATORS.get(suffix)
        if validator is None:
            continue
        argv_template, runtime = validator
        runtime_path = _resolve_runtime(runtime)
        if runtime_path is None:
            file_findings.append({
                "path": str(path.relative_to(skill_dir)),
                "runtime": runtime,
                "ok": False,
                "detail": f"runtime {runtime!r} is not on PATH (skipped)",
                "skipped": True,
            })
            continue
        cmd = [runtime_path] + [str(path) if part == "{path}" else part for part in argv_template[1:]]
        result = _run_check(cmd, cwd=skill_dir)
        ok = result["returncode"] == 0 and not result["timeout"]
        file_findings.append({
            "path": str(path.relative_to(skill_dir)),
            "runtime": runtime,
            "ok": ok,
            "returncode": result["returncode"],
            "timeout": result["timeout"],
            "stderr": result["stderr"][:2000],
            "stdout": result["stdout"][:2000],
        })

    overall_ok = (
        all(f.get("ok") for f in manifest_findings)
        and all(f.get("ok") for f in file_findings)
        and omitted_count == 0
        and (not paths or any(f.get("ok") for f in file_findings))
    )
    payload = {
        "skill": skill_name,
        "skill_dir": str(skill_dir),
        "manifest": manifest_findings,
        "files": file_findings,
        "files_checked": len(file_findings),
        "files_failed": sum(1 for f in file_findings if not f.get("ok") and not f.get("skipped")),
        "omitted_count": omitted_count,
        "omitted_files": omitted_files,
        "ok": bool(overall_ok),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


_PREFLIGHT_SCHEMA = {
    "name": "skill_preflight",
    "description": (
        "Read-only payload syntax validator for one skill. Runs Python "
        "compile() (no __pycache__), node --check, and bash -n on every reviewable file "
        "(or just the ones in `paths` if provided), plus a manifest "
        "parse. Cheap and offline (no LLM, no review.json mutation, "
        "no review status change). Heal-mode agents use this before "
        "calling review_skill so silly syntax errors are caught "
        "without spending tri-model review tokens. Argv-only "
        "subprocess invocation, cwd=skill_dir, scrubbed env, 30s "
        "per-file cap, panic-tracked process group."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill name (directory basename in the skills tree).",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional subset of payload files (relative to the "
                    "skill dir) to validate. Empty = walk the same "
                    "surface skill_review reads."
                ),
            },
        },
        "required": ["skill"],
    },
}


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="skill_preflight",
            schema=_PREFLIGHT_SCHEMA,
            handler=_handle_skill_preflight,
            is_code_tool=False,
            timeout_sec=120,
        ),
    ]


__all__ = ["get_tools"]
