"""Private Tool API v1 -> v2 migration aliases.

Legacy names are intentionally absent from public schemas. This module only
keeps old internal prompts/tests from failing during the v6.3 migration.
"""

from __future__ import annotations

import pathlib
from typing import Any, Dict, Optional

from ouroboros.contracts.task_constraint import TaskConstraint


LEGACY_TOOL_ALIASES = {
    "repo_read": "read_file",
    "repo_list": "list_files",
    "repo_write": "write_file",
    "str_replace_editor": "edit_text",
    "data_read": "read_file",
    "data_list": "list_files",
    "data_write": "write_file",
    "code_search": "search_code",
    "run_shell": "run_command",
    "git_status": "vcs_status",
    "git_diff": "vcs_diff",
    "repo_commit": "commit_reviewed",
    "pull_from_remote": "vcs_pull_ff",
    "restore_to_head": "vcs_restore",
    "revert_commit": "vcs_revert",
    "rollback_to_target": "vcs_rollback",
    "schedule_task": "schedule_subagent",
    "wait_for_task": "wait_task",
    "wait_for_tasks": "wait_tasks",
    "advisory_pre_review": "advisory_review",
    "review_skill": "skill_review",
}


def translate_legacy_tool_call(name: str, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    mapped = LEGACY_TOOL_ALIASES.get(name)
    if not mapped:
        return name, args
    translated = dict(args or {})
    translated["_legacy_tool_name"] = name
    if name.startswith("data_"):
        translated.setdefault("root", "runtime_data")
    if name == "repo_list" and "path" in translated and "dir" not in translated:
        translated["dir"] = translated.pop("path")
    if name == "data_list" and "path" in translated and "dir" not in translated:
        translated["dir"] = translated.pop("path")
    return mapped, translated


def constraint_bucket_skill(constraint: Optional[TaskConstraint]) -> tuple[str, str]:
    if not constraint or not constraint.payload_root:
        return "", ""
    parts = pathlib.PurePosixPath(str(constraint.payload_root).replace("\\", "/")).parts
    if len(parts) >= 3 and parts[0] == "skills":
        return parts[1], parts[2]
    return "", ""
