#!/usr/bin/env python3
"""Autonomous SWE-Pro range runner with retry-on-network-transient behavior.

Runs run_pro.py one task at a time. After each task:
  - LEGIT (patch exists OR api_err==0): snapshot last-good volumes and continue.
  - TRANSIENT (patch==0B and network api_err>0): restore last-good volumes, sleep --retry-wait, retry the same task.
Transient means the LLM/provider channel failed to sustain the agent run; see the network-transient retry policy.
last-good at start is the current volume state (= post-(start-1)).

  OPENROUTER_API_KEY=<fallback .env> python3 pro/auto_run.py --start 27 --end 50 --out-dir runs/pro_e1_27_50
"""
from __future__ import annotations
import argparse, json, os, pathlib, subprocess, sys, time

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

from devtools.benchmarks.common.run_roots import ensure_outside_repo
from ouroboros.platform_layer import kill_process_tree, subprocess_new_group_kwargs

HARN = pathlib.Path(__file__).resolve().parent
REPO_ROOT = pathlib.Path(__file__).resolve().parents[4]
RUN_PRO = HARN / "run_pro.py"


def log(msg: str) -> None:
    t = time.strftime("%m-%d %H:%M:%S")
    print(f"[auto {t}] {msg}", file=sys.stderr, flush=True)


def snapshot(dst: pathlib.Path) -> None:
    """Dump live obo-data/obo-repo volumes into dst/*.tgz (last-good rollback point)."""
    dst.mkdir(parents=True, exist_ok=True)
    for vol, name in (("obo-data", "obo-data.tgz"), ("obo-repo", "obo-repo.tgz")):
        tmp = dst / (name + ".partial")
        r = subprocess.run(["docker", "run", "--rm", "-v", f"{vol}:/src:ro", "-v", f"{dst}:/dump",
                            "--entrypoint", "tar", "alpine:3", "czf", f"/dump/{name}.partial", "-C", "/src", "."],
                           capture_output=True, timeout=1800)
        if r.returncode == 0 and tmp.exists():
            os.replace(tmp, dst / name)
        else:
            tmp.unlink(missing_ok=True)
            log(f"!! snapshot {name} FAILED rc={r.returncode}")


def restore(src: pathlib.Path) -> None:
    """Restore obo-data/obo-repo volumes from src/*.tgz."""
    for vol, name in (("obo-data", "obo-data.tgz"), ("obo-repo", "obo-repo.tgz")):
        subprocess.run(["docker", "volume", "rm", "-f", vol], capture_output=True)
        subprocess.run(["docker", "volume", "create", vol], capture_output=True)
        subprocess.run(["docker", "run", "--rm", "-v", f"{vol}:/d", "-v", f"{src}:/src:ro",
                        "alpine:3", "tar", "xzf", f"/src/{name}", "-C", "/d"], capture_output=True, timeout=1800)


def reflections() -> int:
    r = subprocess.run(["docker", "run", "--rm", "-v", "obo-data:/d:ro", "alpine:3",
                        "sh", "-c", "wc -l </d/logs/task_reflections.jsonl 2>/dev/null || echo 0"],
                       capture_output=True, text=True)
    try:
        return int((r.stdout or "0").strip().split()[0])
    except Exception:
        return -1


def _rm_obopro_containers() -> None:
    """Remove leftover benchmark containers (named ``obopro-*`` solve and
    ``obopro-dump-*`` teardown containers) after a task-wall-timeout. Avoids the
    GNU-only ``xargs -r`` (BSD/macOS xargs lacks it) by listing ids in Python."""
    try:
        ids = subprocess.run(["docker", "ps", "-aq", "--filter", "name=obopro-"],
                             capture_output=True, text=True, timeout=60).stdout.split()
    except Exception:
        ids = []
    if ids:
        try:
            subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, timeout=300)
        except Exception:
            pass


