#!/usr/bin/env bash
set -uo pipefail
OBO_PY=/opt/miniconda3/envs/oboros/bin/python
export HOME=/
export OUROBOROS_APP_ROOT=/obo-data/app
export OUROBOROS_DATA_DIR=/obo-data
export OUROBOROS_REPO_DIR=/obo-repo
export OUROBOROS_SETTINGS_PATH=/obo-data/settings.json
export OUROBOROS_RETURN_REASONING=true
export PYTHONPATH=/obo-repo
export PYTHONDONTWRITEBYTECODE=1
export OUROBOROS_DATA_DIR=/obo-data
export NO_PROXY=127.0.0.1,localhost,::1 ; export no_proxy="$NO_PROXY"
mkdir -p /obo-data /out
IID="${OBO_INSTANCE_ID:-task}"
WORK="${OBO_WORKDIR:-/app}"

[ -e /obo-repo/.git ] || { echo "[pro] seed /obo-repo" >&2; cp -a /opt/ouroboros-ro/. /obo-repo/; }
git -C /obo-repo config user.name  "Ouroboros"          2>/dev/null || true
git -C /obo-repo config user.email "ouroboros@local.mac" 2>/dev/null || true
cp /opt/oboros-settings-ro.json /obo-data/settings.json

touch /obo-data/.ouroboros_isolated_benchmark
# Seed owner_chat_id BEFORE the budget reset. reset_per_task_budget() does a
# load-modify-write that creates state.json with ONLY the zeroed budget keys on
# a fresh volume; if the seed ran after it, the "[ ! -f ]" guard would be false
# and owner_chat_id would never be set -> post_task_evolution drops every cycle
# and E1v2 silently degrades to E0. Seeding first (then letting the reset
# preserve all non-budget keys) keeps native evolution active on fresh runs.
if [ ! -f /obo-data/state/state.json ]; then
  mkdir -p /obo-data/state
  printf '{"owner_chat_id": 1}' > /obo-data/state/state.json
  echo "[pro] seeded owner_chat_id (fresh state.json)" >&2
fi
"$OBO_PY" - <<'PYEOF' 2>/dev/null || true
from supervisor.state import reset_per_task_budget
reset_per_task_budget("/obo-data", confirm_isolated=True)
PYEOF
echo "[pro] budget ledger reset requested through guarded isolated helper" >&2

# --- Option A: at task start, close a dangling committed evolution transaction
# left by the previous cycle. The native in-cycle restart (request_restart ->
# execvpe -> restart-verify) is unreliable: the agent inconsistently calls
# request_restart / makes extra commits, leaving the transaction active with
# commit_sha + restart_verified=False -> a poison-pill that wedges
# enqueue_evolution_task_if_needed for ALL subsequent tasks (E1v2 degrades to
# E1). But between tasks the container FULLY restarts on /obo-repo with the
# already-committed evolution code, and the previous task's health-gate verified
# its import -> the commit IS absorbed, just verified by the container boundary
# rather than an in-cycle execvpe. Mirror the verified path of
# record_evolution_cycle: restart_verified=True + absorbed_cycles_done++ + move
# to transaction_history + pop active_transaction -> gate cleared, counters
# intact. GUARD: if commit_sha is NOT reachable from /obo-repo HEAD (health-gate
# rolled the self-edit back -> commit lost), do NOT mark absorbed; ABANDON the
# transaction instead (still clears the poison-pill, without incrementing the
# counter). With a core that performs its own boot reconciliation this is a
# harmless no-op (no active_transaction left to heal).
"$OBO_PY" - >&2 2>/dev/null <<'PYEOF' || true
import json, subprocess, time
CAMP = "/obo-data/state/evolution_campaign.json"
try:
    c = json.load(open(CAMP))
except Exception:
    c = None
