"""ProgramBench cleanroom adapter primitives."""

from __future__ import annotations

import pathlib
import json
import subprocess
import tarfile
from typing import Any

from devtools.benchmarks.common.manifests import write_json
from devtools.benchmarks.common.official_commands import programbench_eval_cmd, programbench_info_cmd
from devtools.benchmarks.programbench.schemas import task_body


def docker_executor_ref(
    *,
    container_name: str,
    workspace_host_path: pathlib.Path,
    workspace_backend_path: str = "/workspace",
) -> dict[str, Any]:
    return {
        "type": "docker_exec",
        "id": container_name,
        "container_name": container_name,
        "network": "none",
        "workspace_host_path": str(pathlib.Path(workspace_host_path).resolve(strict=False)),
        "workspace_backend_path": workspace_backend_path,
    }


def build_ouroboros_task_body(
    *,
    instruction: str,
    workspace_host_path: pathlib.Path,
    container_name: str,
    protected_backend_paths: list[str] | None = None,
    task_id: str = "",
) -> dict[str, Any]:
    ensure_git_workspace(workspace_host_path)
    protected = protected_backend_paths or ["/workspace/executable", "executable"]
    return task_body(
        description=instruction,
        workspace_root=str(pathlib.Path(workspace_host_path).resolve(strict=False)),
        executor_ref=docker_executor_ref(container_name=container_name, workspace_host_path=workspace_host_path),
        protected_paths=protected,
        task_id=task_id,
    )


def preflight_cleanroom_container(container_name: str) -> dict[str, Any]:
    proc = subprocess.run(
        ["docker", "inspect", container_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker inspect failed for {container_name}: {proc.stderr.strip()}")
    data = json.loads(proc.stdout or "[]")
    if not isinstance(data, list) or not data:
        raise RuntimeError(f"docker inspect returned no container data for {container_name}")
    info = data[0]
    config = info.get("Config") if isinstance(info, dict) else {}
    host_config = info.get("HostConfig") if isinstance(info, dict) else {}
    image = str((config or {}).get("Image") or (info or {}).get("Image") or "")
    network = str((host_config or {}).get("NetworkMode") or "")
    if "task_cleanroom" not in image:
        raise RuntimeError(f"ProgramBench container must use a task_cleanroom image, got {image!r}")
    if network != "none":
        raise RuntimeError(f"ProgramBench inference container must use Docker NetworkMode=none, got {network!r}")
    return {"image": image, "network": network}


def ensure_git_workspace(workspace_root: pathlib.Path) -> None:
    root = pathlib.Path(workspace_root).resolve(strict=False)
    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True, timeout=10)
    if probe.returncode == 0 and pathlib.Path((probe.stdout or "").strip()).resolve(strict=False) == root:
        return
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
    subprocess.run(["git", "config", "user.email", "ouroboros-bench@example.invalid"], cwd=root, check=True, timeout=10)
    subprocess.run(["git", "config", "user.name", "Ouroboros Bench"], cwd=root, check=True, timeout=10)


def create_submission_tarball(
    workspace_root: pathlib.Path,
    out_path: pathlib.Path,
    *,
    protected_paths: list[str] | None = None,
    workspace_backend_path: str = "/workspace",
) -> pathlib.Path:
    root = pathlib.Path(workspace_root).resolve(strict=False)
    protected = _protected_submission_paths(root, protected_paths or [], workspace_backend_path=workspace_backend_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out_path, "w:gz") as tar:
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if _skip_submission_path(rel):
                continue
            resolved = path.resolve(strict=False)
            if any(_path_matches(resolved, protected_path) for protected_path in protected):
                continue
            tar.add(path, arcname=rel.as_posix(), recursive=False)
    return out_path


def _protected_submission_paths(root: pathlib.Path, protected_paths: list[str], *, workspace_backend_path: str) -> list[pathlib.Path]:
    protected: list[pathlib.Path] = []
    backend_prefix = str(workspace_backend_path or "/workspace").rstrip("/")
    for raw in protected_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        if text == backend_prefix:
            protected.append(root)
            continue
        if text.startswith(backend_prefix + "/"):
            rel = text[len(backend_prefix) + 1:]
            protected.append((root / rel).resolve(strict=False))
            continue
        candidate = pathlib.Path(text)
        if candidate.is_absolute():
            continue
        protected.append((root / candidate).resolve(strict=False))
    return list(dict.fromkeys(protected))


def _path_matches(candidate: pathlib.Path, protected: pathlib.Path) -> bool:
    if candidate == protected:
        return True
    try:
        candidate.relative_to(protected)
        return True
    except ValueError:
        return False


def _skip_submission_path(rel: pathlib.PurePath) -> bool:
    parts = set(rel.parts)
    return bool(parts & {
        ".git",
        ".ouroboros",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "node_modules",
        "build",
        "dist",
        "htmlcov",
    }) or rel.name in {".DS_Store", ".coverage", "coverage.xml"} or rel.suffix in {".pyc", ".pyo", ".log", ".tmp"}


def run_official_eval(run_root: pathlib.Path) -> dict[str, Any]:
    eval_proc = subprocess.run(programbench_eval_cmd(run_root), capture_output=True, text=True)
    info_proc = subprocess.run(programbench_info_cmd(run_root), capture_output=True, text=True)
    result = {
        "eval": {
            "cmd": programbench_eval_cmd(run_root),
            "returncode": eval_proc.returncode,
            "stdout": eval_proc.stdout,
            "stderr": eval_proc.stderr,
        },
        "info": {
            "cmd": programbench_info_cmd(run_root),
            "returncode": info_proc.returncode,
            "stdout": info_proc.stdout,
            "stderr": info_proc.stderr,
        },
    }
    write_json(pathlib.Path(run_root) / "programbench_eval_result.json", result)
    return result