def run_one(i: int, out_dir: pathlib.Path, args) -> tuple[int | None, int | None, str, bool, bool]:
    """Run run_pro once. Returns (patch_bytes, api_err, instance_id, evolution_degraded, permanent_skip)."""
    cmd = [sys.executable, str(RUN_PRO), "--start", str(i), "--limit", "1",
           "--out-dir", str(out_dir), "--total-budget", str(args.total_budget),
           "--per-task-cost", str(args.per_task_cost), "--pause-on-api-err", "-1"]
    env = dict(os.environ)
    for p in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(p, None)
    tl = out_dir / "timeline.jsonl"
    tl.unlink(missing_ok=True)        # Freshness: run_pro rewrites timeline; if it did not write (failure/disk-full),
                                      # there is nothing to read -> None -> retry, not a stale previous-task record
    # Launch run_pro in its OWN process group/session (cross-platform via
    # platform_layer) so a wall-clock overrun can kill the whole subprocess tree
    # (run_pro + its docker client + zstd children), not just the direct child —
    # otherwise a hung teardown keeps contending with the next task.
    proc = subprocess.Popen(cmd, env=env, **subprocess_new_group_kwargs())
    try:
        proc.wait(timeout=args.task_wall_timeout)
    except subprocess.TimeoutExpired:
        # Almost always a post-solve colima teardown stall (volume dump / next image
        # pull), NOT the solve itself. Kill the whole process tree (cross-platform),
        # reap the direct child so it does not linger as a zombie, then remove any
        # leftover obopro-*/obopro-dump-* container. The patch (if any) is already on
        # disk and run_pro writes the timeline row BEFORE teardown, so the read below
        # still sees a LEGIT task instead of a phantom failure that gets re-solved.
        log(f"idx{i} TASK-WALL-TIMEOUT after {args.task_wall_timeout}s — killing run_pro process tree + obopro containers; continuing")
        kill_process_tree(proc)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        _rm_obopro_containers()
    try:
        rows = [json.loads(l) for l in tl.read_text().splitlines() if l.strip()]
        last = rows[-1]
        if last.get("secret_opt_in_required"):
            # Hard configuration error, NOT a transient: run_pro refused to inject the
            # provider key (OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS unset), so the task
            # never executed. Stop the whole autonomous run rather than retrying a
            # config error or counting an unexecuted task as LEGIT.
            log("FATAL: OPENROUTER_API_KEY was not injected into the task container "
                "(set OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS=1 for audited local smoke). Stopping.")
            raise SystemExit(2)
        if last.get("infra_suspect"):
            reason = str(last.get("infra_reason") or "")
            if reason in {"pyexpat_abi_mismatch", "server_import_failed", "pip_bootstrap_failed", "libc_skip"}:
                log(f"idx{i} permanent infra_suspect reason={reason}; recording non-run and continuing without retry")
                return (0, 0, last.get("instance_id", "?"), bool(last.get("evolution_degraded", False)), True)
            # Task did not actually execute (e.g. musl-image env-volume skip). Never
            # snapshot a non-run task as a LEGIT last-good: surface as patch_bytes=None
            # so the caller treats it as a failure (retry/stop), like a missing timeline.
            return (None, None, last.get("instance_id", "?"), bool(last.get("evolution_degraded", False)), False)
        return (int(last.get("patch_bytes", 0)), int(last.get("api_errors", 0)),
                last.get("instance_id", "?"), bool(last.get("evolution_degraded", False)), False)
    except Exception as e:
        log(f"!! timeline was not written after idx{i} (run_pro failure): {e}")
        return None, None, "?", False, False


