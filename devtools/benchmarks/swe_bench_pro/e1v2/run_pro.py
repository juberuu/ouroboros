#!/usr/bin/env python3
"""SWE-Pro E1v2 single-root driver with native post-task evolution.

Runs SWE-Pro tasks sequentially against one persistent agent carried by obo-repo/obo-data volumes. Each task is one root task plus one prompt:
  1. solves /app through the task prompt and Ouroboros tools;
  2. captures the official SWE patch directly from /app (Method C, not --patch-out).
The code-growth channel is native post-task evolution; the root task is not project-scoped
(no --workspace -> not project-scoped) -> supervisor tick applies promotion -> gated
evolution cycle (reviewed commit in /obo-repo + os.execvpe restart), then wait for absorb, dump state, and continue.
cadence=every_n:1 forces one evolution decision per cycle. Grading is offline.

Modes: default E1v2 (post-task evolution on); --baseline disables code evolution for a clean A/B comparison.

  OPENROUTER_API_KEY=... python pro/run_pro.py --limit 2 --out-dir runs/pro_smoke --reset-state
"""
from __future__ import annotations
import argparse, csv, json, os, subprocess, sys, pathlib

if __package__ in {None, ""}:
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[4]))

from devtools.benchmarks.common.run_roots import ensure_outside_repo

PRO = pathlib.Path(__file__).resolve().parent              # .../swe_bench_pro/e1v2/
ROOT = PRO.parent                                          # .../swe_bench_pro/
SRC = pathlib.Path(__file__).resolve().parents[4]          # Ouroboros repo root (mount ro)
CSV_DEFAULT = ROOT / "task_order_pro_70.csv"
IMG_REPO = "jefzda/sweap-images"


def norm(iid: str) -> str:
    return iid[len("instance_"):] if iid.startswith("instance_") else iid


def read_csv_order(path: pathlib.Path) -> list[str]:
    with path.open(encoding="utf-8") as f:
        rows = sorted(csv.DictReader(f), key=lambda r: int(r["idx"]))
    return [r["instance_id"] for r in rows]


def load_pro_rows(ids: list[str]) -> dict:
    from datasets import load_dataset
    ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
    by_key = {}
    for r in ds:
        by_key[r["instance_id"]] = r
        by_key[norm(r["instance_id"])] = r
    out = {}
    for cid in ids:
        row = by_key.get(cid) or by_key.get(norm(cid)) or by_key.get("instance_" + norm(cid))
        if row:
            out[cid] = row
    return out


def build_prompt(row: dict, self_improve: bool = True) -> str:
    """Build the solve prompt.

    E1v2 versus baseline is controlled by settings (post-task evolution on/off),
    not by a different task prompt.
    """
    tpl = (PRO / "prompt_e1v2.txt").read_text(encoding="utf-8")
    return (tpl
        .replace("{working_dir}", "/app")
        .replace("{repo}", str(row.get("repo") or ""))
        .replace("{repo_language}", str(row.get("repo_language") or ""))
        .replace("{problem_statement}", str(row.get("problem_statement") or "").strip())
        .replace("{requirements}", str(row.get("requirements") or "").strip())
        .replace("{interface}", str(row.get("interface") or "").strip()))


