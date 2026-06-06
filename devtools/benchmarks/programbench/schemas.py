"""ProgramBench adapter schemas."""

from __future__ import annotations

from typing import Any


def protected_reference_policy(paths: list[str]) -> dict[str, Any]:
    clean = [str(path) for path in paths if str(path or "").strip()]
    return {
        "protected_artifacts": [
            {
                "id": "programbench_reference",
                "role": "black_box_reference",
                "paths": clean,
                "allow": ["execute"],
                "deny": [
                    "read_bytes",
                    "copy",
                    "hash",
                    "static_introspection",
                    "dynamic_trace",
                    "debug",
                ],
            }
        ]
    }


def task_body(
    *,
    description: str,
    workspace_root: str,
    executor_ref: dict[str, Any],
    protected_paths: list[str],
    task_id: str = "",
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "description": description,
        "workspace_root": workspace_root,
        "workspace_mode": "external",
        "memory_mode": "empty",
        "allowed_resources": {"web": False, "network": False, "internet": False},
        "resource_policy": protected_reference_policy(protected_paths),
        "executor_ref": executor_ref,
        "actor_id": "programbench",
        "source": "programbench",
        "metadata": {"source": "programbench"},
    }
    if task_id:
        body["task_id"] = task_id
    return body
