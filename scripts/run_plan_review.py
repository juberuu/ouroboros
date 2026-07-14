#!/usr/bin/env python3
"""Standalone Ouroboros multi-model plan-review dry-run.

This script mirrors the reviewer-panel portion of ``plan_task`` for operator use:
it loads the same governance docs, optional touched-file snapshots, optional
generated Atlas context, accepted raw scout-handoff artifacts, and the configured
review-model slots, then prints every reviewer response without truncation. It
does not spawn a second planning engine: live scouts remain owned by production
``plan_task``; their saved handoffs can be supplied with ``--scout-handoff``.

Usage (from anywhere):
    python scripts/run_plan_review.py --plan /path/to/plan.md --context-level broad
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
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


def _plan_class_for_subject(subject_root: pathlib.Path, requested: str = "") -> str:
    """Default external subjects to external framing; Ouroboros itself is self-mod."""
    explicit = str(requested or "").strip()
    if explicit:
        return explicit
    return "self_mod" if subject_root == REPO else "external"


def _read_scout_handoffs(paths: list[str], formatter) -> tuple[str, str, list[dict]]:
    """Load production scout artifacts once, with compact refs for fit fallback."""
    raw_sections: list[str] = []
    compact_sections: list[str] = []
    refs: list[dict] = []
    for raw in paths:
        path = pathlib.Path(raw).expanduser().resolve(strict=False)
        try:
            payload_bytes = path.read_bytes()
            text = payload_bytes.decode("utf-8")
        except Exception as exc:
            raise SystemExit(f"ERROR: could not read scout handoff at {path}: {exc}") from exc
        ref = {
            "kind": "plan_task_handoffs",
            "path": str(path),
            "sha256": hashlib.sha256(payload_bytes).hexdigest(),
            "bytes": len(payload_bytes),
        }
        refs.append(ref)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        heading = (
            "## Supplied Production Scout Handoff\n\n"
            "Canonical raw artifact ref:\n\n```json\n"
            + json.dumps(ref, ensure_ascii=False, indent=2)
            + "\n```\n\n"
        )
        if isinstance(payload, dict):
            raw_sections.append(heading + formatter(payload, raw=True))
            compact_sections.append(heading + formatter(payload, raw=False))
        else:
            raw_sections.append(heading + "```text\n" + text + "\n```")
            compact_sections.append(
                heading
                + "Raw non-JSON handoff omitted inline only for prompt fit; the exact "
                "path/hash above remains the forensic source."
            )
    return "\n\n".join(raw_sections), "\n\n".join(compact_sections), refs


async def _run(args: argparse.Namespace) -> str:
    import tempfile

    from ouroboros.tools.plan_review import (
        _format_planning_handoffs,
        _get_review_models,
        _run_plan_review_async,
    )
    from ouroboros.tools.registry import ToolContext

    files_to_touch = _split_paths(args.files_to_touch or [])
    plan = _read_text_file(args.plan, label="plan")
    extra_context = _read_extra_context(args.extra_context or [])
    scout_handoff_raw, scout_handoff_compact, scout_handoff_refs = _read_scout_handoffs(
        getattr(args, "scout_handoff", []) or [], _format_planning_handoffs,
    )
    goal = str(args.goal or "").strip() or "Review the proposed implementation plan before code is written."

    drive_root = (
        pathlib.Path(args.drive_root).expanduser().resolve(strict=False)
        if args.drive_root
        else pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-plan-review-"))
    )
    drive_root.mkdir(parents=True, exist_ok=True)
    (drive_root / "logs").mkdir(parents=True, exist_ok=True)
    subject_root = (
        pathlib.Path(getattr(args, "subject_root", "")).expanduser().resolve(strict=False)
        if getattr(args, "subject_root", "") else REPO
    )
    plan_class = _plan_class_for_subject(
        subject_root,
        getattr(args, "plan_class", ""),
    )
    ctx = ToolContext(
        repo_dir=REPO,
        system_repo_dir=REPO,
        workspace_root=subject_root if subject_root != REPO else None,
        workspace_mode="external" if subject_root != REPO else "",
        drive_root=drive_root,
    )
    models = _get_review_models()
    coordinated = await _run_plan_review_async(
        ctx,
        plan,
        goal,
        files_to_touch,
        context_level=args.context_level,
        context_notes=str(args.context_notes or ""),
        include_tests=bool(args.include_tests),
        plan_class=plan_class,
        planning_handoff_override=(scout_handoff_raw, scout_handoff_compact),
        additional_context=extra_context,
    )
    raw_results = list(getattr(ctx, "_last_plan_review_raw_results", []) or [])
    estimated_tokens = int(getattr(ctx, "_last_plan_review_estimated_tokens", 0) or 0)

    sep = "=" * 80
    raw_block = "\n".join(
        [
            sep,
            "RESOLVED PLAN REVIEW CONFIG",
            sep,
            json.dumps(
                {
                    "models": models,
                    "context_level": args.context_level,
                    "include_tests": bool(args.include_tests),
                    "estimated_tokens": estimated_tokens,
                    "drive_root": str(drive_root),
                    "governance_root": str(getattr(ctx, "_last_plan_review_governance_root", REPO)),
                    "subject_root": str(getattr(ctx, "_last_plan_review_subject_root", subject_root)),
                    "plan_class": plan_class,
                    "files_to_touch": files_to_touch,
                    "scout_handoff_refs": scout_handoff_refs,
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
            coordinated,
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
    parser.add_argument(
        "--subject-root",
        default="",
        help="Active repository/workspace whose files and Atlas are under review; governance remains Ouroboros.",
    )
    parser.add_argument(
        "--plan-class",
        default="",
        choices=["", "self_mod", "external", "research", "creative"],
        help="Planning framing; defaults to external for an external subject root and self_mod for Ouroboros.",
    )
    parser.add_argument("--extra-context", action="append", default=[], help="Extra text file to include.")
    parser.add_argument(
        "--scout-handoff",
        action="append",
        default=[],
        help="Saved production plan_task_handoffs.json (repeatable); raw content is used when it fits and exact refs are always retained.",
    )
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