def derive_run_settings(base_path: str, out_dir: pathlib.Path, solve_model: str,
                        total_budget: float, per_task_cost: float,
                        post_task_evolution: bool = True, cadence: str = "every_n:1") -> pathlib.Path:
    """Build per-run settings for obo-data from the committed base plus benchmark overrides. Secrets are blanked; live keys enter only through explicit environment opt-in."""
    d = json.loads(pathlib.Path(base_path).expanduser().read_text(encoding="utf-8"))
    d["TOTAL_BUDGET"] = float(total_budget)
    d["OUROBOROS_PER_TASK_COST_USD"] = float(per_task_cost)
    for k in ("OUROBOROS_MODEL", "OUROBOROS_MODEL_CODE", "OUROBOROS_MODEL_LIGHT", "OUROBOROS_MODEL_FALLBACK"):
        d[k] = solve_model          # pin all slots (insurance against settings.json drift)
    d["OUROBOROS_REVIEW_MODELS"] = ",".join([solve_model] * 3)
    d["OUROBOROS_SCOPE_REVIEW_MODEL"] = solve_model
    for k in ("OUROBOROS_EFFORT_TASK", "OUROBOROS_EFFORT_EVOLUTION", "OUROBOROS_EFFORT_REVIEW",
              "OUROBOROS_EFFORT_SCOPE_REVIEW", "OUROBOROS_EFFORT_DEEP_SELF_REVIEW",
              "OUROBOROS_EFFORT_CONSCIOUSNESS"):
        d[k] = "high"
    d["OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS"] = "true"
    d["OUROBOROS_SERVER_HOST"] = "127.0.0.1"
    d["OUROBOROS_SERVER_PORT"] = 8765
    # cadence "off" disables evolution through the documented POST_TASK_EVOLUTION
    # contract (false), not by relying on a downstream cadence guard — so the CLI's
    # advertised `--cadence off` reliably turns post-task evolution off.
    evolution_enabled = bool(post_task_evolution) and str(cadence).strip().lower() != "off"
    d["OUROBOROS_POST_TASK_EVOLUTION"] = "true" if evolution_enabled else "false"
    d["OUROBOROS_POST_TASK_EVOLUTION_CADENCE"] = cadence
    d["OUROBOROS_POST_TASK_EVOLUTION_BUDGET_USD"] = 0.0
    _STEER_FALLBACK = (
        "At the evolve stage, implement the objective as at most ONE reviewed commit, then restart once. "
        "Fold reviewer fixes into that same change before committing. After the reviewed commit lands "
        "(clean working tree, HEAD = that commit), call request_restart once with a short reason and stop. "
        "An honest no-op is valid when the objective is already solved, unsafe, too broad, or needs owner input. "
        "Do not churn. Do ABSOLUTELY NO release bookkeeping in this benchmark environment: never edit VERSION, "
        "CHANGELOG, README, docs/ARCHITECTURE, pyproject.toml, or package.json, and do not apply any version-bump / "
        "P9 release-carrier rule; advisory review will flag their absence, which is expected and must be left as "
        "advisory. Never modify the review-enforcement machinery to make findings always block or pass regardless "
        "of the configured mode."
    )
    try:
        _steer = (PRO / "prompt_evolution_steer.txt").read_text(encoding="utf-8").strip()
    except Exception:
        _steer = ""
    d["OUROBOROS_EVOLUTION_PERSISTENT_OBJECTIVE"] = _steer or _STEER_FALLBACK
    for k in list(d):
        if any(t in k.upper() for t in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
            d[k] = ""
    p = out_dir / "_run_settings.json"
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return p


def read_spent_usd(img: str) -> float:
    try:
        r = subprocess.run(["docker", "run", "--rm", "-v", "obo-data:/d:ro",
                            "--entrypoint", "cat", img, "/d/state/state.json"],
                           capture_output=True, text=True, timeout=180)
        return float(json.loads(r.stdout or "{}").get("spent_usd", 0.0))
    except Exception:
        return 0.0


def kill_container(name: str) -> None:
    subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def volume_exists(name: str) -> bool:
    return subprocess.run(["docker", "volume", "inspect", name], capture_output=True).returncode == 0


def image_libc(img: str) -> str:
    """Choose the environment volume that matches the task image libc (glibc versus musl)."""
    try:
        r = subprocess.run(["docker", "run", "--rm", "--entrypoint", "sh", img, "-c",
                            "ls /lib/libc.musl* >/dev/null 2>&1 && echo musl || echo glibc"],
                           capture_output=True, text=True, timeout=120)
        return "musl" if "musl" in (r.stdout or "") else "glibc"
    except Exception:
        return "glibc"


def dump_state(out: pathlib.Path, img: str) -> None:
    for vol, name in (("obo-data", "obo-data.tgz"), ("obo-repo", "obo-repo.tgz")):
        try:
            subprocess.run(
                ["docker", "run", "--rm", "-v", f"{vol}:/src:ro", "-v", f"{out}:/dump",
                 "--entrypoint", "tar", img, "czf", f"/dump/{name}", "-C", "/src", "."],
                capture_output=True, timeout=1200)
            sz = (out / name).stat().st_size if (out / name).exists() else 0
            print(f"[pro]   dump {name}: {sz//1024} KiB", file=sys.stderr)
        except Exception as e:
            print(f"[pro]   dump {name} FAILED: {e}", file=sys.stderr)


IMG_CACHE = pathlib.Path("/Volumes/OBOCACHE/swebench-cache")


def docker_pull_if_missing(img: str):
    if subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode == 0:
        return
    cp = IMG_CACHE / f"sweap_{img.split(':', 1)[1].replace('/', '_')}.tar.zst"
    if cp.is_file() and cp.stat().st_size > 1_000_000:
        print(f"[pro] load from cache {cp.name} ({cp.stat().st_size/1e9:.2f}GB)", file=sys.stderr)
        zp = subprocess.Popen(["zstd", "-dc", str(cp)], stdout=subprocess.PIPE)
        subprocess.run(["docker", "load"], stdin=zp.stdout)
        zp.stdout.close(); zp.wait()
        if subprocess.run(["docker", "image", "inspect", img], capture_output=True).returncode == 0:
            return
        print("[pro] cache-load produced no image - fallback to pull", file=sys.stderr)
    print(f"[pro] pull {img}", file=sys.stderr)
    subprocess.run(["docker", "pull", img], timeout=3600)


def run_instance(cid: str, row: dict, args, api_key: str, seed_settings: pathlib.Path,
                 task_total: float) -> dict:
    out = (ensure_outside_repo(pathlib.Path(args.out_dir).expanduser(), SRC) / cid.replace("/", "_")).resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "problem_statement.txt").write_text(build_prompt(row, args.self_improve), encoding="utf-8")
    img = f"{IMG_REPO}:{row['dockerhub_tag']}"
    docker_pull_if_missing(img)
    libc = image_libc(img)
    env_vol = "oboros-env" if libc == "glibc" else "oboros-env-musl"
    if not volume_exists(env_vol):
        print(f"[pro] {cid}: SKIP - missing env volume '{env_vol}' for libc={libc} (musl image?)", file=sys.stderr)
        return {"instance_id": cid, "model_name_or_path": args.model_name, "model_patch": "",
                "timed_out": False, "infra_suspect": True, "health_rollback": False,
                "libc_skip": f"{libc}:{env_vol}", "refl_line": "", "solve_line": "", "quiet_line": ""}
    if str(os.environ.get("OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS", "")).lower() not in {"1", "true", "yes"}:
        print("[pro] refusing to inject OPENROUTER_API_KEY into an untrusted Pro task container; set OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS=1 for audited local smoke only", file=sys.stderr)
        return {"instance_id": cid, "model_name_or_path": args.model_name, "model_patch": "",
                "timed_out": False, "infra_suspect": True, "health_rollback": False,
                "secret_opt_in_required": True, "refl_line": "", "solve_line": "", "quiet_line": ""}
    cname = "obopro-" + norm(cid).replace("__", "-").replace("_", "-").replace(".", "-").lower()[:90]
    M = lambda h, c, ro=True: ["-v", f"{h}:{c}" + (":ro" if ro else "")]
    mem_flags = []
    if args.mem_limit:
        # --memory-swap == --memory disables swap so a runaway allocation is
        # capped at the RAM limit (clean OOM, exit 137) rather than swapping the
        # host to death. See README "Diagnosing SIGKILL / OOM".
        mem_flags = ["--memory", args.mem_limit, "--memory-swap", args.mem_limit]
    cmd = ["docker", "run", "--rm", "--name", cname,
        *mem_flags,
        # Name-only env form: docker forwards the value from our process environment
        # (set below) so the live key never appears in the host argv / `ps` output.
        "-e", "OPENROUTER_API_KEY",
        "-e", f"OUROBOROS_MODEL={args.solve_model}",
        "-e", f"OUROBOROS_MODEL_CODE={args.solve_model}",
        "-e", f"OUROBOROS_MODEL_LIGHT={args.solve_model}",
        "-e", f"OUROBOROS_MODEL_FALLBACK={args.solve_model}",
        "-e", "OUROBOROS_RUNTIME_MODE=pro",
        "-e", "OUROBOROS_PRE_PUSH_TESTS=0",
        "-e", f"TOTAL_BUDGET={task_total}",
        "-e", f"OUROBOROS_PER_TASK_COST_USD={args.per_task_cost}",
        "-e", f"OBO_BASE_COMMIT={row['base_commit']}",
        "-e", f"OBO_INSTANCE_ID={cid}",
        "-e", f"OBO_REPO={row.get('repo','')}",
        "-e", "OBO_WORKDIR=/app",
        "-e", f"OBO_SOLVE_TIMEOUT={args.solve_timeout}",
        "-e", f"OBO_ABSORB_MAX={args.absorb_max}",
        "-e", f"OBO_REFLECT_MIN={args.reflect_min}",
        "-e", f"OBO_REFLECT_MAX={args.reflect_max}",
        "-e", f"OBO_QUIET_STABLE={args.quiet_stable}",
        "-e", "OUROBOROS_ALLOW_MUTATIVE_SUBAGENTS=true",
        "-e", f"OBO_SELFIMPROVE={1 if args.self_improve else 0}",
        "-e", "OUROBOROS_MAX_SUBAGENT_DEPTH=2",
        "-e", "OUROBOROS_MAX_ACTIVE_SUBAGENTS_PER_ROOT=3",
        "-e", "OUROBOROS_SUBAGENT_WORKTREE_ROOT=/Ouroboros/subagent_worktrees",
        "-v", f"{env_vol}:/opt/miniconda3/envs/oboros:ro",
        "-v", "obo-repo:/obo-repo", "-v", "obo-data:/obo-data",
        *M(SRC, "/opt/ouroboros-ro"),
        *M(seed_settings, "/opt/oboros-settings-ro.json"),
        *M(PRO / "entrypoint_pro.sh", "/opt/entrypoint_pro.sh"),
        *M(out / "problem_statement.txt", "/opt/problem_statement.txt"),
        "-v", f"{out}:/out",
        "--entrypoint", "bash", img, "/opt/entrypoint_pro.sh"]
    kill_container(cname)
    timed_out = False
    host_to = args.solve_timeout + args.absorb_max + 1200
    # Pass the provider key through the child environment (not argv) for the
    # name-only `-e OPENROUTER_API_KEY` above.
    docker_env = dict(os.environ)
    docker_env["OPENROUTER_API_KEY"] = api_key
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=host_to, env=docker_env)
        oom_note = ""
        if r.returncode == 137:
            oom_note = (
                f"\n[driver] container exited 137 (SIGKILL) — likely OOM at --mem-limit={args.mem_limit}. "
                "A worker killed mid-task with 'signal 9 — terminal' is usually this. Check for an "
                "unbounded operation (e.g. search over a root that resolved to '/'); raise --mem-limit "
                "or narrow the task. Host dmesg shows the kernel OOM line.\n"
            )
        (out / "container.log").write_text(r.stdout + "\n" + r.stderr + oom_note, encoding="utf-8")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        dec = lambda b: b.decode("utf-8", "replace") if isinstance(b, bytes) else (b or "")
        (out / "container.log").write_text("[driver] CONTAINER TIMEOUT\n" + dec(e.stdout) + "\n" + dec(e.stderr), encoding="utf-8")
        kill_container(cname)
        print(f"[pro] {cid}: TIMEOUT (host) - continuing", file=sys.stderr)

    patch = (out / "patch.diff").read_text(encoding="utf-8") if (out / "patch.diff").exists() else ""
    clog = (out / "container.log").read_text(encoding="utf-8", errors="replace") if (out / "container.log").exists() else ""
    se = out / "solve_events.jsonl"
    api_net = api_ctx = 0
    _CTX = ("prompt is too long", "input is too long", "context length",
            "maximum context", "context_length", "too many tokens")
    if se.exists():
        for ln in se.read_text(errors="replace").splitlines():
            if '"llm_api_error"' not in ln:
                continue
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            if ev.get("type") != "llm_api_error":
                continue
            err = str(ev.get("data", {}).get("error", "")).lower()
            if any(t in err for t in _CTX):
                api_ctx += 1
            else:
                api_net += 1
    api_errors = api_net   # gate-relevant count = network transients, not context overflow
    def grep1(marker: str) -> str:
        for ln in clog.splitlines():
            if marker in ln:
                return ln.strip()
        return ""
    selfedit = {}
    sep = out / "selfedit.json"
    if sep.exists():
        try:
            selfedit = json.loads(sep.read_text(encoding="utf-8"))
        except Exception:
            selfedit = {}
    absorb = {}
    abp = out / "absorb.json"
    if abp.exists():
        try:
            absorb = json.loads(abp.read_text(encoding="utf-8"))
        except Exception:
            absorb = {}
    return {"instance_id": cid, "model_name_or_path": args.model_name, "model_patch": patch,
            "timed_out": timed_out, "api_errors": api_errors, "api_ctx": api_ctx,
            "infra_suspect": "SOLVE_INFRA_SUSPECT" in clog,
            "health_rollback": "HEALTH_GATE_ROLLBACK" in clog,
            "selfedit": selfedit,
            "evolution_degraded": bool(absorb.get("degraded")),
            "absorb_reason": str(absorb.get("reason", "")),
            "refl_line": grep1("knowledge files:"),
            "solve_line": grep1("ROOT-RUN patch="),
            "quiet_line": grep1("[pro] evolution:")}


