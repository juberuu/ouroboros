#!/usr/bin/env python3
"""Standalone Ouroboros multi-model plan-review dry-run.

This script mirrors the reviewer-panel portion of ``plan_task`` for operator use:
it loads the same governance docs, optional touched-file snapshots, optional
generated Atlas context, and the configured review-model slots, then prints every
reviewer response without truncation. It intentionally skips the live planning
scout swarm because that depends on a running worker/supervisor environment.

Usage (from anywhere):
    python scripts/run_plan_review.py --plan /path/to/plan.md --context-level broad
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA = REPO.parent / "data"

if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load_settings_into_env() -> None:
    """Load runtime settings through the shared config path; never print secrets."""
    try:
        from ouroboros.config import apply_settings_to_env, load_settings
        from ouroboros.server_runtime import apply_runtime_provider_defaults

        settings, _changed, _changed_keys = apply_runtime_provider_defaults(load_settings())
        apply_settings_to_env(settings)
    except Exception as exc:  # pragma: no cover - operator script
        print(f"WARN: could not load/apply Ouroboros settings: {exc}", file=sys.stderr)


def _split_paths(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in str(value or "").split(","):
            text = part.strip()
            if text and text not in out:
                out.append(text)
    return out


def _read_text_file(path_text: str, *, label: str) -> str:
    path = pathlib.Path(path_text).expanduser().resolve(strict=False)
    try:
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        raise SystemExit(f"ERROR: could not read {label} at {path}: {exc}") from exc


def _read_extra_context(paths: list[str]) -> str:
    sections: list[str] = []
    for raw in paths:
        path = pathlib.Path(raw).expanduser().resolve(strict=False)
        try:
            text = path.read_text(encoding="utf-8")
        except Exception as exc:
            raise SystemExit(f"ERROR: could not read extra context file {path}: {exc}") from exc
        sections.append(f"### {path}\n\n{text}")
    if not sections:
        return ""
    return "## Additional Plan Context Files\n\n" + "\n\n---\n\n".join(sections)


async def _run(args: argparse.Namespace) -> str:
    from ouroboros.tools.plan_review import (
        _PLAN_BUDGET_TOKEN_LIMIT,
        _build_system_prompt,
        _build_user_content,
        _format_output,
        _get_review_models,
        _load_plan_checklist,
        _plan_context_target_tokens,
        _resolve_plan_context_level,
        _run_plan_review_slots,
    )
    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.review_context_atlas import (
        ReviewContextAtlasRequest,
        compile_review_context_atlas,
    )
    from ouroboros.tools.review_helpers import (
        build_head_snapshot_section,
        load_governance_doc,
    )
    from ouroboros.utils import estimate_tokens

    context_level = _resolve_plan_context_level(args.context_level)
    files_to_touch = _split_paths(args.files_to_touch or [])
    plan = _read_text_file(args.plan, label="plan")
    extra_context = _read_extra_context(args.extra_context or [])
    goal = str(args.goal or "").strip() or "Review the proposed implementation plan before code is written."

    drive_root = pathlib.Path(args.drive_root).expanduser().resolve(strict=False) if args.drive_root else DATA
    drive_root.mkdir(parents=True, exist_ok=True)
    (drive_root / "logs").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(repo_dir=REPO, drive_root=drive_root)

    checklist = _load_plan_checklist()
    bible_text = load_governance_doc(REPO, "BIBLE.md", on_missing="explicit")
    dev_md = load_governance_doc(REPO, "docs/DEVELOPMENT.md", on_missing="explicit")
    arch_md = load_governance_doc(REPO, "docs/ARCHITECTURE.md", on_missing="explicit")
    checklists_md = load_governance_doc(REPO, "docs/CHECKLISTS.md", on_missing="explicit")
    canonical_docs = {
        "BIBLE.md",
        "docs/DEVELOPMENT.md",
        "docs/ARCHITECTURE.md",
        "docs/CHECKLISTS.md",
    }

    head_snapshots = build_head_snapshot_section(REPO, files_to_touch) if files_to_touch else ""
    system_prompt = _build_system_prompt(
        checklist,
        bible_text,
        dev_md,
        arch_md,
        checklists_md,
        context_level=context_level,
    )
    placeholder = "__GENERATED_PLAN_ATLAS_PENDING__"
    user_content = _build_user_content(
        plan,
        goal,
        files_to_touch,
        head_snapshots,
        placeholder if context_level != "minimal" else "",
        "",
        context_level=context_level,
        context_notes=str(args.context_notes or ""),
        include_tests=bool(args.include_tests),
    )
    if extra_context:
        user_content += "\n\n" + extra_context

    fixed_prompt_tokens = estimate_tokens(system_prompt + user_content)
    if context_level != "minimal":
        target_tokens = _plan_context_target_tokens(context_level)
        atlas = compile_review_context_atlas(
            ReviewContextAtlasRequest(
                repo_dir=REPO,
                anchors=tuple(files_to_touch),
                already_included=frozenset(set(files_to_touch) | canonical_docs),
                fixed_prompt_tokens=fixed_prompt_tokens,
                target_total_tokens=target_tokens,
                hard_total_tokens=_PLAN_BUDGET_TOKEN_LIMIT,
                include_tests=bool(args.include_tests),
                title=f"Generated Plan Review Atlas ({context_level})",
                drive_root=drive_root,
            )
        )
        if atlas.status == "budget_exceeded":
            estimated = int((atlas.manifest or {}).get("estimated_total_tokens") or 0)
            return (
                "PLAN_REVIEW_SKIPPED: generated repository atlas exceeded hard budget"
                + (f" ({estimated:,} estimated tokens)" if estimated else "")
            )
        head, sep, tail = user_content.rpartition(placeholder)
        if not sep:
            return "ERROR: Failed to build review context atlas: placeholder missing."
        user_content = head + atlas.text + tail

    estimated_tokens = estimate_tokens(system_prompt + user_content)
    if estimated_tokens > _PLAN_BUDGET_TOKEN_LIMIT:
        return (
            f"PLAN_REVIEW_SKIPPED: assembled prompt too large "
            f"({estimated_tokens:,} estimated tokens, limit {_PLAN_BUDGET_TOKEN_LIMIT:,})."
        )

    models = _get_review_models()
    raw_results = await _run_plan_review_slots(ctx, models, system_prompt, user_content)

    sep = "=" * 80
    raw_block = "\n".join(
        [
            sep,
            "RESOLVED PLAN REVIEW CONFIG",
            sep,
            json.dumps(
                {
                    "models": models,
                    "context_level": context_level,
                    "include_tests": bool(args.include_tests),
                    "estimated_tokens": estimated_tokens,
                    "drive_root": str(drive_root),
                    "files_to_touch": files_to_touch,
                    "plan": str(pathlib.Path(args.plan).expanduser().resolve(strict=False)),
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            sep,
            "PLAN REVIEW RAW RESULTS (full, untruncated)",
            sep,
            json.dumps(raw_results, ensure_ascii=False, indent=2, default=str),
            sep,
            "PLAN REVIEW COORDINATED OUTPUT",
            sep,
            _format_output(raw_results, models, goal, estimated_tokens),
        ]
    )
    return raw_block


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the configured Ouroboros plan-review panel without the live scout swarm."
    )
    parser.add_argument("--plan", required=True, help="Path to the plan file to review.")
    parser.add_argument("--goal", default="", help="High-level goal under review.")
    parser.add_argument(
        "--context-level",
        required=True,
        choices=["minimal", "localized", "broad", "constitutional"],
        help="Plan-review context level.",
    )
    parser.add_argument(
        "--files-to-touch",
        action="append",
        default=[],
        help="Comma-separated or repeated repo-relative planned paths.",
    )
    parser.add_argument("--context-notes", default="", help="Additional plan context notes.")
    parser.add_argument("--extra-context", action="append", default=[], help="Extra text file to include.")
    parser.add_argument("--include-tests", action="store_true", help="Allow generated Atlas test context.")
    parser.add_argument(
        "--drive-root",
        default=os.environ.get("OUROBOROS_REVIEW_DRIVE_ROOT", ""),
        help="Drive root for review observability writes. Prefer a temp dir.",
    )
    parser.add_argument("--output", default="", help="Optional path to also write the full output.")
    args = parser.parse_args()

    _load_settings_into_env()
    output = asyncio.run(_run(args))
    print(output)
    if args.output:
        pathlib.Path(args.output).expanduser().write_text(output + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
