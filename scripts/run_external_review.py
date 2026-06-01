#!/usr/bin/env python3
"""Standalone real triad + scope review dry-run on the STAGED diff.

Recreated per AGENTS.md contract (the workspace can be rebuilt, so this file may
disappear). It runs the actual Ouroboros review substrate against `git diff
--cached` using the real models/prompts/settings, and prints the FULL,
UNTRUNCATED per-reviewer triad records plus the full scope raw result. It NEVER
commits, pushes, or mutates persisted review state, and it never hides
`scope_review_skipped` / budget-exceeded signals.

Usage (from repo/):
    python scripts/run_external_review.py ["commit message"]
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


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Real triad+scope review dry-run on the staged diff (no commit)."
    )
    parser.add_argument(
        "commit_message",
        nargs="?",
        default="release: prepare Ouroboros v6.10.0",
    )
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to also write the full review output to.",
    )
    args = parser.parse_args()

    _load_settings_into_env()

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

    ctx = ToolContext(repo_dir=REPO, drive_root=DATA)
    commit_message = args.commit_message
    goal = os.environ.get(
        "REVIEW_GOAL",
        "Ouroboros 6.10.0: restore full Google Colab source-mode launch, "
        "add role-based GitHub remotes with personal origin provisioning, "
        "allow reviewed chat transports to carry owner slash commands after "
        "owner/chat binding, adapt LLM request parameters so review slots do "
        "not drop models on unsupported optional sampling parameters, record "
        "verified official OuroborosHub provenance profiles without weakening "
        "blocker verdicts, and sync release metadata.",
    )
    scope = os.environ.get(
        "REVIEW_SCOPE",
        "Release/new capability work for Colab launch, transport control, "
        "remote-role persistence, LLM request compatibility, official Hub "
        "provenance profiles, and setup defaults. No model-quality reduction, "
        "no BIBLE edits, no hidden review bypass, no raw secret output, and no "
        "unrelated refactors.",
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
            "scope_review_skipped": getattr(scope_result, "status", "") == "skipped",
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
