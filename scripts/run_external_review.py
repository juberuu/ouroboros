#!/usr/bin/env python3
"""Invoke Ouroboros's actual triad + scope reviewers from outside the runtime.

Use case: developer (or an external orchestrator like an adversarial-review
loop) wants to dry-run the same multi-model commit-time review pipeline that
``repo_commit`` would trigger, against a staged diff, before any actual
commit. Output is the FULL raw response from each reviewer model — no
truncation, no summarisation — so the operator can read everything the
gate would see.

This script does NOT commit, does NOT mutate state outside the temporary
ToolContext fields it owns, and does NOT depend on a live Ouroboros
server process. It only reads:

- ``~/Ouroboros/data/settings.json`` for OPENROUTER_API_KEY,
  OUROBOROS_REVIEW_MODELS, OUROBOROS_SCOPE_REVIEW_MODEL, plus model slot
  defaults (so ``LLMClient`` can route via the same provider lanes).
- The current ``git diff --cached`` (the staged change set).

Usage:
    cd ~/Ouroboros/repo
    git add -A   # stage everything you want reviewed
    python3 scripts/run_external_review.py \
        --commit-message "v5.1.2: light skills + elevation ratchet" \
        --goal "Allow skills in light mode and seal the runtime_mode escalation paths."

Optional flags:
    --no-color       plain ASCII output (otherwise terse ANSI section headers)
    --output PATH    also write the full raw output to a file

Note: this script always reviews the staged diff (``git diff --cached``)
because that is what ``run_parallel_review`` itself reads internally.
There is no working-tree mode — ``git add`` first.

This script is intentionally minimal — it's a development tool, not part
of the runtime gate. It prints to stdout in a structure designed for
direct copy-paste into review-loop summaries.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from typing import Any, Dict, List


# ── Path bootstrap so ``import ouroboros.*`` works outside the package ──────

_REPO_DIR = pathlib.Path(__file__).resolve().parent.parent
_OUROBOROS_HOME = _REPO_DIR.parent
_DATA_DIR = _OUROBOROS_HOME / "data"
_SETTINGS_PATH = _DATA_DIR / "settings.json"

if str(_REPO_DIR) not in sys.path:
    sys.path.insert(0, str(_REPO_DIR))


def _load_settings_into_env() -> Dict[str, Any]:
    """Read settings.json and copy everything string-valued into os.environ.

    The actual Ouroboros runtime does this via
    ``ouroboros.config.apply_settings_to_env``, but that function has a
    fixed allowlist; we want a broader copy here so reviewer flows
    (which hit ``LLMClient`` etc.) see the same provider config the live
    runtime sees. We do NOT call ``apply_settings_to_env`` because it
    would also resolve provider defaults and mutate the in-memory
    settings dict, which is unnecessary for a read-only review.
    """
    if not _SETTINGS_PATH.exists():
        sys.stderr.write(
            f"[run_external_review] settings.json not found at {_SETTINGS_PATH}\n"
            "Run the wizard first or set OPENROUTER_API_KEY / "
            "OUROBOROS_REVIEW_MODELS in the environment manually.\n"
        )
        return {}
    try:
        settings = json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        sys.stderr.write(f"[run_external_review] Failed to parse settings.json: {exc}\n")
        return {}

    pushed: List[str] = []
    for key, value in settings.items():
        if value is None or value == "" or isinstance(value, (dict, list)):
            continue
        os.environ.setdefault(key, str(value))
        pushed.append(key)

    # The official allowlist still wins for the keys it knows about (so
    # apply_settings_to_env semantics remain authoritative if someone adds
    # extra processing). We also explicitly call it so e.g.
    # OUROBOROS_REVIEW_MODELS gets the documented default fallback.
    try:
        from ouroboros.config import apply_settings_to_env
        apply_settings_to_env(settings)
    except Exception as exc:
        sys.stderr.write(
            f"[run_external_review] apply_settings_to_env failed (continuing with raw env copy): {exc}\n"
        )

    sys.stderr.write(
        f"[run_external_review] env populated from {_SETTINGS_PATH} "
        f"({len(pushed)} keys, including: "
        f"{', '.join(k for k in ('OPENROUTER_API_KEY', 'OUROBOROS_REVIEW_MODELS', 'OUROBOROS_SCOPE_REVIEW_MODEL') if k in pushed)})\n"
    )
    return settings


def _ensure_diff_present() -> str:
    """Return the staged diff text the reviewers will see; abort if empty."""
    proc = subprocess.run(
        ["git", "diff", "--cached"],
        cwd=_REPO_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[run_external_review] git diff failed: {proc.stderr}\n")
        sys.exit(2)
    diff = proc.stdout
    if not diff.strip():
        sys.stderr.write(
            "[run_external_review] No staged diff found. "
            "``git add`` the relevant files before running this script.\n"
        )
        sys.exit(3)
    return diff


def _build_ctx() -> Any:
    """Construct a minimal ToolContext sufficient for parallel_review."""
    from ouroboros.tools.registry import ToolContext

    ctx = ToolContext(
        repo_dir=_REPO_DIR,
        drive_root=_DATA_DIR,
    )
    # Reviewer code reads / writes these per-call forensic fields. Default
    # them so attribute access is safe on first use.
    ctx._review_advisory = []
    ctx._review_iteration_count = 0
    ctx._review_history = []
    ctx._scope_review_history = {}
    ctx._last_scope_model = ""
    ctx._last_triad_raw_results = []
    ctx._last_scope_raw_result = {}
    ctx._last_review_block_reason = ""
    ctx._last_review_critical_findings = []
    ctx._current_review_tool_name = "external_review"
    return ctx


def _print_section(title: str, body: str, *, use_color: bool = True) -> None:
    bar = "=" * 78
    if use_color and sys.stdout.isatty():
        head = f"\033[1;33m{title}\033[0m"
    else:
        head = title
    print(f"\n{bar}\n{head}\n{bar}\n{body}\n")


def _format_triad_actor(actor_record: Dict[str, Any]) -> str:
    """Pretty-print one reviewer's full raw record without any truncation."""
    parts = [
        f"model_id     : {actor_record.get('model_id', '?')}",
        f"status       : {actor_record.get('status', '?')}",
        f"tokens_in    : {actor_record.get('tokens_in', 0)}",
        f"tokens_out   : {actor_record.get('tokens_out', 0)}",
        f"cost_usd     : {actor_record.get('cost_usd', 0.0)}",
        f"prompt_chars : {actor_record.get('prompt_chars', 0)}",
        "",
        "── raw_text (verbatim, no truncation) ──",
        actor_record.get("raw_text", "<empty>"),
        "",
        "── parsed_items ──",
        json.dumps(actor_record.get("parsed_items", []), indent=2, ensure_ascii=False),
    ]
    return "\n".join(parts)


