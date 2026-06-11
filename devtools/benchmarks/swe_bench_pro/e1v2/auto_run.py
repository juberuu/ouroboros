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


def run_one(i: int, out_dir: pathlib.Path, args) -> tuple[int | None, int | None, str, bool]:
    """Run run_pro once for task index i. Returns (patch_bytes, api_err, instance_id, evolution_degraded)."""
    cmd = [sys.executable, str(RUN_PRO), "--start", str(i), "--limit", "1",
           "--out-dir", str(out_dir), "--total-budget", str(args.total_budget),
           "--per-task-cost", str(args.per_task_cost), "--pause-on-api-err", "-1"]
    env = dict(os.environ)
    for p in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(p, None)
    tl = out_dir / "timeline.jsonl"
    tl.unlink(missing_ok=True)        # Freshness: run_pro rewrites timeline; if it did not write (failure/disk-full),
    subprocess.run(cmd, env=env)      # there is nothing to read -> None -> retry, not a stale previous-task record
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
            # Task did not actually execute (e.g. musl-image env-volume skip). Never
            # snapshot a non-run task as a LEGIT last-good: surface as patch_bytes=None
            # so the caller treats it as a failure (retry/stop), like a missing timeline.
            return (None, None, last.get("instance_id", "?"), bool(last.get("evolution_degraded", False)))
        return (int(last.get("patch_bytes", 0)), int(last.get("api_errors", 0)),
                last.get("instance_id", "?"), bool(last.get("evolution_degraded", False)))
    except Exception as e:
        log(f"!! timeline was not written after idx{i} (run_pro failure): {e}")
        return None, None, "?", False


def free_after_task(keep_images: int) -> None:
    """Docker image cache budget: keep only the newest `keep_images` sweap images in Docker.raw.
    does not grow without bound, while recent images stay available for fast same-task retries.
    This does not prune obo-*.tgz dumps; those are cheap host-side rollback
    points at every task boundary.
    """
    ids = subprocess.run(["docker", "images", "jefzda/sweap-images", "-q"],
                         capture_output=True, text=True).stdout.split()
    seen = []
    for i in ids:
        if i not in seen:
            seen.append(i)
    for old in seen[keep_images:]:
        subprocess.run(["docker", "rmi", "-f", old], capture_output=True)


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
            pb, ae, iid, degraded = run_one(i, out_dir, args)
            ok = (pb is not None) and (pb > 0 or ae == 0)
            if ok:
                snapshot(lastgood)  # new last-good = post-idx_i
                free_after_task(args.keep_images)  # Keep a bounded Docker image window; state dumps are preserved.
                log(f"idx{i} LEGIT: patch={pb}B api_err={ae} refl={reflections()} degraded={degraded} img≤{args.keep_images} :: {str(iid)[:46]}")
                results.append({"idx": i, "instance_id": iid, "patch_bytes": pb, "api_err": ae,
                                "retries": tries, "evolution_degraded": degraded})
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