if isinstance(c, dict):
    at = c.get("active_transaction")
    if isinstance(at, dict):
        sha = str(at.get("commit_sha") or "").strip()
        if sha and not at.get("restart_verified"):
            rc = subprocess.run(["git", "-C", "/obo-repo", "merge-base", "--is-ancestor", sha, "HEAD"],
                                capture_output=True).returncode
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            th = c.get("transaction_history")
            if not isinstance(th, list):
                th = []
            if rc == 0:   # commit reachable from HEAD -> really present -> absorbed
                at["restart_verified"] = True
                at["restart_required"] = False
                at["restart_verified_at"] = now
                at["restart_verified_by"] = "harness_option_a_container_boundary"
                th.append(at); c["transaction_history"] = th[-50:]
                c["absorbed_cycles_done"] = int(c.get("absorbed_cycles_done") or 0) + 1
                c.pop("active_transaction", None)
                print(f"[pro] Option A: healed dangling evolution tx {sha[:8]} as restart-verified (container boundary)")
            else:         # commit lost (rollback) -> abandon, but clear the poison-pill
                at["abandoned"] = True
                at["abandoned_at"] = now
                at["abandoned_reason"] = "commit_not_reachable_after_health_gate_or_rollback"
                th.append(at); c["transaction_history"] = th[-50:]
                c.pop("active_transaction", None)
                print(f"[pro] Option A: abandoned dangling evolution tx {sha[:8]} (commit not reachable from HEAD — rolled back)")
            json.dump(c, open(CAMP, "w"))
PYEOF

git -C "$WORK" -c advice.detachedHead=false checkout -q "$OBO_BASE_COMMIT" 2>/dev/null || true
git -C "$WORK" reset -q --hard "$OBO_BASE_COMMIT" || { echo "[pro] FATAL: reset $WORK failed" >&2; exit 1; }

REPO_HEAD0="$(git -C /obo-repo rev-parse HEAD 2>/dev/null)"

export OUROBOROS_SERVER_HOST=127.0.0.1
export OUROBOROS_SERVER_PORT=8765
"$OBO_PY" /obo-repo/server.py >>/out/server.log 2>&1 &
SRV=$!
ready_probe() {
  "$OBO_PY" - <<'PYEOF' 2>/dev/null
import urllib.request, json, sys
try:
    urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=5).read()
    s = urllib.request.urlopen("http://127.0.0.1:8765/api/state", timeout=5).read().decode()
    sys.exit(0 if json.loads(s).get("supervisor_ready") else 1)
except Exception:
    sys.exit(1)
PYEOF
}
READY_MAX="${OBO_READY_MAX:-900}"; R=0; T0=$(date +%s)
while [ $(( $(date +%s) - T0 )) -lt "$READY_MAX" ]; do
  if ready_probe; then R=1; break; fi
  kill -0 "$SRV" 2>/dev/null || { echo "[pro] server died" >&2; tail -30 /out/server.log >&2; exit 1; }
  sleep 3
done
[ "$R" = 1 ] || { echo "[pro] not ready after ${READY_MAX}s" >&2; tail -30 /out/server.log >&2; kill "$SRV" 2>/dev/null; exit 1; }
echo "[pro] server ready in $(( $(date +%s) - T0 ))s" >&2

"$OBO_PY" -m ouroboros.cli --url http://127.0.0.1:8765 evolve stop >/dev/null 2>&1 || true

cp /opt/oboros-settings-ro.json /obo-data/settings.json   # Close the short window where the model could be overwritten in settings.
echo "[pro] ROOT-RUN $IID (self_modification; root digs /app via user_files (HOME=/); post-task evolution=native)" >&2
"$OBO_PY" -m ouroboros.cli --url http://127.0.0.1:8765 run \
  --jsonl --result-json-out /out/solve_result.json --timeout "${OBO_SOLVE_TIMEOUT:-3000}" \
  "$(cat /opt/problem_statement.txt)" >/out/solve_events.jsonl 2>/out/solve.stderr || true
JUNK_RE='appendonlydir|\.rdb$|\.aof$|\.manifest$|\.log$|\.tmp$|\.pid$|\.sock$|(^|/)node_modules/|__pycache__|\.pyc$|\.pyo$|\.pytest_cache|\.ruff_cache|\.mypy_cache|/\.cache/|/dist/|/build/|\.DS_Store|(^|/)\.coverage$|coverage\.xml$|/htmlcov/'
git -C "$WORK" add -A 2>/dev/null || true
git -C "$WORK" status --porcelain >/out/app_status.txt 2>/dev/null || true     # ARCHIVE: what the agent left in /app
git -C "$WORK" diff --cached --name-only "$OBO_BASE_COMMIT" 2>/dev/null | grep -E "$JUNK_RE" | while IFS= read -r f; do
  git -C "$WORK" reset -q -- "$f" 2>/dev/null
done
git -C "$WORK" diff --cached --numstat "$OBO_BASE_COMMIT" 2>/dev/null | awk -F'\t' '$1=="-" && $2=="-" {print $3}' | while IFS= read -r f; do
  [ -n "$f" ] && git -C "$WORK" reset -q -- "$f" 2>/dev/null
