"""Inspect solver shim that invokes ``ouroboros run --result-json-out``.

This module is imported by inspect_evals when running GAIA. It is deliberately
small: official task construction/scoring stays in inspect_evals, while this
shim is only responsible for obtaining Ouroboros's structured final_answer.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import shutil
from hashlib import sha256
from types import SimpleNamespace
from typing import Any

if str(pathlib.Path(__file__).resolve().parents[4]) not in sys.path:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

from devtools.benchmarks.common.run_roots import ensure_outside_repo, run_root


try:
    from inspect_ai.solver import Generate, TaskState, solver
except Exception:  # pragma: no cover - inspect is an optional benchmark dependency
    Generate = Any  # type: ignore
    TaskState = Any  # type: ignore

    def solver(fn):  # type: ignore
        return fn


def run_ouroboros(prompt: str, sample_id: str = "sample", attachments: list[pathlib.Path] | None = None) -> dict:
    repo = pathlib.Path(__file__).resolve().parents[4]
    root = pathlib.Path(os.environ.get("GAIA_OUROBOROS_RUN_ROOT") or run_root("gaia")).resolve(strict=False)
    root = ensure_outside_repo(root, repo)
    sample_dir = root / "samples" / "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in sample_id)
    sample_dir.mkdir(parents=True, exist_ok=True)
    result_json = sample_dir / "result.json"
    cmd = [
        sys.executable,
        "-m",
        "ouroboros.cli",
        "--url",
        os.environ.get("GAIA_OUROBOROS_URL", "http://127.0.0.1:8765"),
        "run",
        "--start",
        "--memory-mode",
        "empty",
        "--quiet",
        "--disable-tools",
        "web_search,claude_code_edit",
        "--result-json-out",
        str(result_json),
    ]
    for path in [str(path) for path in (attachments or [])]:
        cmd.extend(["--attach", path])
    cmd.append(prompt)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
    payload = {}
    if result_json.exists():
        try:
            payload = json.loads(result_json.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    answer = payload.get("final_answer") or payload.get("result") or ""
    return {
        "final_answer": str(answer or "").strip(),
        "returncode": proc.returncode,
        "result_json": str(result_json),
        "stderr_tail": proc.stderr[-4000:],
    }


def _state_prompt(state: Any) -> str:
    user_prompt = getattr(state, "user_prompt", None)
    if getattr(user_prompt, "text", None):
        return str(user_prompt.text)
    if getattr(state, "input_text", None):
        return str(state.input_text)
    if getattr(state, "input", None):
        return str(state.input)
    return ""


def _prompt_shared_file_paths(prompt: str) -> list[pathlib.Path]:
    shared_root = pathlib.Path(os.environ.get("GAIA_SHARED_FILES_ROOT") or "/shared_files").resolve(strict=False)
    out: list[pathlib.Path] = []
    for match in re.findall(r"/shared_files/[^\s)'\"`>,]+", str(prompt or "")):
        rel = pathlib.PurePosixPath(match).relative_to("/shared_files")
        if rel.is_absolute() or ".." in rel.parts:
            continue
        out.append((shared_root / pathlib.Path(*rel.parts)).resolve(strict=False))
    return out


def _attachment_paths_from_state(state: Any, sample_dir: pathlib.Path, prompt: str = "") -> list[pathlib.Path]:
    raw_items: list[Any] = []
    for attr in ("files", "attachments"):
        value = getattr(state, attr, None)
        if isinstance(value, dict):
            raw_items.extend(value.values())
        elif isinstance(value, (list, tuple)):
            raw_items.extend(value)
    metadata = getattr(state, "metadata", {}) or {}
    if isinstance(metadata, dict):
        for key in ("files", "attachments"):
            value = metadata.get(key)
            if isinstance(value, dict):
                raw_items.extend(value.values())
            elif isinstance(value, (list, tuple)):
                raw_items.extend(value)
    raw_items.extend(_prompt_shared_file_paths(prompt))
    out: list[pathlib.Path] = []
    attach_dir = sample_dir / "attachments"
    attach_dir.mkdir(parents=True, exist_ok=True)
    repo = pathlib.Path(__file__).resolve().parents[4].resolve(strict=False)
    live_data = repo.parent / "data"
    home = pathlib.Path.home().resolve(strict=False)
    for item in raw_items:
        path = pathlib.Path(str(getattr(item, "path", item))).expanduser().resolve(strict=False)
        if not path.exists() or not path.is_file():
            continue
        try:
            path.relative_to(repo)
            continue
        except ValueError:
            pass
        try:
            path.relative_to(live_data)
            continue
        except ValueError:
            pass
        lower = path.name.lower()
        secret_dirs = {".ssh", ".aws", ".config", ".gnupg"}
        if any(part.lower() in secret_dirs for part in path.parts):
            continue
        if any(token in lower for token in ("key", "token", "credential", ".env", "settings", "id_rsa", "id_ed25519")):
            continue
        digest = sha256(str(path).encode("utf-8", errors="replace")).hexdigest()[:10]
        target = attach_dir / f"{path.stem}-{digest}{path.suffix}"
        if path.resolve(strict=False) != target.resolve(strict=False):
            shutil.copy2(path, target)
        out.append(target)
    return out


@solver
def ouroboros_solver():
    async def solve(state: TaskState, generate: Generate) -> TaskState:
        sample_id = str(getattr(state, "sample_id", "") or getattr(state, "id", "") or "sample")
        repo = pathlib.Path(__file__).resolve().parents[4]
        run_root_path = ensure_outside_repo(
            pathlib.Path(os.environ.get("GAIA_OUROBOROS_RUN_ROOT") or run_root("gaia")).resolve(strict=False),
            repo,
        )
        sample_dir = run_root_path / "samples" / "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in sample_id)
        prompt = _state_prompt(state)
        attachments = _attachment_paths_from_state(state, sample_dir, prompt)
        result = run_ouroboros(prompt, sample_id=sample_id, attachments=attachments)
        if not hasattr(state, "metadata") or getattr(state, "metadata") is None:
            state.metadata = {}
        state.metadata["ouroboros_result_json"] = result.get("result_json", "")
        if not hasattr(state, "output") or getattr(state, "output") is None:
            state.output = SimpleNamespace(completion="")
        state.output.completion = result["final_answer"]
        return state

    return solve
