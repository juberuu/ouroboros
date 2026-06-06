#!/usr/bin/env python3
"""Generate SWE-bench predictions JSONL with Ouroboros.

This helper prepares the official prediction artifact only. Evaluation remains
the responsibility of ``swebench.harness.run_evaluation``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.run_roots import (
    ensure_file_output_outside_repo,
    ensure_outside_repo,
    repo_root_from_devtools,
    safe_benchmark_id,
)
from devtools.benchmarks.common.official_commands import swebench_eval_cmd
from devtools.benchmarks.swe_bench.presets import resolve_preset
from ouroboros.config import get_finalization_grace_sec


REPO_ROOT = repo_root_from_devtools()


def _records(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        item = json.loads(raw)
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _resolve_workspace(item: dict[str, Any], workspaces_root: str) -> str:
    workspace = str(item.get("workspace_root") or "").strip()
    if workspace or not workspaces_root:
        return workspace
    root = Path(workspaces_root).expanduser()
    instance_id = str(item.get("instance_id") or "")
    repo = str(item.get("repo") or "").strip()
    candidates = [root / instance_id]
    if repo:
        candidates.extend([root / repo.replace("/", "__"), root / repo.split("/")[-1]])
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return ""


def _git_stdout(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=10)


def _record_error(errors: list[dict[str, Any]], row: dict[str, Any], continue_on_error: bool) -> None:
    if not continue_on_error:
        raise RuntimeError(str(row.get("error") or row))
    errors.append(row)


def _write_logs(logs_dir: str, instance_id: str, stdout: str, stderr: str, summary: dict[str, Any]) -> None:
    if not logs_dir:
        return
    log_dir = Path(logs_dir).expanduser() / safe_benchmark_id(instance_id)
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "ouroboros.stdout").write_text(stdout, encoding="utf-8")
    (log_dir / "ouroboros.stderr").write_text(stderr, encoding="utf-8")
    (log_dir / "ouroboros-agent-result.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _build_ouroboros_cmd(args: argparse.Namespace, workspace: Path, result_json_path: Path, prompt: str) -> list[str]:
    cli_prefix = shlex.split(args.cli) if args.cli else [sys.executable, "-m", "ouroboros.cli"]
    return [
        *cli_prefix,
        "run",
        "--workspace",
        str(workspace),
        "--memory-mode",
        "empty",
        "--timeout",
        str(int(args.timeout)),
        "--patch",
        "--result-json-out",
        str(result_json_path),
        prompt,
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="JSONL instances")
    parser.add_argument("--output", required=True, help="SWE-bench predictions JSONL")
    parser.add_argument("--model-name", default="ouroboros-cli")
    parser.add_argument("--cli", default="", help="optional Ouroboros CLI command prefix")
    parser.add_argument("--timeout", type=int, default=7200, help="per-instance Ouroboros timeout seconds")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument("--errors-output", default="")
    parser.add_argument("--logs-dir", default="")
    parser.add_argument("--workspaces-root", default="")
    parser.add_argument("--print-eval-command", default="", help="optional preset/dataset for official eval command")
    args = parser.parse_args()

    output_path = ensure_file_output_outside_repo(Path(args.output), REPO_ROOT)
    errors_output_path = (
        ensure_file_output_outside_repo(Path(args.errors_output), REPO_ROOT)
        if args.errors_output
        else Path(str(output_path) + ".errors.jsonl")
    )
    logs_dir = str(ensure_outside_repo(Path(args.logs_dir), REPO_ROOT)) if args.logs_dir else ""

    predictions: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []
    for item in _records(Path(args.input)):
        instance_id = str(item.get("instance_id") or "")
        try:
            safe_instance_id = safe_benchmark_id(instance_id)
        except ValueError as exc:
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "error": str(exc),
                    "reason_code": "invalid_instance_id",
                },
                args.continue_on_error,
            )
            continue
        workspace = _resolve_workspace(item, args.workspaces_root)
        prompt = str(item.get("problem_statement") or item.get("prompt") or "")
        if not instance_id or not workspace or not prompt:
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "error": "each row must include instance_id, workspace_root or --workspaces-root, and problem_statement/prompt",
                    "reason_code": "invalid_instance",
                },
                args.continue_on_error,
            )
            continue

        workspace_path = Path(workspace).expanduser().resolve(strict=False)
        if not workspace_path.is_dir():
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "error": f"workspace_root is not a directory for {instance_id}: {workspace}",
                    "reason_code": "invalid_workspace",
                },
                args.continue_on_error,
            )
            continue
        head = _git_stdout(["git", "rev-parse", "HEAD"], workspace_path)
        if head.returncode != 0:
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "error": f"workspace_root is not a git checkout for {instance_id}: {workspace_path}",
                    "reason_code": "not_git_checkout",
                },
                args.continue_on_error,
            )
            continue
        base_commit = str(item.get("base_commit") or "").strip()
        if base_commit and head.stdout.strip() != base_commit:
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "error": f"workspace HEAD for {instance_id} is {head.stdout.strip()}, expected base_commit {base_commit}",
                    "reason_code": "wrong_base_commit",
                },
                args.continue_on_error,
            )
            continue
        status = _git_stdout(["git", "status", "--porcelain=v1", "--untracked-files=all"], workspace_path)
        if status.returncode != 0 or status.stdout.strip():
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "error": f"workspace must be clean before SWE-bench run for {instance_id}",
                    "reason_code": "dirty_workspace",
                },
                args.continue_on_error,
            )
            continue

        if logs_dir:
            result_json_path = Path(logs_dir) / safe_instance_id / "task_result.json"
        else:
            result_json_path = Path(tempfile.gettempdir()) / f"ouroboros_swebench_{safe_instance_id}.task_result.json"
        result_json_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = _build_ouroboros_cmd(args, workspace_path, result_json_path, prompt)
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=int(args.timeout) + get_finalization_grace_sec() + 60,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", errors="replace")
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode("utf-8", errors="replace")
            _write_logs(
                logs_dir,
                instance_id,
                stdout,
                stderr,
                {
                    "instance_id": instance_id,
                    "returncode": 124,
                    "stdout_chars": len(stdout),
                    "stderr_chars": len(stderr),
                    "timeout_sec": int(args.timeout),
                    "failure_mode": "timeout",
                },
            )
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "returncode": 124,
                    "error": f"ouroboros run timed out after {int(args.timeout)}s",
                    "timeout": True,
                },
                args.continue_on_error,
            )
            continue

        task_result: dict[str, Any] = {}
        if result_json_path.exists():
            try:
                loaded = json.loads(result_json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    task_result = loaded
            except Exception:
                task_result = {}
        summary = {
            "instance_id": instance_id,
            "returncode": result.returncode,
            "stdout_chars": len(result.stdout or ""),
            "stderr_chars": len(result.stderr or ""),
            "patch_empty": not bool((result.stdout or "").strip()),
            "timeout_sec": int(args.timeout),
            "outcome_axes": task_result.get("outcome_axes"),
            "reason_code": task_result.get("reason_code"),
            "artifact_bundle": task_result.get("artifact_bundle"),
        }
        _write_logs(logs_dir, instance_id, result.stdout or "", result.stderr or "", summary)
        if result.returncode != 0:
            details = (result.stderr or result.stdout or "").strip()
            if len(details) > 4000:
                details = details[:4000] + "\n...[truncated]"
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "returncode": result.returncode,
                    "error": details or f"ouroboros run exited {result.returncode}",
                    "outcome_axes": task_result.get("outcome_axes"),
                    "reason_code": task_result.get("reason_code"),
                    "artifact_bundle": task_result.get("artifact_bundle"),
                    "trace_refs": task_result.get("trace_refs"),
                },
                args.continue_on_error,
            )
            continue
        if not (result.stdout or "").strip():
            _record_error(
                errors,
                {
                    "instance_id": instance_id,
                    "returncode": 0,
                    "error": "ouroboros run produced no patch",
                    "outcome_axes": task_result.get("outcome_axes"),
                    "reason_code": task_result.get("reason_code") or "no_patch",
                    "artifact_bundle": task_result.get("artifact_bundle"),
                    "trace_refs": task_result.get("trace_refs"),
                },
                args.continue_on_error,
            )
            continue
        predictions.append(
            {
                "instance_id": instance_id,
                "model_name_or_path": args.model_name,
                "model_patch": result.stdout,
            }
        )

    output_path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in predictions) + ("\n" if predictions else ""),
        encoding="utf-8",
    )
    if errors:
        errors_output_path.write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in errors) + "\n",
            encoding="utf-8",
        )
    if args.print_eval_command:
        print(" ".join(shlex.quote(part) for part in swebench_eval_cmd(resolve_preset(args.print_eval_command), output_path, "ouroboros", 1)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
