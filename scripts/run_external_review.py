#!/usr/bin/env python3
"""Run the REAL production commit-gate cycle (advisory → triad → scope) on the
staged diff, as an external operator, without committing.

This is a thin wrapper over the runtime's own review substrate — models,
prompts, checklists, limits, and efforts all resolve from the same
config/settings SSOT the live gate uses, so the dry-run cannot drift from
production behavior. What the wrapper adds is operator ergonomics only:

* an isolated detached worktree so the reviewed tree cannot change mid-run;
* a fresh drive root (never the live data root) for review state/observability;
* `OUROBOROS_RUNTIME_MODE=pro` by default — release diffs touch protected
  paths, which only pro mode may stage for review;
* OpenRouter key health-check/selection from the named operator pool
  (`limit_remaining` probe; `hope*` keys last; values never printed);
* the real advisory pre-review by default (the same Claude-SDK advisory the
  gate requires), so the pytest-preflight substitute is not forced;
* typed exit codes separating infrastructure failures from genuine review
  blocks.

Exit codes:
    0  review passed
    1  genuine review block (critical findings)
    2  staged diff is empty
    3  not a reviewer verdict (oversize diff policy, advisory/transport/key
       trouble, protection gate, quorum loss, preflight) — diagnose the named
       cause; rerunning without fixing it reproduces the same block

Usage (from repo/):
    python scripts/run_external_review.py ["commit message"] [--output DIR]
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
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

# Release diffs touch protected core paths; only pro mode may stage them for
# review. An explicit operator env value still wins.
os.environ.setdefault("OUROBOROS_RUNTIME_MODE", "pro")

# Genuine review verdicts (the author must address findings); every other
# non-passed outcome is environment/infrastructure and is safe to retry after
# fixing the environment.
_GENUINE_BLOCK_REASONS = {"critical_findings"}
_OPENROUTER_MIN_REMAINING_USD = 10.0


def _keys_file() -> pathlib.Path | None:
    candidates = [
        pathlib.Path(os.environ["OUROBOROS_KEYS_FILE"]).expanduser()
        if os.environ.get("OUROBOROS_KEYS_FILE", "").strip()
        else None,
        DATA.parent / "file1.txt",
        pathlib.Path.home() / "ouro" / "file1.txt",
        pathlib.Path.home() / "file1.txt",
    ]
    return next((path for path in candidates if path is not None and path.is_file()), None)


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

    def _fallback(env_name: str, prefix: str) -> None:
        if os.environ.get(env_name, "").strip():
            return
        f1 = _keys_file()
        if f1 is None:
            return
        for line in f1.read_text(encoding="utf-8").splitlines():
            if line.strip().lower().startswith(prefix + ":"):
                os.environ[env_name] = line.split(":", 1)[1].strip()
                break

    _fallback("OPENAI_API_KEY", "openai")
    _fallback("ANTHROPIC_API_KEY", "anthropic")
    _fallback("OPENROUTER_API_KEY", "openrouter")
    if not os.environ.get("TOTAL_BUDGET", "").strip():
        print(
            "WARN: TOTAL_BUDGET is not configured (settings.json not found?) — "
            "the $10 default will starve a full triad+scope run. Export "
            "OUROBOROS_SETTINGS_PATH or TOTAL_BUDGET explicitly.",
            file=sys.stderr,
        )


def _openrouter_pool() -> list[tuple[str, str]]:
    """Named OpenRouter candidates: env/settings first, pool order, hope* last."""
    pool: list[tuple[str, str]] = []
    env_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if env_key:
        pool.append(("<env/settings>", env_key))
    f1 = _keys_file()
    if f1 is not None:
        for line in f1.read_text(encoding="utf-8").splitlines():
            match = re.match(
                r"^\s*([A-Za-z0-9_.-]*openrouter[A-Za-z0-9_.-]*)\s*:\s*(\S+)\s*$", line, re.I
            )
            if match and match.group(2) not in {token for _, token in pool}:
                pool.append((match.group(1), match.group(2)))
    return sorted(pool, key=lambda item: "hope" in item[0].lower())


def _openrouter_key_health(token: str) -> tuple[bool, str]:
    """Probe `limit_remaining` for one key. Returns (healthy, detail)."""
    try:
        import httpx

        response = httpx.get(
            "https://openrouter.ai/api/v1/key",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except Exception as exc:
        return False, f"probe_error:{type(exc).__name__}"
    if response.status_code == 403:
        return False, "forbidden_tos"
    if response.status_code != 200:
        return False, f"http_{response.status_code}"
    try:
        data = (response.json() or {}).get("data") or {}
    except Exception:
        return False, "unreadable_body"
    if data.get("limit") is None:
        return True, "uncapped"
    try:
        remaining = float(data.get("limit_remaining"))
    except (TypeError, ValueError):
        return False, "unreadable_limit"
    if remaining < _OPENROUTER_MIN_REMAINING_USD:
        return False, f"remaining_below_${_OPENROUTER_MIN_REMAINING_USD:g}"
    return True, f"remaining_ok(>=${_OPENROUTER_MIN_REMAINING_USD:g})"


def _select_healthy_openrouter_key() -> None:
    """Pick the first healthy key from the allowed pool (values never printed)."""
    pool = _openrouter_pool()
    if not pool:
        print("WARN: no OpenRouter key candidates found.", file=sys.stderr)
        return
    for name, token in pool:
        healthy, detail = _openrouter_key_health(token)
        print(f"OpenRouter key {name!r}: {detail}", file=sys.stderr)
        if healthy:
            os.environ["OPENROUTER_API_KEY"] = token
            return
    print(
        "WARN: no healthy OpenRouter key in the allowed pool — reviewers routed "
        "through OpenRouter will fail closed. Fix keys and rerun (exit 3 class).",
        file=sys.stderr,
    )


def _create_isolated_checkout(staged_patch: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Detached worktree at HEAD with the staged diff applied to its index.

    The review then reads a frozen tree: edits in the primary worktree during
    the run cannot change what the reviewers see.
    """
    checkout_root = pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-review-checkout-"))
    checkout = checkout_root / "repo"
    add = subprocess.run(
        ["git", "worktree", "add", "--detach", str(checkout), "HEAD"],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    if add.returncode != 0:
        raise RuntimeError(f"worktree add failed: {add.stderr.strip()}")
    if staged_patch.strip():
        apply = subprocess.run(
            ["git", "apply", "--index", "--whitespace=nowarn", "--binary"],
            cwd=str(checkout), input=staged_patch, capture_output=True, text=True, timeout=120,
        )
        if apply.returncode != 0:
            raise RuntimeError(
                f"staged diff did not apply to the isolated checkout: {apply.stderr.strip()}"
            )
    return checkout_root, checkout


def _remove_isolated_checkout(checkout_root: pathlib.Path, checkout: pathlib.Path) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(checkout)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
    )
    shutil.rmtree(checkout_root, ignore_errors=True)


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
        "runtime_mode": os.environ.get("OUROBOROS_RUNTIME_MODE", ""),
    }


