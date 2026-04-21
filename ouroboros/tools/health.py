"""Codebase health tool — complexity metrics and self-assessment."""

import logging
import os
import pathlib
from typing import Any, Dict

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)


def _codebase_health(ctx: ToolContext) -> str:
    """Compute and format codebase health report."""
    try:
        from ouroboros.review import collect_sections, compute_complexity_metrics

        repo_dir = pathlib.Path(ctx.repo_dir)
        drive_root = pathlib.Path(os.environ.get("DRIVE_ROOT", str(pathlib.Path.home() / "Ouroboros" / "data")))

        sections, stats = collect_sections(repo_dir, drive_root)
        metrics = compute_complexity_metrics(sections)

        # Format report
        lines = []
        lines.append("## Codebase Health Report\n")
        lines.append(f"**Analyzed:** {stats['files']} files, {stats['chars']:,} chars")
        if stats.get("truncated"):
            lines.append(f"**Compacted files:** {stats['truncated']}")
        if stats.get("dropped"):
            dropped_paths = stats.get("dropped_paths") or []
            preview = ", ".join(dropped_paths[:5])
            lines.append(
                f"**Dropped files due review budget:** {stats['dropped']}"
                + (f" ({preview}{' ...' if len(dropped_paths) > 5 else ''})" if preview else "")
            )
        lines.append(f"**Files:** {metrics['total_files']} ({metrics['py_files']} Python)")
        lines.append(f"**Total lines:** {metrics['total_lines']:,}")
        lines.append(f"**Functions:** {metrics['total_functions']}")
        lines.append(f"**Avg function length:** {metrics['avg_function_length']} lines")
        lines.append(f"**Max function length:** {metrics['max_function_length']} lines")

        from ouroboros.review import (
            MAX_FUNCTION_LINES,
            MAX_MODULE_LINES,
            MAX_TOTAL_FUNCTIONS,
            TARGET_FUNCTION_LINES,
            TARGET_MODULE_LINES,
        )

        # Largest files
        if metrics.get("largest_files"):
            lines.append("\n### Largest Files")
            for path, size in metrics["largest_files"][:10]:
                if size > MAX_MODULE_LINES:
                    marker = " 🚫 HARD LIMIT"
                elif size > TARGET_MODULE_LINES:
                    marker = " ⚠️ TARGET DRIFT"
                else:
                    marker = ""
                lines.append(f"  {path}: {size} lines{marker}")

        # Longest functions
        if metrics.get("longest_functions"):
            lines.append("\n### Longest Functions")
            for path, start, length in metrics["longest_functions"][:10]:
                if length > MAX_FUNCTION_LINES:
                    marker = " 🚫 HARD LIMIT"
                elif length > TARGET_FUNCTION_LINES:
                    marker = " ⚠️ TARGET DRIFT"
                else:
                    marker = ""
                lines.append(f"  {path}:{start}: {length} lines{marker}")

        # Warnings
        target_drift_funcs = metrics.get("target_drift_functions", [])
        target_drift_mods = metrics.get("target_drift_modules", [])
        grandfathered_mods = metrics.get("grandfathered_modules", [])
        oversized_funcs = metrics.get("oversized_functions", [])
        oversized_mods = metrics.get("oversized_modules", [])
        function_count_violation = int(metrics.get("total_functions") or 0) > MAX_TOTAL_FUNCTIONS

        if (
            oversized_funcs
            or oversized_mods
            or grandfathered_mods
            or target_drift_funcs
            or target_drift_mods
            or function_count_violation
        ):
            lines.append("\n### Complexity Status (Principle 5: Minimalism)")
            if function_count_violation:
                lines.append(
                    f"  Hard-limit total functions > {MAX_TOTAL_FUNCTIONS}: {metrics['total_functions']}"
                )
            if oversized_funcs:
                lines.append(f"  Hard-limit functions > {MAX_FUNCTION_LINES} lines: {len(oversized_funcs)}")
                for path, start, length in oversized_funcs:
                    lines.append(f"    - {path}:{start} ({length} lines)")
            elif target_drift_funcs:
                lines.append(f"  Target-drift functions > {TARGET_FUNCTION_LINES} lines: {len(target_drift_funcs)}")
            if oversized_mods:
                lines.append(f"  Hard-limit modules > {MAX_MODULE_LINES} lines: {len(oversized_mods)}")
                for path, size in oversized_mods:
                    lines.append(f"    - {path} ({size} lines)")
            if grandfathered_mods:
                lines.append(f"  Grandfathered modules still above {MAX_MODULE_LINES} lines: {len(grandfathered_mods)}")
                for path, size in grandfathered_mods:
                    lines.append(f"    - {path} ({size} lines)")
            elif target_drift_mods:
                lines.append(f"  Target-drift modules > {TARGET_MODULE_LINES} lines: {len(target_drift_mods)}")
        else:
            lines.append(
                "\n✅ No hard P5 limit violations detected "
                f"(all functions <= {MAX_FUNCTION_LINES} lines, total function count <= {MAX_TOTAL_FUNCTIONS}, "
                f"all non-grandfathered modules <= {MAX_MODULE_LINES} lines)"
            )

        return "\n".join(lines)

    except Exception as e:
        log.warning("codebase_health failed: %s", e, exc_info=True)
        return f"⚠️ Failed to compute codebase health: {e}"


def get_tools():
    return [
        ToolEntry("codebase_health", {
            "name": "codebase_health",
            "description": "Get codebase complexity metrics: file sizes, longest functions, modules exceeding limits. Useful for self-assessment per Bible Principle 5 (Minimalism).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        }, _codebase_health),
    ]