def free_after_task(keep_images: int) -> None:
    """Docker image cache budget: keep only the newest `keep_images` sweap images in Docker.raw.
    does not grow without bound, while recent images stay available for fast same-task retries.
    This does not prune obo-*.tgz dumps; those are cheap host-side rollback
    points at every task boundary.
    """
    try:
        ids = subprocess.run(["docker", "images", "jefzda/sweap-images", "-q"],
                             capture_output=True, text=True, timeout=120).stdout.split()
    except Exception:
        return
    seen = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    for old in seen[keep_images:]:
        try:
            subprocess.run(["docker", "rmi", "-f", old], capture_output=True, timeout=300)
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=int, required=True)
    ap.add_argument("--end", type=int, required=True, help="inclusive")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--retry-wait", type=int, default=300, help="sleep before retrying a transient (s)")
    ap.add_argument("--max-retries", type=int, default=24, help="max retries for one task before stopping")
    ap.add_argument("--total-budget", type=float, default=500.0)
    ap.add_argument("--per-task-cost", type=float, default=50.0)
    ap.add_argument("--keep-images", type=int, default=10, help="keep only N newest sweap images in Docker.raw (keep all state dumps)")
    ap.add_argument("--task-wall-timeout", type=int, default=9000,
                    help="kill run_pro + its obopro container and continue if one task exceeds this wall-clock "
                         "(s). The captured patch.diff is already on disk and run_pro writes the timeline row "
                         "before teardown, so a teardown stall under a loaded docker daemon does not hang the run.")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY", "").strip():
        log("error: OPENROUTER_API_KEY is not set"); return 2

    out_dir = ensure_outside_repo(pathlib.Path(args.out_dir).expanduser(), REPO_ROOT)
    lastgood = out_dir / "_lastgood"

    log(f"START autonomous run idx{args.start}..{args.end}; current volume reflections={reflections()}")
    log("capturing baseline last-good (= state before first task)...")
    snapshot(lastgood)

    results = []
    for i in range(args.start, args.end + 1):
        tries = 0
        while True:
            pb, ae, iid, degraded, permanent_skip = run_one(i, out_dir, args)
            ok = (pb is not None) and (pb > 0 or ae == 0)
            if ok:
                if permanent_skip:
                    restore(lastgood)
                    log(f"idx{i} SKIP_PERMANENT_INFRA: patch={pb}B api_err={ae} degraded={degraded} :: {str(iid)[:46]}")
                else:
                    snapshot(lastgood)  # new last-good = post-idx_i
                    log(f"idx{i} LEGIT: patch={pb}B api_err={ae} refl={reflections()} degraded={degraded} img≤{args.keep_images} :: {str(iid)[:46]}")
                free_after_task(args.keep_images)  # Keep a bounded Docker image window; state dumps are preserved.
                results.append({"idx": i, "instance_id": iid, "patch_bytes": pb, "api_err": ae,
                                "retries": tries, "evolution_degraded": degraded,
                                "permanent_skip": permanent_skip})
                if degraded:
                    log(f"idx{i}: evolution degraded (benign telemetry); run continues")
                break
            tries += 1
            kind = "run_pro-failure" if pb is None else f"TRANSIENT(0B,api_err={ae})"
            log(f"idx{i} {kind} - retry {tries}/{args.max_retries} after {args.retry_wait}s; restore last-good")
            restore(lastgood)
            if tries > args.max_retries:
                log(f"idx{i}: max retries exhausted - stopping autonomous run; network did not recover.")
                _write_summary(out_dir, results, stopped_at=i)
                return 1
            time.sleep(args.retry_wait)

    _write_summary(out_dir, results, stopped_at=None)
    log(f"DONE idx{args.start}..{args.end}: {len(results)} tasks, volume reflections={reflections()}")
    return 0


def _write_summary(out_dir: pathlib.Path, results: list, stopped_at) -> None:
    s = {"completed": results, "stopped_at": stopped_at,
         "n_done": len(results), "n_with_patch": sum(1 for r in results if r["patch_bytes"] > 0),
         "total_retries": sum(r["retries"] for r in results)}
    (out_dir / "auto_summary.json").write_text(json.dumps(s, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