def _classify_exit(outcome: dict) -> int:
    if str(outcome.get("status") or "") == "passed":
        return 0
    block_reason = str(outcome.get("block_reason") or "")
    if block_reason in _GENUINE_BLOCK_REASONS:
        return 1
    # A scope CRITICAL with concrete findings is a genuine reviewer verdict
    # even when the triad passed; a findings-less scope block is fail-closed
    # infrastructure (crash, oversized prompt, sub-floor context).
    if block_reason == "scope_blocked" and outcome.get("combined_findings"):
        return 1
    return 3


def main() -> int:
    import argparse

    version = (REPO / "VERSION").read_text(encoding="utf-8").strip()
    parser = argparse.ArgumentParser(
        description="Real advisory+triad+scope commit-gate dry-run on the staged diff (no commit)."
    )
    parser.add_argument(
        "commit_message",
        nargs="?",
        default=f"release: Ouroboros v{version} deep core capability release",
    )
    parser.add_argument(
        "--output",
        default="",
        help=(
            "Directory for full review artifacts. Defaults to a new append-only "
            "run directory under ~/ouro/review_runs/."
        ),
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
    parser.add_argument(
        "--no-isolated-checkout",
        action="store_true",
        help=(
            "Review the primary worktree directly instead of a frozen detached "
            "checkout. WARNING: the production cycle stages EVERYTHING (staged + "
            "unstaged + untracked) there and unstages your index when it finishes."
        ),
    )
    args = parser.parse_args()

    _load_settings_into_env()
    _select_healthy_openrouter_key()
    resolved_config = _resolved_review_config()
    print(
        "Resolved review config: "
        + json.dumps(resolved_config, ensure_ascii=False),
        file=sys.stderr,
    )

    staged = subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=str(REPO), capture_output=True, text=True
    ).stdout
    if not staged.strip():
        print("ERROR: staged diff is empty — `git add` the changes first.", file=sys.stderr)
        return 2

    from ouroboros.tools.claude_advisory_review import _MAX_DIFF_CHARS_ERROR

    if len(staged) > _MAX_DIFF_CHARS_ERROR:
        print(
            f"ERROR: staged diff is {len(staged):,} chars — over the advisory hard cap "
            f"({_MAX_DIFF_CHARS_ERROR:,}). Policy: split the phase into smaller "
            "single-intent commits instead of relaxing the gate.",
            file=sys.stderr,
        )
        return 3

    sha8 = subprocess.run(
        ["git", "rev-parse", "--short=8", "HEAD"], cwd=str(REPO), capture_output=True, text=True
    ).stdout.strip() or "nohead"
    output_dir = pathlib.Path(
        args.output
        or pathlib.Path.home()
        / "ouro"
        / "review_runs"
        / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}_{sha8}"
    ).expanduser().resolve(strict=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    from ouroboros.tools.registry import ToolContext

    review_drive_root = (
        pathlib.Path(args.drive_root).expanduser().resolve(strict=False)
        if args.drive_root
        else pathlib.Path(tempfile.mkdtemp(prefix="ouroboros-external-review-"))
    )
    review_drive_root.mkdir(parents=True, exist_ok=True)
    (review_drive_root / "logs").mkdir(parents=True, exist_ok=True)

    checkout_root: pathlib.Path | None = None
    checkout: pathlib.Path | None = None
    repo_for_review = REPO
    if not args.no_isolated_checkout:
        try:
            checkout_root, checkout = _create_isolated_checkout(staged)
            repo_for_review = checkout
            print(f"Isolated review checkout: {checkout}", file=sys.stderr)
        except Exception as exc:
            print(f"ERROR: isolated checkout failed: {exc}", file=sys.stderr)
            (output_dir / "outcome.json").write_text(
                json.dumps({
                    "exit_code": 3,
                    "outcome": {"status": "blocked", "block_reason": "isolated_checkout_failed"},
                }, indent=2) + "\n",
                encoding="utf-8",
            )
            return 3

    ctx = ToolContext(repo_dir=repo_for_review, drive_root=review_drive_root)
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
    try:
        # Stage 1: the REAL advisory pre-review — the same gate production
        # requires. Its freshness state lives in this run's drive root, so the
        # cycle below sees a fresh advisory instead of forcing the hermetic
        # pytest substitute.
        if not os.environ.get("ANTHROPIC_API_KEY", "").strip():
            print(
                "WARN: no ANTHROPIC_API_KEY — advisory will record an audited "
                "bypass and the gate falls back to its hermetic pytest preflight.",
                file=sys.stderr,
            )
        from ouroboros.tools.claude_advisory_review import _handle_advisory_pre_review

        advisory_text = _handle_advisory_pre_review(
            ctx, commit_message=commit_message, goal=goal, scope=scope,
        )
        (output_dir / "advisory.txt").write_text(str(advisory_text) + "\n", encoding="utf-8")
        print("=" * 80 + "\nADVISORY PRE-REVIEW (full)\n" + "=" * 80 + f"\n{advisory_text}")

        from ouroboros.tools.git import _run_non_committing_review_cycle

        outcome = _run_non_committing_review_cycle(
            ctx,
            commit_message,
            goal=goal,
            scope=scope,
        )
        if checkout is not None:
            # The cycle may auto-sync release metadata (version carriers) in the
            # checkout; a drifted tree means reviewers approved MORE than the
            # operator's staged patch — surface that loudly. The cycle's final
            # ``git reset HEAD`` turns NEW files untracked, and ``git diff HEAD``
            # would not show them — re-stage everything so the comparison is
            # homogeneous with the operator's staged patch.
            subprocess.run(
                ["git", "add", "-A"],
                cwd=str(checkout), capture_output=True, text=True, timeout=120,
            )
            post_tree = subprocess.run(
                ["git", "diff", "--cached", "--binary"],
                cwd=str(checkout), capture_output=True, text=True, timeout=120,
            ).stdout
            if post_tree.strip() != staged.strip():
                print(
                    "WARN: the reviewed checkout tree drifted from the staged "
                    "patch (release-metadata auto-sync?). Reconcile the primary "
                    "worktree before committing what was reviewed.",
                    file=sys.stderr,
                )
                (output_dir / "reviewed-tree-drift.diff").write_text(
                    post_tree, encoding="utf-8",
                )
    finally:
        if checkout_root is not None and checkout is not None:
            _remove_isolated_checkout(checkout_root, checkout)

    evidence_refs, cost_report = _review_evidence_and_cost(ctx)
    exit_code = _classify_exit(outcome)

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
            "complete": exit_code == 0,
            "exit_code": exit_code,
            "exit_class": {
                0: "passed",
                1: "genuine_review_block",
                3: "infrastructure",
            }.get(exit_code, "unknown"),
            "production_outcome": outcome,
            "scope_model": getattr(ctx, "_last_scope_model", ""),
            "raw_evidence_refs": evidence_refs,
            "cost_report": cost_report,
            "elapsed_sec": round(time.time() - t0, 1),
        }, indent=2, ensure_ascii=False, default=str),
    ])
    print(out)
    (output_dir / "full-output.txt").write_text(out + "\n", encoding="utf-8")
    (output_dir / "outcome.json").write_text(
        json.dumps(
            {"exit_code": exit_code, "outcome": outcome},
            indent=2, ensure_ascii=False, default=str,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"Artifacts: {output_dir}", file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        # An uncaught crash is infrastructure, never a reviewer verdict.
        sys.exit(3)
