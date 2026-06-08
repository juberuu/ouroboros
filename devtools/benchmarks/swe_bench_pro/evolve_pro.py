#!/usr/bin/env python3
"""SWE-bench Pro EVOLUTIONARY driver (isolated; live Ouroboros never touched).

Generalizes the Phase 0 `devtools/benchmarks/evolve_smoke.py` harness from
scratch workspaces into real SWE-bench Pro instances, and wires the C1 post-task
self-evolution loop between instances. The hypothesis under test (Anton's original
goal): solving instances *in sequence with one self-improvement cycle between each*
yields a better cumulative result than independent frozen runs.

What it does, per ordered instance:
  1. prepare an external workspace (a prepared `repo_dir`, or `repo_url`+`base_commit`
     cloned into the isolated run root) checked out at `base_commit`;
  2. run standalone `ouroboros run --workspace <repo> --memory-mode forked` on the
     instance's `problem_statement` (headless, no server, isolated data + clone) —
     external workspaces forbid `shared`, but forked carries the isolated canonical
     memory in and writes reflections back, so learning accumulates across instances;
  3. capture a grade_pro-compatible `model_patch` via `capture_patch.sh`;
  4. reset the per-task budget (guarded; isolated data root ONLY) so the next
     instance is not falsely flagged `budget: emergency`;
  5. between instances (with --evolve-between, default on) it drives ONE explicit
     self-evolution cycle on the clone — a non-workspace shared-memory `ouroboros run`
     where the agent may commit ONE reviewed improvement or record the lesson; learned
     code/memory carries into the next solve. The live body is never modified.
     (The headless CLI runs the worker but not the supervisor tick that would apply
     the C1 post-task signal, so the cycle is driven explicitly here.)

Outputs (under an isolated run root, never repo/ or live data):
  - `predictions.jsonl`  — feed straight to `grade_pro.py --predictions ...`
  - `evolve_pro_ledger.json` — per-instance rc/patch-bytes/budget-reset + acceptance
  - `run_manifest.json`  — non-secret provenance

Usage (from repo/):
  python -m devtools.benchmarks.swe_bench_pro.evolve_pro \\
      --instances instances.jsonl --memory-mode forked --timeout 1800
  # no dataset handy? a self-contained smoke that exercises the whole loop:
  python -m devtools.benchmarks.swe_bench_pro.evolve_pro --demo 2 --timeout 180

Each `instances.jsonl` row:
  {"instance_id": "...", "repo_dir": "/prepared/app", "base_commit": "<sha>",
   "problem_statement": "..."}            # or "repo_url" instead of "repo_dir"

Grading (official scorer = source of truth) stays in `grade_pro.py`; this driver
only produces the predictions + provenance for it.
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

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from devtools.benchmarks.common.manifests import benchmark_run_manifest, write_json
from devtools.benchmarks.common.result_index import task_result_row, write_result_index
from devtools.benchmarks.common.run_roots import ensure_outside_repo, safe_benchmark_id

REPO_DIR = pathlib.Path(__file__).resolve().parents[3]
LIVE_DATA = pathlib.Path.home() / "Ouroboros" / "data"
CAPTURE = pathlib.Path(__file__).resolve().parent / "capture_patch.sh"


def _log(msg: str) -> None:
    print(f"[evolve_pro] {msg}", flush=True)


def _git(args: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _reflections_count(data_root: pathlib.Path) -> int:
    path = data_root / "logs" / "task_reflections.jsonl"
    try:
        return sum(1 for _ in path.open("r", encoding="utf-8")) if path.exists() else 0
    except OSError:
        return 0


def _rows(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _seed_settings(data_root: pathlib.Path) -> pathlib.Path:
    """Isolated settings seeded from live (provider keys + model slots), but every
    runtime artifact lands in the isolated data root. Post-task evolution is ENABLED
    here because the data root is a throwaway (the guard refuses the live root)."""
    settings_path = data_root / "settings.json"
    cfg: dict = {}
    live = LIVE_DATA / "settings.json"
    if live.exists():
        try:
            cfg = json.loads(live.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            cfg = {}
    cfg["OUROBOROS_RUNTIME_MODE"] = "advanced"
    cfg["OUROBOROS_POST_TASK_EVOLUTION"] = True
    cfg.setdefault("OUROBOROS_POST_TASK_EVOLUTION_CADENCE", "llm")
    cfg.setdefault("TOTAL_BUDGET", 50.0)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings_path


def _run_ouroboros(args: list[str], env: dict, timeout: int) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "ouroboros.cli", "run", *args]
    try:
        p = subprocess.run(cmd, cwd=str(REPO_DIR), env=env, capture_output=True, text=True,
                           timeout=timeout + 120)
        return p.returncode, (p.stdout or "") + "\n" + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "TIMEOUT"


def _make_demo_instances(run_root: pathlib.Path, n: int) -> list[dict]:
    """Self-contained instances (no dataset/Docker) that still drive the full loop:
    a tiny repo with a failing function the agent is asked to fix."""
    rows: list[dict] = []
    for i in range(1, n + 1):
        repo = run_root / "demo_instances" / f"app{i}"
        repo.mkdir(parents=True, exist_ok=True)
        (repo / "calc.py").write_text(
            f"def add(a, b):\n    return a - b  # BUG #{i}: should add\n", encoding="utf-8"
        )
        _git(["init", "-q"], repo)
        _git(["add", "-A"], repo)
        _git(["-c", "user.email=bench@local", "-c", "user.name=bench", "commit", "-q", "-m", "seed"], repo)
        base = _git(["rev-parse", "HEAD"], repo)[1].strip()
        rows.append({
            "instance_id": f"demo-{i:03d}",
            "repo_dir": str(repo),
            "base_commit": base,
            "problem_statement": "calc.add(a, b) must return the SUM a + b, but it subtracts. "
                                 "Fix the bug in calc.py. Do not edit anything else.",
        })
    return rows


def _prepare_workspace(item: dict, run_root: pathlib.Path) -> tuple[pathlib.Path, str]:
    """Return (repo_dir, base_commit) for an instance, cloning repo_url if needed."""
    base_commit = str(item.get("base_commit") or "").strip()
    repo_dir = str(item.get("repo_dir") or item.get("workspace_root") or "").strip()
    if repo_dir:
        repo = pathlib.Path(repo_dir).expanduser()
        if not (repo / ".git").is_dir():
            raise RuntimeError(f"repo_dir is not a git checkout: {repo}")
    else:
        repo_url = str(item.get("repo_url") or "").strip()
        if not repo_url or not base_commit:
            raise RuntimeError("row needs repo_dir, or repo_url + base_commit")
        iid = safe_benchmark_id(str(item.get("instance_id") or ""))
        repo = run_root / "instances" / iid
        repo.parent.mkdir(parents=True, exist_ok=True)
        rc, out = _git(["clone", "--no-hardlinks", "-q", repo_url, str(repo)], run_root)
        if rc != 0:
            raise RuntimeError(f"clone failed for {repo_url}: {out}")
    if base_commit:
        rc, out = _git(["checkout", "-q", base_commit], repo)
        if rc != 0:
            raise RuntimeError(f"checkout {base_commit} failed: {out}")
    else:
        base_commit = _git(["rev-parse", "HEAD"], repo)[1].strip()
    return repo, base_commit


def _capture_patch(repo: pathlib.Path, base_commit: str, out_path: pathlib.Path) -> str:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["bash", str(CAPTURE), str(repo), base_commit, str(out_path)],
                          capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise RuntimeError(f"capture_patch.sh failed: {proc.stderr or proc.stdout}")
    patch = out_path.read_text(encoding="utf-8", errors="replace")
    if not patch.strip():
        raise RuntimeError("capture_patch.sh produced an empty patch")
    return patch


def _solve_instance(item: dict, run_root: pathlib.Path, patch_dir: pathlib.Path,
                    env: dict, memory_mode: str, timeout: int) -> tuple[dict, dict, dict | None]:
    """Run one instance end-to-end. Returns (ledger_row, prediction|{}, error|None)."""
    iid = str(item.get("instance_id") or "").strip()
    try:
        repo, base_commit = _prepare_workspace(item, run_root)
        problem = str(item.get("problem_statement") or "").strip()
        if not iid or not problem:
            raise RuntimeError("row needs instance_id and problem_statement")
        rc, out = _run_ouroboros(
            ["--workspace", str(repo), "--memory-mode", memory_mode, "--timeout", str(timeout), problem],
            env, timeout,
        )
        patch_out = patch_dir / f"{safe_benchmark_id(iid)}.diff"
        patch = _capture_patch(repo, base_commit, patch_out)
        emergency = ("budget: emergency" in out.lower()) or ("budget exhausted" in out.lower())
        prediction = {"instance_id": iid, "model_name_or_path": "ouroboros-pro-evolve", "model_patch": patch}
        row = task_result_row(
            benchmark="swe_bench_pro", instance_id=iid, status="completed",
            reason_code="patch_generated", prediction_written=True, official_eval_status="pending",
            output_paths={"patch": str(patch_out)},
            details={"rc": rc, "patch_bytes": len(patch.encode("utf-8", "replace")), "budget_emergency": emergency},
        )
        return row, prediction, None
    except Exception as exc:  # noqa: BLE001 — driver records the failure, keeps going
        reason = "empty_patch" if "empty patch" in str(exc) else "failed"
        row = task_result_row(benchmark="swe_bench_pro", instance_id=iid, status=reason,
                              reason_code=reason, error=str(exc))
        return row, {}, {"instance_id": iid, "error": str(exc), "reason_code": reason}


def _evolve_cycle(env: dict, timeout: int) -> dict:
    """One between-instance self-evolution cycle on the CLONE (non-workspace, shared
    memory). The headless CLI runs the worker but not the supervisor tick that would
    apply the C1 post-task signal, so — like evolve_smoke — we drive the cycle
    explicitly: the agent may commit ONE reviewed improvement to the clone or just
    record the lesson. Returns {rc, clone_head, committed}."""
    clone = pathlib.Path(env["OUROBOROS_REPO_DIR"])
    head_before = _git(["rev-parse", "HEAD"], clone)[1].strip()
    objective = (
        "Reflect on the SWE-bench Pro instance you just solved. If it revealed ONE "
        "concrete, tiny, generalizable improvement to Ouroboros, make exactly one "
        "reviewed change and commit it via commit_reviewed; otherwise record the "
        "lesson in memory. Keep it minimal."
    )
    rc, _out = _run_ouroboros(["--memory-mode", "shared", "--timeout", str(timeout), objective], env, timeout)
    head_after = _git(["rev-parse", "HEAD"], clone)[1].strip()
    return {"rc": rc, "clone_head": head_after, "committed": head_after != head_before}


def main() -> int:
    ap = argparse.ArgumentParser(description="SWE-bench Pro evolutionary driver (isolated).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--instances", help="JSONL of instance rows")
    src.add_argument("--demo", type=int, metavar="N", help="synthesize N self-contained demo instances")
    # External-workspace tasks forbid `shared` (gateway/tasks.py); memory still
    # carries across instances via the PERSISTENT isolated data root (a forked task
    # writes its reflections back to the canonical isolated memory).
    ap.add_argument("--memory-mode", default="forked", choices=["forked", "empty"])
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--evolve-between", action=argparse.BooleanOptionalAction, default=True,
                    help="run a self-evolution cycle on the clone between instances (default on)")
    ap.add_argument("--keep", action="store_true", help="keep the temp run root (default for real instances)")
    args = ap.parse_args()

    run_root = pathlib.Path(tempfile.mkdtemp(prefix="evolve_pro_"))
    ensure_outside_repo(run_root, REPO_DIR)
    clone = run_root / "clone"
    data_root = run_root / "data"
    data_root.mkdir(parents=True, exist_ok=True)
    ensure_outside_repo(data_root, REPO_DIR)
    patch_dir = run_root / "patches"
    _log(f"run root: {run_root}")

    rc, out = _git(["clone", "--no-hardlinks", "-q", str(REPO_DIR), str(clone)], run_root)
    if rc != 0:
        _log(f"clone failed: {out}")
        return 2
    settings_path = _seed_settings(data_root)

    env = dict(os.environ)
    env["OUROBOROS_REPO_DIR"] = str(clone)
    env["OUROBOROS_DATA_DIR"] = str(data_root)
    env["OUROBOROS_SETTINGS_PATH"] = str(settings_path)
    env["OUROBOROS_BENCH_BUDGET_RESET"] = "1"
    os.environ["OUROBOROS_DATA_DIR"] = str(data_root)  # so the budget guard sees the isolated dir here too

    instances = _make_demo_instances(run_root, int(args.demo)) if args.demo else _rows(pathlib.Path(args.instances).expanduser())
    if not instances:
        _log("no instances")
        return 2

    from supervisor import state as sstate

    live_status_before = _git(["status", "--porcelain"], REPO_DIR)[1]
    refl_before = _reflections_count(data_root)
    predictions: list[dict] = []
    ledger_rows: list[dict] = []
    errors: list[dict] = []
    budget_resets: list[bool] = []
    evolution_cycles: list[dict] = []

    for n, item in enumerate(instances, 1):
        _log(f"instance {n}/{len(instances)}: {item.get('instance_id')}")
        row, prediction, error = _solve_instance(item, run_root, patch_dir, env, args.memory_mode, args.timeout)
        ledger_rows.append(row)
        if prediction:
            predictions.append(prediction)
        if error:
            errors.append(error)
        did_reset = sstate.reset_per_task_budget(data_root, confirm_isolated=True)
        budget_resets.append(bool(did_reset))
        _log(f"instance {n}: status={row.get('status')} budget_reset={did_reset}")
        # Self-evolution between instances (not after the last): learned code/memory
        # carries into the next solve. Budget is reset again afterwards.
        if args.evolve_between and n < len(instances):
            evo = _evolve_cycle(env, args.timeout)
            evolution_cycles.append(evo)
            ledger_rows.append(task_result_row(
                benchmark="swe_bench_pro", instance_id=f"evolve-after-{n}",
                status="completed", reason_code="evolution_cycle", details=evo))
            sstate.reset_per_task_budget(data_root, confirm_isolated=True)
            _log(f"evolve after {n}: committed={evo.get('committed')} rc={evo.get('rc')}")

    refl_after = _reflections_count(data_root)
    live_status_after = _git(["status", "--porcelain"], REPO_DIR)[1]
    acceptance = {
        "instances": len(instances),
        "predictions": len(predictions),
        "errors": len(errors),
        "budget_reset_worked": all(budget_resets) if budget_resets else False,
        "live_repo_untouched": live_status_before == live_status_after,
        "reflections_grew": refl_after > refl_before,
        "refl_before": refl_before,
        "refl_after": refl_after,
        "evolution_cycles": len(evolution_cycles),
        "evolution_commits": sum(1 for e in evolution_cycles if e.get("committed")),
    }

    predictions_path = run_root / "predictions.jsonl"
    predictions_path.write_text(
        "".join(json.dumps(p, ensure_ascii=False) + "\n" for p in predictions), encoding="utf-8"
    )
    write_result_index(run_root / "result_index.jsonl", ledger_rows)
    write_json(run_root / "run_manifest.json", benchmark_run_manifest(
        benchmark="swe_bench_pro", run_root=run_root, repo_dir=REPO_DIR,
        requested_task_ids=[str(i.get("instance_id") or "") for i in instances],
        output_paths={"predictions": str(predictions_path), "patch_dir": str(patch_dir)},
        dataset="ScaleAI/SWE-bench_Pro", timeout_sec=int(args.timeout),
        isolated_data_root=str(data_root), settings_path=settings_path,
        extra={"mode": "evolutionary", "memory_mode": args.memory_mode, "acceptance": acceptance},
    ))
    (run_root / "evolve_pro_ledger.json").write_text(
        json.dumps({"run_root": str(run_root), "acceptance": acceptance, "errors": errors}, indent=2),
        encoding="utf-8",
    )
    _log(f"acceptance: {json.dumps(acceptance)}")
    _log(f"predictions: {predictions_path}")
    _log("grade with: python -m devtools.benchmarks.swe_bench_pro.grade_pro "
         f"--predictions {predictions_path}")

    ok = acceptance["budget_reset_worked"] and acceptance["live_repo_untouched"] and bool(predictions)
    if not args.keep and args.demo:
        shutil.rmtree(run_root, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