def normalize_result(row: dict, cid: str, args) -> dict:
    defaults = {
        "instance_id": cid,
        "model_name_or_path": args.model_name,
        "model_patch": "",
        "timed_out": False,
        "infra_suspect": False,
        "health_rollback": False,
        "api_errors": 0,
        "api_ctx": 0,
        "refl_line": "",
        "solve_line": "",
        "quiet_line": "",
        "selfedit": {},
        "evolution_degraded": False,
        "absorb_reason": "",
    }
    return {**defaults, **(row or {})}


def build_timeline_row(order: int, cid: str, res: dict, spent_after: float, flags: list) -> dict:
    """Build one timeline.jsonl row.

    Persists the infra non-execution markers (`infra_suspect`,
    `secret_opt_in_required`, `libc_skip`) so auto_run.run_one can hard-stop on a
    secret-injection refusal and avoid counting a skipped/non-executed task as a
    LEGIT last-good. Dropping them here would silently re-break that handoff.
    """
    se = res.get("selfedit") or {}
    return {"order": order, "instance_id": cid, "patch_bytes": len(res["model_patch"]),
            "spent_after_usd": round(spent_after, 4), "flags": flags,
            "infra_suspect": bool(res.get("infra_suspect")),
            "secret_opt_in_required": bool(res.get("secret_opt_in_required")),
            "libc_skip": res.get("libc_skip", ""),
            "api_errors": res["api_errors"], "api_ctx": res["api_ctx"],
            "refl": res["refl_line"], "quiet": res["quiet_line"],
            "commits_added": se.get("commits_added", 0),
            "loc_added": se.get("loc_added", 0), "loc_removed": se.get("loc_removed", 0),
            "tools_added": se.get("tools_added", []), "verdicts": se.get("verdicts", {}),
            "self_rollback": se.get("health_rollback", False),
            "evolution_degraded": res.get("evolution_degraded", False),
            "absorb_reason": res.get("absorb_reason", "")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(CSV_DEFAULT))
    ap.add_argument("--start", type=int, default=1, help="first task index (1-based, from CSV)")
    ap.add_argument("--limit", type=int, default=2, help="number of tasks to run")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--settings", default=str(PRO / "settings_base.json"),
                    help="benchmark base settings.json (committed template, not a personal agent folder)")
    ap.add_argument("--solve-model", default="anthropic/claude-sonnet-4.5")
    ap.add_argument("--total-budget", type=float, default=500.0)
    ap.add_argument("--per-task-cost", type=float, default=25.0)
    ap.add_argument("--mem-limit", default="8g",
                    help="docker --memory cap per instance container (e.g. 8g). Bounds a runaway "
                         "search/process so it OOMs the container cleanly (exit 137) instead of the "
                         "host OOM-killer ambiguously SIGKILLing the worker. Empty string disables.")
    ap.add_argument("--model-name", default="ouroboros-e1-pro-sonnet-4.5")
    ap.add_argument("--solve-timeout", type=int, default=4500,
                    help="root task timeout for solving /app.")
    ap.add_argument("--cadence", default="every_n:1",
                    help="native post-task evolution cadence: every_n:<k> | llm | off (default every_n:1).")
    ap.add_argument("--absorb-max", type=int, default=1800,
                    help="max wait for absorbed evolution cycle after a task (seconds). Cycle = "
                         "separate evolution task (review triad) plus os.execvpe restart.")
    ap.add_argument("--reflect-min", type=int, default=30, help="deprecated: wait-until-quiet was replaced by wait-for-absorb")
    ap.add_argument("--reflect-max", type=int, default=900, help="deprecated: see --absorb-max")
    ap.add_argument("--quiet-stable", type=int, default=25, help="deprecated")
    ap.add_argument("--baseline", action="store_true",
                    help="baseline E1': disable the code-evolution channel (POST_TASK_EVOLUTION=false). "
                         "By default E1v2 enables native post-task evolution.")
    ap.add_argument("--self-improve", action="store_true",
                    help="deprecated: E1v2 is now the default; kept as a no-op for auto_run compatibility")
    ap.add_argument("--selfimprove-timeout", type=int, default=900,
                    help="deprecated in single-root mode; kept for compatibility")
    ap.add_argument("--reset-state", action="store_true", help="recreate obo-repo/obo-data volumes (clean X0)")
    ap.add_argument("--pause-on-api-err", type=int, default=0,
                    help="pause after a task whose api_errors count exceeds N (manual check: transient interruption vs legitimate recovery). -1 disables pausing")
    args = ap.parse_args()
    args.self_improve = not args.baseline

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("error: OPENROUTER_API_KEY is not set", file=sys.stderr); return 2

    out_dir = ensure_outside_repo(pathlib.Path(args.out_dir).expanduser(), SRC)
    order = read_csv_order(pathlib.Path(args.csv).expanduser())
    ids = order[args.start - 1: args.start - 1 + args.limit]
    print(f"[pro] sequence ({len(ids)}): " + " -> ".join(norm(i)[:40] for i in ids), file=sys.stderr)
    rows = load_pro_rows(ids)
    missing = [i for i in ids if i not in rows]
    if missing:
        print(f"[pro] !! missing from dataset (skip): {missing}", file=sys.stderr)

    if args.reset_state:
        for v in ("obo-repo", "obo-data"):
            subprocess.run(["docker", "volume", "rm", "-f", v], capture_output=True)
    for v in ("obo-repo", "obo-data"):
        subprocess.run(["docker", "volume", "create", v], capture_output=True)

    def atomic_write(p: pathlib.Path, text: str) -> None:
        tmp = p.with_suffix(p.suffix + ".tmp"); tmp.write_text(text, encoding="utf-8"); os.replace(tmp, p)

    preds, timeline = [], []
    for i, cid in enumerate([c for c in ids if c in rows], 1):
        row = rows[cid]
        img = f"{IMG_REPO}:{row['dockerhub_tag']}"
        docker_pull_if_missing(img)
        spent = read_spent_usd(img) if i > 1 else 0.0
        if spent >= args.total_budget:
            print(f"[pro] STOP: budget ${args.total_budget} exhausted (spent ${spent:.2f})", file=sys.stderr); break
        task_total = min(args.total_budget, spent + args.per_task_cost)
        seed = derive_run_settings(args.settings, out_dir, args.solve_model, task_total, args.per_task_cost,
                                   post_task_evolution=args.self_improve, cadence=args.cadence)
        print(f"\n[pro] === task {i}/{len(ids)}: {norm(cid)[:50]} === spent=${spent:.2f} cap=${task_total:.2f} lang={row.get('repo_language')}", file=sys.stderr)
        res = normalize_result(run_instance(cid, row, args, api_key, seed, task_total), cid, args)
        dump_state(out_dir / cid.replace("/", "_"), img)
        spent_after = read_spent_usd(img)
        if res["model_patch"].strip():
            preds.append({k: res[k] for k in ("instance_id", "model_name_or_path", "model_patch")})
        flags = [f for f, on in (("TIMEOUT", res["timed_out"]), ("INFRA", res["infra_suspect"]),
                                 ("ROLLBACK", res["health_rollback"])) if on]
        timeline.append(build_timeline_row(i, cid, res, spent_after, flags))
        se = res.get("selfedit") or {}
        print(f"[pro] {norm(cid)[:50]}: patch={len(res['model_patch'])}B spent=${spent_after:.2f} api_err={res['api_errors']} ctx_err={res['api_ctx']} {' '.join(flags) or 'ok'}", file=sys.stderr)
        if args.self_improve:
            print(f"[pro]    self-edit: commits={se.get('commits_added',0)} loc=+{se.get('loc_added',0)}/-{se.get('loc_removed',0)} "
                  f"tools={len(se.get('tools_added',[]))} verdicts={se.get('verdicts',{})} rollback={se.get('health_rollback',False)}", file=sys.stderr)
        for key in ("solve_line", "refl_line", "quiet_line"):
            if res[key]:
                print(f"[pro]    {res[key]}", file=sys.stderr)
        d = out_dir / cid.replace("/", "_")
        print(f"[pro]    dump: data={'OK' if (d/'obo-data.tgz').exists() else 'NO'} repo={'OK' if (d/'obo-repo.tgz').exists() else 'NO'}", file=sys.stderr)
        atomic_write(out_dir / "timeline.jsonl", "\n".join(json.dumps(t, ensure_ascii=False) for t in timeline) + "\n")
        atomic_write(out_dir / "predictions.jsonl", "\n".join(json.dumps(p, ensure_ascii=False) for p in preds) + ("\n" if preds else ""))
        if args.pause_on_api_err >= 0 and res["api_errors"] > args.pause_on_api_err:
            print(f"\n[pro] ⏸ PAUSED_API_ERR: task {i} ({norm(cid)[:46]}) api_errors={res['api_errors']} > {args.pause_on_api_err}, "
                  f"patch={len(res['model_patch'])}B", file=sys.stderr)
            print("[pro]    MANUAL CHECK: legitimate recovery (real patch, events appended) or transient interruption (0B/few edits).", file=sys.stderr)
            print(f"[pro]    post-task dump saved in {cid.replace('/','_')}/. Rerun this task by restoring volumes to the previous dump and using --start {args.start + i - 1}.", file=sys.stderr)
            break

    print(f"\n[pro] done. tasks={len(timeline)} predictions={len(preds)} -> {out_dir/'predictions.jsonl'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
