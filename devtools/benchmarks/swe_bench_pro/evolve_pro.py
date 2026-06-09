#!/usr/bin/env python3
"""SWE-bench Pro driver (B-full, production-faithful, isolated) + best-effort evolution.

Solves SWE-bench Pro instances in sequence on a THROWAWAY Ouroboros clone + isolated
data root, driving a REAL isolated production server so each instance runs on the
current (potentially evolved) code. The live Ouroboros is never touched.

How (vs the old headless driver, which silently attached to the live server):
  1. git clone --no-hardlinks the repo (kept on branch `ouroboros` so a reviewed
     self-mod commit survives safe_restart's checkout); origin removed (no push-back);
     isolated data + settings (provider keys from live; post-task evolution enabled in
     settings; runtime advanced).
  2. Spawn a REAL isolated `server.py` on a free port (devtools/benchmarks/common/
     server_runner.IsolatedServer); seed state.json owner_chat_id ONLY (the /api/tasks
     path never binds it, and the post-task loop's apply_pending_request enables the
     one-shot campaign after a qualifying task — idle evolution is NOT pre-enabled).
  3. Per instance: checkout base_commit, POST /api/tasks (workspace=instance,
     memory_mode=forked), poll to terminal, capture a grade_pro-compatible model_patch,
     reset the per-task budget (guarded; isolated root only) before the next instance.

KNOWN LIMITATION — cross-task self-evolution is DEFERRED (owner-decided for rc.3: ship
the isolation/hardening now, evolve-between-instances as a follow-up):
  Each instance is submitted as an EXTERNAL WORKSPACE, so api_tasks_create derives a
  project_id, and the Phase-3 leak guard (agent_task_pipeline: `maybe_promote` runs only
  when `not project_id`) intentionally SKIPS post-task promotion for project-scoped tasks
  so project work never touches GLOBAL evolution state. Consequence: the between-instance
  post-task evolution loop does NOT fire for benchmark instances — `wait_for_absorb`
  normally returns no_promotion. The driver still delivers its core value: faithful
  isolation, a real server, solve + grade_pro-compatible capture, guarded budget reset,
  and a live body that is never touched. Making isolated-benchmark instances actually
  feed self-evolution (e.g. an isolated-root opt-in that permits promotion for throwaway
  project tasks) is a tracked follow-up — see METHODOLOGY.md.

Outputs (under an isolated run root, never repo/ or live data): predictions.jsonl
(feed straight to grade_pro.py), result_index.jsonl, run_manifest.json, ledger.

Usage (from repo/):
  python -m devtools.benchmarks.swe_bench_pro.evolve_pro \\
      --instances instances.jsonl --timeout 1800
  # no dataset/Docker handy? a self-contained smoke of the whole loop:
  python -m devtools.benchmarks.swe_bench_pro.evolve_pro --demo 2 --timeout 600 --keep

Each instances.jsonl row: {"instance_id","repo_dir" or "repo_url","base_commit","problem_statement"}.
Grading stays in grade_pro.py (official scorer = source of truth).
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
from devtools.benchmarks.common.server_runner import (
    IsolatedServer,
    absorbed_cycles_done,
    build_isolated_settings,
    seed_owner_state,
)

REPO_DIR = pathlib.Path(__file__).resolve().parents[3]
LIVE_DATA = pathlib.Path.home() / "Ouroboros" / "data"
# A custom/Drive-backed live data root from the LAUNCH env, captured at import BEFORE main()
# overrides OUROBOROS_DATA_DIR with the isolated root — so the pre-submit overlap guard in
# _prepare_workspace also refuses a repo_dir under a non-default live data root.
_LAUNCH_DATA_DIR = os.environ.get("OUROBOROS_DATA_DIR", "").strip()
CAPTURE = pathlib.Path(__file__).resolve().parent / "capture_patch.sh"


def _log(msg: str) -> None:
    print(f"[evolve_pro] {msg}", flush=True)


def _git(args: list[str], cwd: pathlib.Path) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _rows(path: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _seed_settings(data_root: pathlib.Path, cadence: str) -> pathlib.Path:
    """Isolated settings seeded from live (provider keys + model slots); post-task
    evolution ENABLED (the data root is a throwaway — the budget guard refuses the
    live root). runtime advanced so evolution is permitted."""
    settings_path = data_root / "settings.json"
    live_cfg: dict = {}
    live = LIVE_DATA / "settings.json"
    if live.exists():
        try:
            live_cfg = json.loads(live.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            live_cfg = {}
    # Copy ONLY provider/model/budget keys from live (build_isolated_settings drops owner
    # secrets + stale routing/host/port keys), then apply the isolated overrides. The
    # isolated data root is reachable by untrusted benchmark tasks, so no owner secrets here.
    cfg = build_isolated_settings(
        live_cfg,
        OUROBOROS_RUNTIME_MODE="advanced",
        OUROBOROS_POST_TASK_EVOLUTION="true",  # string, mirrors live settings.json
        OUROBOROS_POST_TASK_EVOLUTION_CADENCE=cadence,
    )
    cfg.setdefault("TOTAL_BUDGET", 50.0)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return settings_path


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
    from ouroboros.tool_access import paths_overlap_casefold

    base_commit = str(item.get("base_commit") or "").strip()
    repo_dir = str(item.get("repo_dir") or item.get("workspace_root") or "").strip()
    if repo_dir:
        repo = pathlib.Path(repo_dir).expanduser().resolve(strict=False)
        # Isolation (v6.24.0-rc.3): the later `git checkout <base_commit>` mutates whatever
        # repo_dir points at, BEFORE the isolated server's protection applies. NEVER let it
        # overlap the live Ouroboros repo or data (symlink/casefold-safe, both directions).
        forbidden_roots = [REPO_DIR, LIVE_DATA]
        if _LAUNCH_DATA_DIR:
            forbidden_roots.append(pathlib.Path(_LAUNCH_DATA_DIR).expanduser())
        for forbidden in forbidden_roots:
            if paths_overlap_casefold(repo, forbidden):
                raise RuntimeError(
                    f"repo_dir overlaps the live Ouroboros body/data ({forbidden}) — "
                    f"refusing to checkout/mutate it: {repo}")
        # Accept any git worktree root (a linked worktree has a `.git` FILE, not a dir),
        # mirroring the runtime contract (gateway/tasks.py uses `git rev-parse --show-toplevel`).
        rc, top = _git(["rev-parse", "--show-toplevel"], repo)
        if rc != 0 or not str(top or "").strip():
            raise RuntimeError(f"repo_dir is not a git worktree: {repo}")
        # Must be the worktree ROOT, not a subdir: gateway/tasks.py::_resolve_workspace_root
        # uses the top-level root, so a subdir would pass here yet be rejected at POST /api/tasks.
        if pathlib.Path(top.strip()).resolve(strict=False) != repo:
            raise RuntimeError(
                f"repo_dir must be the git worktree ROOT (top-level is {top.strip()}): {repo}")
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


class _DriverAbort(RuntimeError):
    """Fatal driver condition (e.g. a task that will not terminate even after cancel) —
    must STOP the run, not be recorded as a recoverable per-instance error and continued."""


def _solve_instance(server: IsolatedServer, item: dict, run_root: pathlib.Path,
                    patch_dir: pathlib.Path, memory_mode: str, timeout: int) -> tuple[dict, dict, dict | None]:
    """Submit one instance to the isolated server, wait, capture the patch."""
    iid = str(item.get("instance_id") or "").strip()
    try:
        repo, base_commit = _prepare_workspace(item, run_root)
        problem = str(item.get("problem_statement") or "").strip()
        if not iid or not problem:
            raise RuntimeError("row needs instance_id and problem_statement")
        task_id = server.submit(problem, workspace_root=str(repo), memory_mode=memory_mode, timeout_sec=timeout)
        if not task_id:
            raise RuntimeError("server did not return a task_id")
        result = server.wait_task(task_id, timeout=timeout + 600)
        status = str(result.get("status") or "")
        if status == "timeout":
            # wait_task hit its OWN deadline; the server task may still be RUNNING. Cancel
            # it and wait for a real terminal status BEFORE capturing/continuing, so the
            # next instance's budget reset + workspace capture cannot race a live worker.
            server.cancel_task(task_id)
            result = server.wait_task(task_id, timeout=300)
            status = str(result.get("status") or "")
            if status not in ("completed", "failed", "cancelled", "rejected_duplicate"):
                raise _DriverAbort(f"task {task_id} did not terminate after cancel (still {status or 'timeout'})")
        patch_out = patch_dir / f"{safe_benchmark_id(iid)}.diff"
        patch = _capture_patch(repo, base_commit, patch_out)
        prediction = {"instance_id": iid, "model_name_or_path": "ouroboros-pro-evolve", "model_patch": patch}
        # Record the TRUE task status: a partial patch captured from a failed/timed-out
        # task is NOT 'completed' (DEVELOPMENT.md benchmark-ledger contract). The official
        # scorer still judges the patch, but the ledger stays auditable about its origin.
        completed = status == "completed"
        row = task_result_row(
            benchmark="swe_bench_pro", instance_id=iid,
            status="completed" if completed else (status or "unknown"),
            reason_code="patch_generated" if completed else "partial_patch",
            prediction_written=True, official_eval_status="pending",
            output_paths={"patch": str(patch_out)},
            details={"task_status": status, "task_id": task_id,
                     "patch_bytes": len(patch.encode("utf-8", "replace"))},
        )
        return row, prediction, None
    except _DriverAbort:
        raise  # fatal — must NOT be downgraded to a recoverable per-instance error
    except Exception as exc:  # noqa: BLE001 — driver records the failure, keeps going
        reason = "empty_patch" if "empty patch" in str(exc) else "failed"
        row = task_result_row(benchmark="swe_bench_pro", instance_id=iid, status=reason,
                              reason_code=reason, error=str(exc))
        return row, {}, {"instance_id": iid, "error": str(exc), "reason_code": reason}


def main() -> int:
    ap = argparse.ArgumentParser(description="SWE-bench Pro evolutionary driver (B-full, isolated server).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--instances", help="JSONL of instance rows")
    src.add_argument("--demo", type=int, metavar="N", help="synthesize N self-contained demo instances")
    # External-workspace tasks forbid `shared`; memory carries across instances via the
    # PERSISTENT isolated data root (a forked task writes reflections back to canonical).
    ap.add_argument("--memory-mode", default="forked", choices=["forked", "empty"])
    ap.add_argument("--cadence", default="llm",
                    help="post-task evolution cadence in the isolated run: llm | every_n:<k> | off")
    ap.add_argument("--timeout", type=int, default=1800, help="per-instance solve timeout (sec)")
    ap.add_argument("--absorb-timeout", type=int, default=1800,
                    help="between-instance wait for an absorbed self-evolution cycle (sec)")
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
    # The reviewed self-mod commit must land on the branch safe_restart checks out
    # (BRANCH_DEV='ouroboros'); -B guarantees that branch exists at the cloned HEAD even
    # if the source is on a tag/detached HEAD/other branch, and we fail loudly if git errs.
    rc, out = _git(["checkout", "-B", "ouroboros"], clone)
    if rc != 0:
        _log(f"checkout -B ouroboros failed: {out}")
        return 2
    # Isolation: drop origin (== live REPO_DIR) so an isolated evolution self-mod
    # commit's _auto_push -> push_to_remote can NEVER push back to the live repo.
    _git(["remote", "remove", "origin"], clone)
    settings_path = _seed_settings(data_root, args.cadence)
    # Seed ONLY owner_chat_id (the evolution loop gates on it; /api/tasks never binds
    # it). Do NOT pre-enable evolution_mode_enabled — that would let the idle tick
    # self-modify the clone before the first instance; the post-task loop's
    # apply_pending_request enables the one-shot campaign after a qualifying task.
    seed_owner_state(data_root)
    os.environ["OUROBOROS_DATA_DIR"] = str(data_root)  # so the budget guard sees the isolated dir here too

    instances = _make_demo_instances(run_root, int(args.demo)) if args.demo else _rows(pathlib.Path(args.instances).expanduser())
    if not instances:
        _log("no instances")
        return 2

    from supervisor import state as sstate

    # Mark this throwaway data root as an isolated benchmark root so the guarded
    # reset_per_task_budget will operate on it — and refuse any live root, which lacks it.
    (data_root / sstate.ISOLATED_BENCHMARK_SENTINEL).write_text("isolated benchmark data root\n", encoding="utf-8")

    live_status_before = _git(["status", "--porcelain"], REPO_DIR)[1]
    predictions: list[dict] = []
    ledger_rows: list[dict] = []
    errors: list[dict] = []
    budget_resets: list[bool] = []
    absorb_events: list[dict] = []

    server = IsolatedServer(clone, data_root, settings_path)
    try:
        _log(f"starting isolated server on {server.base_url} (clone @ ouroboros) …")
        server.start(ready_timeout=240)
        for n, item in enumerate(instances, 1):
            _log(f"instance {n}/{len(instances)}: {item.get('instance_id')}")
            prev_sha = server.current_sha()
            prev_absorbed = absorbed_cycles_done(data_root)
            row, prediction, error = _solve_instance(server, item, run_root, patch_dir, args.memory_mode, args.timeout)
            ledger_rows.append(row)
            if prediction:
                predictions.append(prediction)
            if error:
                errors.append(error)
            _log(f"instance {n}: status={row.get('status')}")
            # Between instances: let the REAL supervisor loop run a self-evolution cycle
            # (commit_reviewed -> request_restart -> os.execvpe -> verify_restart absorb),
            # then reset the per-task budget so the next instance is not falsely emergency'd.
            if n < len(instances):
                absorb = server.wait_for_absorb(prev_sha, prev_absorbed, timeout=args.absorb_timeout)
                absorb_events.append(absorb)
                _log(f"between {n}->{n+1}: absorbed={absorb.get('absorbed')} cycles={absorb.get('cycles')}")
                did_reset = sstate.reset_per_task_budget(data_root, confirm_isolated=True)
                budget_resets.append(bool(did_reset))
                _log(f"between {n}->{n+1}: budget_reset={did_reset}")
    except _DriverAbort as exc:
        # A task would not terminate even after cancel: STOP the run rather than capture
        # or budget-reset against a live worker (do not continue to the next instance).
        _log(f"FATAL: {exc}; aborting run")
        return 2
    finally:
        server.stop()

    live_status_after = _git(["status", "--porcelain"], REPO_DIR)[1]
    acceptance = {
        "instances": len(instances),
        "predictions": len(predictions),
        "errors": len(errors),
        # Resets happen only BETWEEN instances; a single-instance run has none, so
        # absence of resets is a pass here (evolve_smoke defaults False since it always
        # has >=1 reset between its tasks).
        "budget_reset_worked": all(budget_resets) if budget_resets else True,
        "live_repo_untouched": live_status_before == live_status_after,
        "absorbed_cycles": absorbed_cycles_done(data_root),
        "absorb_events": absorb_events,
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
        extra={"mode": "evolutionary_server_driven", "memory_mode": args.memory_mode,
               "cadence": args.cadence, "server_url": server.base_url, "acceptance": acceptance},
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
