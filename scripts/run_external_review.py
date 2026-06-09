#!/usr/bin/env python3
"""Standalone real triad + scope review dry-run on the STAGED diff.

Recreated per AGENTS.md contract (the workspace can be rebuilt, so this file may
disappear). It runs the actual Ouroboros review substrate against `git diff
--cached` using the real models/prompts/settings, and prints the FULL,
UNTRUNCATED per-reviewer triad records plus the full scope raw result. It NEVER
commits, pushes, or mutates persisted review state, and it never hides
`scope_review_skipped` / budget-exceeded signals.

Usage (from repo/):
    python scripts/run_external_review.py ["commit message"] [--drive-root /tmp/review-data]
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA = REPO.parent / "data"

# Allow `import ouroboros` when invoked as a standalone script from any cwd.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load_settings_into_env() -> None:
    """Load data/settings.json scalars into env; never print secret values."""
    settings_path = DATA / "settings.json"
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - operator script
            print(f"WARN: could not parse settings.json: {exc}", file=sys.stderr)
            data = {}
        for key, value in (data.items() if isinstance(data, dict) else []):
            if isinstance(value, bool):
                os.environ[key] = "1" if value else "0"
            elif isinstance(value, (str, int, float)) and str(value) != "":
                os.environ[key] = str(value)
    else:
        print(f"WARN: settings.json not found at {settings_path}", file=sys.stderr)

    # Transient provider-key fallback from ~/file1.txt (never printed/persisted).
    def _fallback(env_name: str, prefix: str) -> None:
        if os.environ.get(env_name, "").strip():
            return
        f1 = pathlib.Path.home() / "file1.txt"
        if not f1.exists():
            return
        for line in f1.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith(prefix + ":"):
                os.environ[env_name] = line.split(":", 1)[1].strip()
                break

    _fallback("OPENROUTER_API_KEY", "openrouter")
    _fallback("OPENAI_API_KEY", "openai")
    _fallback("ANTHROPIC_API_KEY", "anthropic")


def _scope_review_skipped(scope_result: object, scope_advisory_items: object) -> bool:
    status = str(getattr(scope_result, "status", "") or "").strip().lower()
    if status in {"skipped", "budget_exceeded"}:
        return True
    if isinstance(scope_advisory_items, list):
        return any(
            isinstance(item, dict)
            and str(item.get("item") or "").strip() == "scope_review_skipped"
            for item in scope_advisory_items
        )
    return False


def _resolved_review_config() -> dict:
    """Return resolved review slots and efforts after settings/env loading."""
    from ouroboros.config import (
        get_review_models,
        get_scope_review_models,
        resolve_effort,
    )

    return {
        "triad_models": get_review_models(),
        "triad_effort": resolve_effort("review"),
        "scope_models": get_scope_review_models(),
        "scope_effort": resolve_effort("scope_review"),
    }


def main() -> int:
    import argparse

    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    parser = argparse.ArgumentParser(
        description="Real triad+scope review dry-run on the staged diff (no commit)."
    )
    parser.add_argument(
        "commit_message",
        nargs="?",
        default=f"release: Ouroboros v{version} deep core capability release",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to also write the full review output to.",
    )
    parser.add_argument(
        "--drive-root",
        default=os.environ.get("OUROBOROS_REVIEW_DRIVE_ROOT", ""),
        help=(
            "Drive root for review observability writes. Defaults to ../data. "
            "Use a temp dir to avoid writing review artifacts/events into live data."
        ),
    )
    args = parser.parse_args()

    _load_settings_into_env()
    resolved_config = _resolved_review_config()
    print(
        "Resolved review config: "
        + json.dumps(resolved_config, ensure_ascii=False),
        file=sys.stderr,
    )

    staged = subprocess.run(
        ["git", "diff", "--cached"], cwd=str(REPO), capture_output=True, text=True
    ).stdout
    if not staged.strip():
        print("ERROR: staged diff is empty — `git add` the changes first.", file=sys.stderr)
        return 2

    from ouroboros.tools.registry import ToolContext
    from ouroboros.tools.parallel_review import (
        run_parallel_review,
        aggregate_review_verdict,
    )

    review_drive_root = pathlib.Path(args.drive_root).expanduser().resolve(strict=False) if args.drive_root else DATA
    review_drive_root.mkdir(parents=True, exist_ok=True)
    (review_drive_root / "logs").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(repo_dir=REPO, drive_root=review_drive_root)
    commit_message = args.commit_message
    goal = os.environ.get(
        "REVIEW_GOAL",
        f"Ouroboros {version}: validate the deep core capability release. "
        "Check task_contract/outcome_axes/verification_ledger semantics, "
        "LLM-first task acceptance, broad capability envelopes with explicit "
        "omission manifests, workspace readonly subagent delegation with "
        "enabled external tools, deadline/finalization/artifact states, "
        "and evolution reviewed-commit plus restart-verification accounting.",
    )
    scope = os.environ.get(
        "REVIEW_SCOPE",
        "Core runtime/API/CLI/UI/docs release work. No BIBLE edits, no "
        "benchmark-specific routing, no deterministic semantic success scoring, "
        "no hidden review bypass, no new finish/submit tool, no second scheduler, "
        "and no stale public result_status dependence.",
    )

    t0 = time.time()
    review_err, scope_result, triad_block_reason, triad_advisory = run_parallel_review(
        ctx, commit_message, goal=goal, scope=scope
    )
    blocked, combined_msg, block_reason, combined_findings, scope_advisory_items = (
        aggregate_review_verdict(
            review_err, scope_result, triad_block_reason, triad_advisory,
            ctx, commit_message, t0, str(REPO),
        )
    )

    sep = "=" * 80
    out = "\n".join([
        sep, "RESOLVED REVIEW CONFIG", sep,
        json.dumps({**resolved_config, "drive_root": str(review_drive_root)}, indent=2, ensure_ascii=False, default=str),
        sep, "TRIAD RAW RESULTS (full, untruncated)", sep,
        json.dumps(getattr(ctx, "_last_triad_raw_results", []), indent=2, ensure_ascii=False, default=str),
        sep, "SCOPE RAW RESULT (full, untruncated)", sep,
        json.dumps(getattr(ctx, "_last_scope_raw_result", {}), indent=2, ensure_ascii=False, default=str),
        sep, "AGGREGATE VERDICT", sep,
        json.dumps({
            "blocked": blocked,
            "block_reason": block_reason,
            "triad_block_reason": triad_block_reason,
            "scope_model": getattr(ctx, "_last_scope_model", ""),
            "scope_status": getattr(scope_result, "status", None),
            "scope_blocked": getattr(scope_result, "blocked", None),
            "scope_review_skipped": _scope_review_skipped(scope_result, scope_advisory_items),
            "review_err": review_err,
            "combined_message": combined_msg,
            "combined_findings": combined_findings,
            "scope_advisory_items": scope_advisory_items,
            "triad_advisory": triad_advisory,
            "elapsed_sec": round(time.time() - t0, 1),
        }, indent=2, ensure_ascii=False, default=str),
    ])
    print(out)
    if args.output:
        pathlib.Path(args.output).write_text(out + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
