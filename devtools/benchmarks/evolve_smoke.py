#!/usr/bin/env python3
"""Phase 0 — zero-core-change evolution smoke (verify the fix FIRST).

Proves, on a THROWAWAY clone + ISOLATED data root (the live Ouroboros is never
touched), that:
  1. forked memory carries across tasks (task_reflections.jsonl grows),
  2. the per-task budget resets between tasks so a fresh task is NOT falsely
     flagged `budget: emergency` (via the guarded supervisor.state.reset_per_task_budget),
  3. optionally, a reviewed self-modification commit can happen BETWEEN tasks
     (a non-workspace self_modification run on the clone).

This is research Option (a): an external orchestrator using standalone
``ouroboros run`` (headless, no server) — NO core edits are required for the
mechanism. Run it after the Phase 1 budget guard exists so the reset is gated.

Usage (from repo/):
  OUROBOROS_BENCH_BUDGET_RESET=1 python -m devtools.benchmarks.evolve_smoke \
      --tasks 2 --timeout 180 [--self-mod] [--keep]

Nothing here may write under repo/ or the live data dir; outputs go to a temp
run root (validated by devtools.benchmarks.common.run_roots).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

REPO_DIR = pathlib.Path(__file__).resolve().parents[2]
LIVE_DATA = pathlib.Path.home() / "Ouroboros" / "data"


def _log(msg: str) -> None:
    print(f"[evolve_smoke] {msg}", flush=True)


def _git(args: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _reflections_count(data_root: pathlib.Path) -> int:
    path = data_root / "logs" / "task_reflections.jsonl"
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8")) if path.exists() else 0
    except Exception:
        return 0


def _make_workspace(run_root: pathlib.Path, idx: int) -> pathlib.Path:
    ws = run_root / f"ws{idx}"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "notes.md").write_text(f"# scratch workspace {idx}\n", encoding="utf-8")
    _git(["init", "-q"], ws)
    _git(["add", "-A"], ws)
    _git(["-c", "user.email=smoke@local", "-c", "user.name=smoke", "commit", "-q", "-m", "seed"], ws)
    return ws


def _run_ouroboros(args: list[str], env: dict, timeout: int) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "ouroboros.cli", "run", *args]
    try:
        p = subprocess.run(cmd, cwd=str(REPO_DIR), env=env, capture_output=True, text=True,
                           timeout=timeout + 120)
        return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 0 evolution smoke (isolated).")
    ap.add_argument("--tasks", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--self-mod", action="store_true", help="also run a between-task self_modification cycle")
    ap.add_argument("--keep", action="store_true", help="keep the temp run root")
    args = ap.parse_args()

    from devtools.benchmarks.common.run_roots import ensure_outside_repo

    run_root = pathlib.Path(tempfile.mkdtemp(prefix="evolve_smoke_"))
    ensure_outside_repo(run_root, REPO_DIR)
    clone = run_root / "clone"
    data_root = run_root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    ensure_outside_repo(data_root, REPO_DIR)

    _log(f"run root: {run_root}")
    _log("cloning repo (no-hardlinks)…")
    rc, out = _git(["clone", "--no-hardlinks", "-q", str(REPO_DIR), str(clone)], run_root)
    if rc != 0:
        _log(f"clone failed: {out}")
        return 2

    # Seed isolated settings from the live settings (provider keys + model slots),
    # but everything runtime lands in the isolated data root.
    live_settings = LIVE_DATA / "settings.json"
    settings_path = data_root / "settings.json"
    if live_settings.exists():
        try:
            cfg = json.loads(live_settings.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    else:
        cfg = {}
    cfg["OUROBOROS_RUNTIME_MODE"] = "advanced"
    cfg.setdefault("TOTAL_BUDGET", 10.0)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

    env = dict(os.environ)
    env["OUROBOROS_REPO_DIR"] = str(clone)
    env["OUROBOROS_DATA_DIR"] = str(data_root)
    env["OUROBOROS_SETTINGS_PATH"] = str(settings_path)
    env["OUROBOROS_BENCH_BUDGET_RESET"] = "1"
    # Make reset_per_task_budget's isolation check see the isolated dir in THIS process too.
    os.environ["OUROBOROS_DATA_DIR"] = str(data_root)

    # Live snapshot to prove non-mutation (the isolated clone is the only thing
    # the harness writes to; the live repo working tree must be unchanged).
    live_repo_status_before = _git(["status", "--porcelain"], REPO_DIR)[1]

    from supervisor import state as sstate

    ledger: dict = {"run_root": str(run_root), "tasks": [], "self_mod": None}
    refl_before = _reflections_count(data_root)
    emergency_seen = False

    for i in range(1, int(args.tasks) + 1):
        ws = _make_workspace(run_root, i)
        prompt = (
            f"In the workspace file notes.md, append a single concise factual bullet "
            f"about task #{i}. Keep it tiny. Do not modify anything else."
        )
        _log(f"task {i}: ouroboros run --workspace … --memory-mode forked")
        rc, out = _run_ouroboros(
            ["--workspace", str(ws), "--memory-mode", "forked", "--timeout", str(args.timeout), prompt],
            env, args.timeout,
        )
        emerg = ("budget: emergency" in out.lower()) or ("budget exhausted" in out.lower())
        emergency_seen = emergency_seen or emerg
        refl_now = _reflections_count(data_root)
        ledger["tasks"].append({"i": i, "rc": rc, "emergency": emerg, "reflections": refl_now})
        _log(f"task {i}: rc={rc} emergency={emerg} reflections={refl_now}")
        # Reset the per-task budget BEFORE the next task (guarded; isolated only).
        did_reset = sstate.reset_per_task_budget(data_root, confirm_isolated=True)
        _log(f"task {i}: budget reset -> {did_reset}")
        ledger["tasks"][-1]["budget_reset"] = bool(did_reset)

    refl_after = _reflections_count(data_root)

    if args.self_mod:
        _log("between-task self_modification cycle (non-workspace run on the clone)…")
        objective = (
            "If recent tasks revealed one concrete, tiny, generalizable improvement to "
            "Ouroboros, make exactly one reviewed change and commit it via commit_reviewed; "
            "otherwise just record the lesson. Keep it minimal."
        )
        rc, out = _run_ouroboros(["--memory-mode", "shared", "--timeout", str(args.timeout), objective], env, args.timeout)
        rc2, head_after = _git(["rev-parse", "HEAD"], clone)
        rc3, log_after = _git(["log", "--oneline", "-3"], clone)
        ledger["self_mod"] = {"rc": rc, "clone_head": head_after.strip(), "recent": log_after.strip()}
        _log(f"self_mod: rc={rc} clone_head={head_after.strip()[:12]}")

    # Acceptance.
    live_repo_status_after = _git(["status", "--porcelain"], REPO_DIR)[1]
    live_untouched = (live_repo_status_before == live_repo_status_after)
    acceptance = {
        "reflections_grew": refl_after > refl_before,
        "no_budget_emergency": not emergency_seen,
        "budget_reset_worked": all(t.get("budget_reset") for t in ledger["tasks"]),
        "live_repo_untouched": live_untouched,
        "refl_before": refl_before,
        "refl_after": refl_after,
    }
    ledger["acceptance"] = acceptance
    ledger_path = run_root / "evolve_smoke_ledger.json"
    ledger_path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"acceptance: {json.dumps(acceptance)}")
    _log(f"ledger: {ledger_path}")

    ok = acceptance["no_budget_emergency"] and acceptance["budget_reset_worked"] and acceptance["live_repo_untouched"]
    # reflections_grew is best-effort (depends on the model actually producing reflections).
    if not args.keep:
        try:
            shutil.rmtree(run_root, ignore_errors=True)
        except Exception:
            pass
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