done
git -C "$WORK" diff --cached --binary "$OBO_BASE_COMMIT" >/out/patch.diff 2>/dev/null || true
git -C "$WORK" reset -q 2>/dev/null || true                                    # restore the index (leave the working tree untouched)
git -C "$WORK" diff --binary "$OBO_BASE_COMMIT" >/out/patch_tracked_only.diff 2>/dev/null || true
[ "${OBO_ARCHIVE_APP:-0}" = "1" ] && tar czf /out/app_state.tgz -C "$WORK" --exclude=.git --exclude=node_modules . 2>/dev/null || true
ROOT_TID="$("$OBO_PY" -c "import json;print(json.load(open('/out/solve_result.json')).get('task_id',''))" 2>/dev/null || echo '')"
SOLVE_EVENTS="$(wc -l < /out/solve_events.jsonl 2>/dev/null || echo 0)"
echo "[pro] ROOT-RUN patch=$(wc -c < /out/patch.diff)B events=$SOLVE_EVENTS task_id=$ROOT_TID" >&2
[ "$SOLVE_EVENTS" -lt 2 ] && echo "[pro] WARNING: SOLVE_INFRA_SUSPECT (too few events - possible server/network failure?)" >&2 || true

ABSORB_MAX="${OBO_ABSORB_MAX:-1800}"
echo "[pro] wait-for-absorb: max=${ABSORB_MAX}s (native post-task evolution)" >&2
"$OBO_PY" - "$ABSORB_MAX" >/out/absorb.json 2>/dev/null <<'PYEOF' || printf '{"absorbed":false,"reason":"error","cycles":0}' >/out/absorb.json
import json, os, subprocess, sys, time, urllib.request
MAX = int(sys.argv[1]); IDLE_GRACE = 180; URL = "http://127.0.0.1:8765/api/state"
CAMP = "/obo-data/state/evolution_campaign.json"
REQ  = "/obo-data/state/post_task_evolution_request.json"
def camp():
    try: return json.load(open(CAMP))
    except Exception: return {}
def absorbed():
    try: return int(camp().get("absorbed_cycles_done") or 0)
    except Exception: return 0
def head():
    try: return subprocess.run(["git","-C","/obo-repo","rev-parse","HEAD"],capture_output=True,text=True,timeout=15).stdout.strip()
    except Exception: return ""
def state():
    try:
        with urllib.request.urlopen(URL, timeout=5) as r: return json.loads(r.read().decode())
    except Exception: return {}
def pending_restart():
    at = camp().get("active_transaction") or {}
    return bool(str(at.get("commit_sha") or "").strip() and not at.get("restart_verified"))
def is_idle(st):
    return bool(st and st.get("supervisor_ready")
                and int(st.get("pending_count") or 0) == 0
                and int(st.get("running_count") or 0) == 0)
EVO0 = absorbed(); SHA0 = head(); t0 = time.time(); reason = "timeout"
while time.time() - t0 < MAX:
    c = absorbed(); sha = head()
    if c > EVO0 and sha and sha != SHA0:
        d2 = time.time() + 180   # Wait until the server is alive again after execvpe.
        while time.time() < d2:
            st = state()
            if st.get("supervisor_ready") and int(st.get("workers_total") or 0) > 0: break
            time.sleep(2)
        reason = "absorbed"; break
    if time.time() - t0 > IDLE_GRACE and c == EVO0 and is_idle(state()) and not os.path.exists(REQ):
        if pending_restart():
            time.sleep(6)
            if is_idle(state()) and pending_restart() and absorbed() == EVO0:
                reason = "degraded"; break
        else:
            reason = "no_promotion"; break
    time.sleep(5)
at = (camp().get("active_transaction") or {})
degraded = pending_restart()   # commit_sha exists AND restart_verified=False (same criterion as no_promotion)
print(json.dumps({"absorbed": reason == "absorbed", "reason": reason, "cycles": absorbed(),
                  "degraded": degraded, "active_tx_commit": str(at.get("commit_sha") or "")[:8],
                  "evo_before": EVO0, "sha_before": SHA0, "sha_after": head()}))
PYEOF
"$OBO_PY" - <<'PYEOF' >&2 2>/dev/null || true
import json
try: d = json.load(open('/out/absorb.json'))
except Exception: d = {}
print(f"[pro] evolution: absorbed={d.get('absorbed',False)} cycles={d.get('cycles',0)} reason={d.get('reason','?')} degraded={d.get('degraded',False)}")
if d.get('degraded'):
    print(f"[pro] EVOLUTION_DEGRADED_RECOVERABLE: cycle committed (tx={d.get('active_tx_commit','')}) but in-cycle restart verification did not pass. "
          f"Core boot reconciliation / supervisor auto-restart will recover the transaction at the next server boundary; the run continues.")