def _format_scope(scope_raw: Dict[str, Any]) -> str:
    parts = [
        f"model_id     : {scope_raw.get('model_id', '?')}",
        f"status       : {scope_raw.get('status', '?')}",
        f"tokens_in    : {scope_raw.get('tokens_in', 0)}",
        f"tokens_out   : {scope_raw.get('tokens_out', 0)}",
        f"cost_usd     : {scope_raw.get('cost_usd', 0.0)}",
        "",
        "── raw_text (verbatim, no truncation) ──",
        scope_raw.get("raw_text", "<empty>"),
        "",
        "── critical_findings ──",
        json.dumps(scope_raw.get("critical_findings", []), indent=2, ensure_ascii=False),
        "",
        "── advisory_findings ──",
        json.dumps(scope_raw.get("advisory_findings", []), indent=2, ensure_ascii=False),
    ]
    return "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--commit-message", required=True, help="Synthetic commit message for the review prompt")
    parser.add_argument("--goal", default="", help="Optional goal/intent string passed to scope reviewer")
    parser.add_argument("--scope", default="", help="Optional scope hint passed to scope reviewer")
    parser.add_argument("--review-rebuttal", default="", help="Optional rebuttal text (for rerun scenarios)")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI colors in section headers")
    parser.add_argument("--output", help="Also write full raw output to this file")
    args = parser.parse_args()

    use_color = not args.no_color
    _load_settings_into_env()
    diff = _ensure_diff_present()
    sys.stderr.write(f"[run_external_review] diff size: {len(diff)} chars\n")

    ctx = _build_ctx()

    from ouroboros.tools.parallel_review import run_parallel_review, aggregate_review_verdict

    sys.stderr.write("[run_external_review] launching triad + scope reviewers in parallel...\n")
    commit_start = time.time()
    review_err, scope_result, triad_block_reason, triad_advisory = run_parallel_review(
        ctx,
        args.commit_message,
        goal=args.goal,
        scope=args.scope,
        review_rebuttal=args.review_rebuttal,
    )
    aggregated = aggregate_review_verdict(
        review_err,
        scope_result,
        triad_block_reason,
        triad_advisory,
        ctx,
        args.commit_message,
        commit_start,
        str(_REPO_DIR),
    )

    out_buf: List[str] = []

    def _emit(title: str, body: str) -> None:
        _print_section(title, body, use_color=use_color)
        out_buf.append(f"\n{'=' * 78}\n{title}\n{'=' * 78}\n{body}\n")

    _emit(
        "META",
        json.dumps(
            {
                "commit_message": args.commit_message,
                "goal": args.goal,
                "scope": args.scope,
                "diff_size_chars": len(diff),
                "triad_block_reason": triad_block_reason,
                "triad_advisory_count": len(triad_advisory),
            },
            indent=2,
            ensure_ascii=False,
        ),
    )

    triad_raw = list(getattr(ctx, "_last_triad_raw_results", []) or [])
    if not triad_raw:
        _emit("TRIAD REVIEWERS", "<empty — no actor records produced>")
    else:
        for idx, actor in enumerate(triad_raw):
            _emit(f"TRIAD REVIEWER {idx + 1}/{len(triad_raw)}", _format_triad_actor(actor))

    scope_raw = getattr(ctx, "_last_scope_raw_result", {}) or {}
    if not scope_raw:
        _emit("SCOPE REVIEWER", "<empty — no scope record produced>")
    else:
        _emit("SCOPE REVIEWER", _format_scope(scope_raw))

    if review_err:
        _emit("TRIAD BLOCK MESSAGE (review_err)", review_err)

    if scope_result is not None and getattr(scope_result, "block_message", None):
        _emit("SCOPE BLOCK MESSAGE (scope_result.block_message)", scope_result.block_message)

    _emit(
        "AGGREGATED VERDICT",
        json.dumps(aggregated, indent=2, ensure_ascii=False, default=str)
        if isinstance(aggregated, (dict, list))
        else str(aggregated),
    )

    if args.output:
        try:
            pathlib.Path(args.output).write_text("".join(out_buf), encoding="utf-8")
            sys.stderr.write(f"[run_external_review] full output also written to {args.output}\n")
        except Exception as exc:
            sys.stderr.write(f"[run_external_review] failed to write --output: {exc}\n")

    # Exit code: 0 always — this is a dry-run reporter, not a gate.
    return 0


if __name__ == "__main__":
    sys.exit(main())
