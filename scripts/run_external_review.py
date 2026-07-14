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
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
DATA = pathlib.Path(
    os.environ.get("OUROBOROS_DATA_DIR", "") or (REPO.parent / "data")
).expanduser().resolve(strict=False)

# Allow `import ouroboros` when invoked as a standalone script from any cwd.
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


def _load_settings_into_env() -> None:
    """Load data/settings.json scalars into env; never print secret values."""
    settings_path = pathlib.Path(
        os.environ.get("OUROBOROS_SETTINGS_PATH", "") or (DATA / "settings.json")
    ).expanduser().resolve(strict=False)
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:  # pragma: no cover - operator script
            print(f"WARN: could not parse settings.json: {exc}", file=sys.stderr)
            data = {}
        for key, value in (data.items() if isinstance(data, dict) else []):
            if os.environ.get(key, "").strip():
                continue
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
        candidates = [
            pathlib.Path(os.environ["OUROBOROS_KEYS_FILE"]).expanduser()
            if os.environ.get("OUROBOROS_KEYS_FILE", "").strip()
            else None,
            DATA.parent / "file1.txt",
            pathlib.Path.home() / "ouro" / "file1.txt",
            pathlib.Path.home() / "file1.txt",
        ]
        f1 = next((path for path in candidates if path is not None and path.is_file()), None)
        if f1 is None:
            return
        for line in f1.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith(prefix + ":"):
                os.environ[env_name] = line.split(":", 1)[1].strip()
                break

    _fallback("OPENROUTER_API_KEY", "openrouter")
    _fallback("OPENAI_API_KEY", "openai")
    _fallback("ANTHROPIC_API_KEY", "anthropic")


def _actor_records(ctx: object) -> list[dict]:
    """Return physical reviewer actor records without double-counting summaries."""
    actors = [
        dict(item)
        for item in (getattr(ctx, "_last_triad_raw_results", []) or [])
        if isinstance(item, dict)
    ]
    scope_raw = getattr(ctx, "_last_scope_raw_result", {}) or {}
    if isinstance(scope_raw, dict) and isinstance(scope_raw.get("raw_results"), list):
        actors.extend(dict(item) for item in scope_raw["raw_results"] if isinstance(item, dict))
    elif isinstance(scope_raw, dict) and any(
        key in scope_raw for key in ("slot", "slot_id", "prompt_ref", "response_ref")
    ):
        actors.append(dict(scope_raw))
    return actors


def _review_evidence_and_cost(ctx: object) -> tuple[list[dict], dict]:
    """Build a neutral actor-level evidence/cost report.

    A zero/missing actor cost is never presented as proof that the call was free.
    It is reported as unreported whenever the actor has usage or durable call refs.
    """
    evidence: list[dict] = []
    reported_cost = 0.0
    reported_slots: list[str] = []
    unreported_slots: list[str] = []
    for idx, actor in enumerate(_actor_records(ctx), start=1):
        slot = str(actor.get("slot_id") or actor.get("slot") or f"actor_{idx}")
        prompt_ref = actor.get("prompt_ref") or {}
        response_ref = actor.get("response_ref") or {}
        evidence.append({
            "slot": slot,
            "model_id": str(actor.get("model_id") or actor.get("model") or ""),
            "status": str(actor.get("status") or ""),
            "prompt_ref": prompt_ref,
            "response_ref": response_ref,
        })
        try:
            cost = float(actor.get("cost_usd"))
        except (TypeError, ValueError):
            cost = 0.0
        if cost > 0:
            reported_cost += cost
            reported_slots.append(slot)
        elif (
            int(actor.get("tokens_in") or 0) > 0
            or int(actor.get("tokens_out") or 0) > 0
            or bool(prompt_ref)
            or bool(response_ref)
        ):
            unreported_slots.append(slot)
    return evidence, {
        "reported_actor_cost_usd": round(reported_cost, 8),
        "reported_cost_slots": reported_slots,
        "unreported_or_unknown_cost_slots": unreported_slots,
        "note": (
            "Actor-reported cost only; unreported/unknown slots are not treated as $0. "
            "The core usage ledger remains the monetary authority."
        ),
    }


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
            "Drive root for review observability writes. Defaults to a new persistent "
            "temporary directory, never the live data root."
        ),
    )
    parser.add_argument(
        "--goal",
        default=os.environ.get("REVIEW_GOAL", ""),
        help="Owner-approved goal. Defaults to a neutral current-release goal.",
    )
    parser.add_argument(
        "--scope",
        default=os.environ.get("REVIEW_SCOPE", ""),
        help="Owner-approved scope. Defaults to staged-tree scope with drift detection.",
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

    review_drive_root = (
        pathlib.Path(args.drive_root).expanduser().resolve(strict=False)
        if args.drive_root
        else pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-external-review-"))
    )
    review_drive_root.mkdir(parents=True, exist_ok=True)
    (review_drive_root / "logs").mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(repo_dir=REPO, drive_root=review_drive_root)
    commit_message = args.commit_message
    goal = args.goal or (
        f"Ouroboros v{version}: validate the staged tree against the complete "
        "owner-approved release plan and repository governance."
    )
    scope = args.scope or (
        "Only the staged owner-approved release changes are in scope. Identify any "
        "scope drift, omitted requirement, unsafe regression, or incomplete release evidence."
    )

    t0 = time.time()
    from ouroboros.tools.git import _run_non_committing_review_cycle

    outcome = _run_non_committing_review_cycle(
        ctx,
        commit_message,
        skip_advisory_review=True,
        goal=goal,
        scope=scope,
    )
    evidence_refs, cost_report = _review_evidence_and_cost(ctx)
    complete = str(outcome.get("status") or "") == "passed"

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
            "complete": complete,
            "production_outcome": outcome,
            "scope_model": getattr(ctx, "_last_scope_model", ""),
            "raw_evidence_refs": evidence_refs,
            "cost_report": cost_report,
            "elapsed_sec": round(time.time() - t0, 1),
        }, indent=2, ensure_ascii=False, default=str),
    ])
    print(out)
    if args.output:
        pathlib.Path(args.output).write_text(out + "\n", encoding="utf-8")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