PYEOF

SI_TID="$ROOT_TID"

if ! PYTHONPATH=/obo-repo "$OBO_PY" -c "import ouroboros.cli, ouroboros.agent, ouroboros.loop, ouroboros.config, ouroboros.subagent_worktrees, ouroboros.tools.subagent_integration, ouroboros.workspace_executor, ouroboros.contracts.task_constraint, ouroboros.retention, server, supervisor.queue" 2>/out/health.err; then
  echo "[pro] HEALTH-GATE FAILED - rollback self-edit to $REPO_HEAD0" >&2
  sed 's/^/[pro]   health: /' /out/health.err >&2 2>/dev/null | head -8
  git -C /obo-repo diff "$REPO_HEAD0" > /out/rejected_self_edit.diff 2>/dev/null || true
  git -C /obo-repo reset -q --hard "$REPO_HEAD0" 2>/dev/null || true
  git -C /obo-repo clean -qfd 2>/dev/null || true
  echo "[pro] HEALTH_GATE_ROLLBACK done -> /out/rejected_self_edit.diff" >&2
else
  echo "[pro] health-gate OK" >&2
fi

REPO_HEAD1="$(git -C /obo-repo rev-parse HEAD 2>/dev/null || echo '')"
SI_ROLLBACK=0; [ -s /out/rejected_self_edit.diff ] && SI_ROLLBACK=1 || true
"$OBO_PY" - "$REPO_HEAD0" "$REPO_HEAD1" "${SI_TID:-}" "$SI_ROLLBACK" <<'PYEOF' > /out/selfedit.json 2>/dev/null || printf '{}' > /out/selfedit.json
import json, subprocess, sys, glob, re
h0, h1, si_tid, rb = (sys.argv[1] or ""), (sys.argv[2] or ""), (sys.argv[3] or ""), sys.argv[4]
def git(*a):
    try: return subprocess.run(["git","-C","/obo-repo",*a], capture_output=True, text=True, timeout=30).stdout
    except Exception: return ""
grew = bool(h0 and h1 and h0 != h1)
commits_n = len([l for l in git("rev-list", f"{h0}..{h1}").splitlines() if l.strip()]) if grew else 0
files = [l for l in git("diff","--name-only",h0,h1).splitlines() if l.strip()] if grew else []
ss = git("diff","--shortstat",h0,h1) if grew else ""
mi = re.search(r'(\d+) insertion', ss); ins = int(mi.group(1)) if mi else 0
md = re.search(r'(\d+) deletion', ss); dele = int(md.group(1)) if md else 0
tools = [f for f in files if f.startswith("ouroboros/tools/") or "/skills/" in f]
verdicts = {}
for p in glob.glob("/obo-data/**/subagent_patch_verdict_*.json", recursive=True):
    try: o = json.load(open(p))
    except Exception: continue
    if si_tid and str(o.get("parent_task_id","")) != si_tid: continue
    k = str(o.get("outcome","")); verdicts[k] = verdicts.get(k, 0) + 1
print(json.dumps({"repo_head_before": h0, "repo_head_after": h1, "health_rollback": bool(int(rb)),
  "commits_added": commits_n, "files_changed": len(files), "file_list": files[:50],
  "loc_added": ins, "loc_removed": dele, "tools_added": tools, "verdicts": verdicts}))
PYEOF
"$OBO_PY" - <<'PYEOF' >&2 2>/dev/null || true
import json
try: d = json.load(open('/out/selfedit.json'))
except Exception: d = {}
print(f"[pro] selfedit: commits={d.get('commits_added',0)} loc=+{d.get('loc_added',0)}/-{d.get('loc_removed',0)} "
      f"tools={len(d.get('tools_added',[]))} verdicts={d.get('verdicts',{})} rollback={d.get('health_rollback',False)}")
PYEOF

echo "[pro] self-edit (obo-repo): HEAD before=$REPO_HEAD0 after=$(git -C /obo-repo rev-parse HEAD 2>/dev/null)" >&2
git -C /obo-repo status --porcelain 2>/dev/null | head -20 | sed 's/^/[pro]   /' >&2
echo "[pro] knowledge files: $(find /obo-data/memory/knowledge -type f 2>/dev/null | wc -l | tr -d ' ')" >&2
kill "$SRV" 2>/dev/null || true
