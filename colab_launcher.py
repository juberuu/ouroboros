# ============================
# Ouroboros — Colab Launcher Cell (pull existing repo + run)
# Fixes: apply_patch shim + no "Drive already mounted" spam
#
# This file is a reference copy of the immutable Colab boot cell.
# The actual boot cell lives in the Colab notebook and must not be
# modified by the agent.  Keep this file in sync manually.
# ============================

import os, sys, json, time, uuid, pathlib, subprocess, datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests

# ----------------------------
# 0) Install deps
# ----------------------------
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "openai>=1.0.0", "requests", "playwright>=1.40.0"], check=True)
subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)

# ----------------------------
# 0.1) provide apply_patch shim (so LLM "apply_patch<<PATCH" won't crash)
# ----------------------------
APPLY_PATCH_PATH = pathlib.Path("/usr/local/bin/apply_patch")
APPLY_PATCH_CODE = r"""#!/usr/bin/env python3
import sys
import pathlib

def _norm_line(l: str) -> str:
    # accept both " context" and "context" as context lines
    if l.startswith(" "):
        return l[1:]
    return l

def _find_subseq(hay, needle):
    if not needle:
        return 0
    n = len(needle)
    for i in range(0, len(hay) - n + 1):
        ok = True
        for j in range(n):
            if hay[i + j] != needle[j]:
                ok = False
                break
        if ok:
            return i
    return -1

def _find_subseq_rstrip(hay, needle):
    if not needle:
        return 0
    hay2 = [x.rstrip() for x in hay]
    needle2 = [x.rstrip() for x in needle]
    return _find_subseq(hay2, needle2)

def apply_update_file(path: str, hunks: list[list[str]]):
    p = pathlib.Path(path)
    if not p.exists():
        sys.stderr.write(f"apply_patch: file not found: {path}\n")
        sys.exit(2)

    text = p.read_text(encoding="utf-8")
    src = text.splitlines()

    for hunk in hunks:
        old_seq = []
        new_seq = []
        for line in hunk:
            if line.startswith("+"):
                new_seq.append(line[1:])
            elif line.startswith("-"):
                old_seq.append(line[1:])
            else:
                c = _norm_line(line)
                old_seq.append(c)
                new_seq.append(c)

        idx = _find_subseq(src, old_seq)
        if idx < 0:
            idx = _find_subseq_rstrip(src, old_seq)
        if idx < 0:
            sys.stderr.write("apply_patch: failed to match hunk in file: " + path + "\n")
            sys.stderr.write("HUNK (old_seq):\n" + "\n".join(old_seq) + "\n")
            sys.exit(3)

        src = src[:idx] + new_seq + src[idx + len(old_seq):]

    p.write_text("\n".join(src) + "\n", encoding="utf-8")

def main():
    lines = sys.stdin.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("*** Begin Patch"):
            i += 1
            continue

        if line.startswith("*** Update File:"):
            path = line.split(":", 1)[1].strip()
            i += 1

            hunks = []
            cur = []
            while i < len(lines) and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if cur:
                        hunks.append(cur)
                        cur = []
                    i += 1
                    continue
                cur.append(lines[i])
                i += 1
            if cur:
                hunks.append(cur)

            apply_update_file(path, hunks)
            continue

        if line.startswith("*** End Patch"):
            i += 1
            continue

        # ignore unknown lines/blocks
        i += 1

if __name__ == "__main__":
    main()
"""
APPLY_PATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
APPLY_PATCH_PATH.write_text(APPLY_PATCH_CODE, encoding="utf-8")
APPLY_PATCH_PATH.chmod(0o755)

# ----------------------------
# 1) Secrets (Colab userdata -> env fallback)
# ----------------------------
from google.colab import userdata  # type: ignore
from google.colab import drive  # type: ignore

def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = userdata.get(name)
    if v is None:
        v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name}"
    return v

OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
TOTAL_BUDGET_DEFAULT = get_secret("TOTAL_BUDGET", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)

OPENAI_API_KEY = get_secret("OPENAI_API_KEY", default="")  # optional

GITHUB_USER = get_secret("GITHUB_USER", default="razzant")
GITHUB_REPO = get_secret("GITHUB_REPO", default="ouroboros")

MAX_WORKERS = int(get_secret("OUROBOROS_MAX_WORKERS", default="5") or "5")
MODEL_MAIN = get_secret("OUROBOROS_MODEL", default="openai/gpt-5.2")

# expose needed env to workers (do not print)
os.environ["OPENROUTER_API_KEY"] = str(OPENROUTER_API_KEY)
os.environ["OPENAI_API_KEY"] = str(OPENAI_API_KEY or "")
os.environ["OUROBOROS_MODEL"] = str(MODEL_MAIN or "openai/gpt-5.2")
os.environ["TELEGRAM_BOT_TOKEN"] = str(TELEGRAM_BOT_TOKEN)  # to support agent-side UX like typing indicator

# ----------------------------
# 2) Mount Drive (quietly)
# ----------------------------
if not pathlib.Path("/content/drive/MyDrive").exists():
    drive.mount("/content/drive")

DRIVE_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros").resolve()
REPO_DIR = pathlib.Path("/content/ouroboros_repo").resolve()

for sub in ["state", "logs", "memory", "index", "artifacts", "locks", "archive"]:
    (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)
REPO_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = DRIVE_ROOT / "state" / "state.json"

def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    st = {
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "owner_id": None,
        "owner_chat_id": None,
        "tg_offset": 0,
        "spent_usd": 0.0,
        "spent_calls": 0,
        "spent_tokens_prompt": 0,
        "spent_tokens_completion": 0,
        "approvals": {},
        "session_id": uuid.uuid4().hex,
        "current_branch": None,
        "current_sha": None,
    }
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    return st

def save_state(st: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")

CHAT_LOG_PATH = DRIVE_ROOT / "logs" / "chat.jsonl"
if not CHAT_LOG_PATH.exists():
    CHAT_LOG_PATH.write_text("", encoding="utf-8")

# ----------------------------
# 3) Git: clone/pull repo (no creation), dev->stable fallback
# ----------------------------
BRANCH_DEV = "ouroboros"
BRANCH_STABLE = "ouroboros-stable"

REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"

def ensure_repo_present() -> None:
    if not (REPO_DIR / ".git").exists():
        subprocess.run(["rm", "-rf", str(REPO_DIR)], check=False)
        subprocess.run(["git", "clone", REMOTE_URL, str(REPO_DIR)], check=True)
    else:
        subprocess.run(["git", "remote", "set-url", "origin", REMOTE_URL], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "config", "user.name", "Ouroboros"], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "config", "user.email", "ouroboros@users.noreply.github.com"], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)

def checkout_and_reset(branch: str) -> None:
    subprocess.run(["git", "checkout", branch], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(REPO_DIR), check=True)
    st = load_state()
    st["current_branch"] = branch
    st["current_sha"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR), capture_output=True, text=True, check=True).stdout.strip()
    save_state(st)

def import_test() -> Dict[str, Any]:
    r = subprocess.run(
        ["python3", "-c", "import ouroboros, ouroboros.agent; print('import_ok')"],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
    )
    return {"ok": (r.returncode == 0), "stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}

ensure_repo_present()
checkout_and_reset(BRANCH_DEV)
t = import_test()
if not t["ok"]:
    append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "import_fail_dev",
        "stdout": t["stdout"],
        "stderr": t["stderr"],
    })
    checkout_and_reset(BRANCH_STABLE)
    t2 = import_test()
    assert t2["ok"], f"Stable branch also failed import.\n\nSTDOUT:\n{t2['stdout']}\n\nSTDERR:\n{t2['stderr']}"

# ----------------------------
# 4) Telegram (long polling)
# ----------------------------
class TelegramClient:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def get_updates(self, offset: int, timeout: int = 10) -> List[Dict[str, Any]]:
        r = requests.get(
            f"{self.base}/getUpdates",
            params={"offset": offset, "timeout": timeout, "allowed_updates": ["message", "edited_message"]},
            timeout=timeout + 5,
        )
        data = r.json()
        assert data.get("ok") is True, f"Telegram getUpdates failed: {data}"
        return data.get("result") or []

    def send_message(self, chat_id: int, text: str) -> None:
        r = requests.post(
            f"{self.base}/sendMessage",
            data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=30,
        )
        data = r.json()
        assert data.get("ok") is True, f"Telegram sendMessage failed: {data}"

TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))

def split_telegram(text: str, limit: int = 3800) -> List[str]:
    chunks: List[str] = []
    s = text
    while len(s) > limit:
        cut = s.rfind("\n", 0, limit)
        if cut < 100:
            cut = limit
        chunks.append(s[:cut])
        s = s[cut:]
    chunks.append(s)
    return chunks

def budget_line() -> str:
    st = load_state()
    spent = float(st.get("spent_usd") or 0.0)
    total = float(get_secret("TOTAL_BUDGET", default=str(TOTAL_BUDGET_DEFAULT)) or TOTAL_BUDGET_DEFAULT)
    pct = (spent / total * 100.0) if total > 0 else 0.0
    sha = (st.get("current_sha") or "")[:8]
    branch = st.get("current_branch") or "?"
    return f"—\nBudget: ${spent:.4f} / ${total:.2f} ({pct:.2f}%) | {branch}@{sha}"

def log_chat(direction: str, chat_id: int, user_id: int, text: str) -> None:
    append_jsonl(DRIVE_ROOT / "logs" / "chat.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": load_state().get("session_id"),
        "direction": direction,
        "chat_id": chat_id,
        "user_id": user_id,
        "text": text,
    })

def send_with_budget(chat_id: int, text: str) -> None:
    st = load_state()
    owner_id = int(st.get("owner_id") or 0)
    log_chat("out", chat_id, owner_id, text)
    full = text.rstrip() + "\n\n" + budget_line()
    for part in split_telegram(full):
        TG.send_message(chat_id, part)

# ----------------------------
# 5) Workers + strict FIFO queue
# ----------------------------
import multiprocessing as mp
CTX = mp.get_context("fork")

@dataclass
class Worker:
    wid: int
    proc: mp.Process
    in_q: Any
    busy_task_id: Optional[str] = None

EVENT_Q = CTX.Queue()
WORKERS: Dict[int, Worker] = {}
PENDING: List[Dict[str, Any]] = []
RUNNING: Dict[str, Dict[str, Any]] = {}
CRASH_TS: List[float] = []

def worker_main(wid: int, in_q: Any, out_q: Any, repo_dir: str, drive_root: str) -> None:
    import sys as _sys
    _sys.path.insert(0, repo_dir)
    from ouroboros.agent import make_agent  # type: ignore
    agent = make_agent(repo_dir=repo_dir, drive_root=drive_root)
    while True:
        task = in_q.get()
        if task is None or task.get("type") == "shutdown":
            break
        events = agent.handle_task(task)
        for e in events:
            e2 = dict(e)
            e2["worker_id"] = wid
            out_q.put(e2)

def spawn_workers(n: int) -> None:
    WORKERS.clear()
    for i in range(n):
        in_q = CTX.Queue()
        proc = CTX.Process(target=worker_main, args=(i, in_q, EVENT_Q, str(REPO_DIR), str(DRIVE_ROOT)))
        proc.daemon = True
        proc.start()
        WORKERS[i] = Worker(wid=i, proc=proc, in_q=in_q, busy_task_id=None)

def kill_workers() -> None:
    for w in WORKERS.values():
        if w.proc.is_alive():
            w.proc.terminate()
    for w in WORKERS.values():
        w.proc.join(timeout=5)
    WORKERS.clear()

def assign_tasks() -> None:
    for w in WORKERS.values():
        if w.busy_task_id is None and PENDING:
            task = PENDING.pop(0)
            w.busy_task_id = task["id"]
            w.in_q.put(task)
            RUNNING[task["id"]] = task
            st = load_state()
            if st.get("owner_chat_id"):
                send_with_budget(int(st["owner_chat_id"]), f"▶️ Стартую задачу {task['id']} (worker {w.wid})")

def update_budget_from_usage(usage: Dict[str, Any]) -> None:
    st = load_state()
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if cost is None:
        cost = 0.0
    st["spent_usd"] = float(st.get("spent_usd") or 0.0) + float(cost)
    st["spent_calls"] = int(st.get("spent_calls") or 0) + 1
    st["spent_tokens_prompt"] = int(st.get("spent_tokens_prompt") or 0) + int(usage.get("prompt_tokens") or 0)
    st["spent_tokens_completion"] = int(st.get("spent_tokens_completion") or 0) + int(usage.get("completion_tokens") or 0)
    save_state(st)

def respawn_worker(wid: int) -> None:
    in_q = CTX.Queue()
    proc = CTX.Process(target=worker_main, args=(wid, in_q, EVENT_Q, str(REPO_DIR), str(DRIVE_ROOT)))
    proc.daemon = True
    proc.start()
    WORKERS[wid] = Worker(wid=wid, proc=proc, in_q=in_q, busy_task_id=None)

def ensure_workers_healthy() -> None:
    for wid, w in list(WORKERS.items()):
        if not w.proc.is_alive():
            CRASH_TS.append(time.time())
            if w.busy_task_id and w.busy_task_id in RUNNING:
                task = RUNNING.pop(w.busy_task_id)
                PENDING.insert(0, task)
            respawn_worker(wid)

    now = time.time()
    CRASH_TS[:] = [t for t in CRASH_TS if (now - t) < 60.0]
    # if crash storm, fallback to stable branch (import must work)
    if len(CRASH_TS) >= 3:
        st = load_state()
        if st.get("owner_chat_id"):
            send_with_budget(int(st["owner_chat_id"]), "⚠️ Частые падения воркеров. Переключаюсь на ouroboros-stable и перезапускаюсь.")
        checkout_and_reset(BRANCH_STABLE)
        kill_workers()
        spawn_workers(MAX_WORKERS)
        CRASH_TS.clear()

def rotate_chat_log_if_needed(max_bytes: int = 800_000) -> None:
    chat = DRIVE_ROOT / "logs" / "chat.jsonl"
    if not chat.exists():
        return
    if chat.stat().st_size < max_bytes:
        return
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_path = DRIVE_ROOT / "archive" / f"chat_{ts}.jsonl"
    archive_path.write_bytes(chat.read_bytes())
    chat.write_text("", encoding="utf-8")

def status_text() -> str:
    st = load_state()
    lines = []
    lines.append(f"owner_id: {st.get('owner_id')}")
    lines.append(f"session_id: {st.get('session_id')}")
    lines.append(f"version: {st.get('current_branch')}@{(st.get('current_sha') or '')[:8]}")
    lines.append(f"workers: {len(WORKERS)} (busy: {sum(1 for w in WORKERS.values() if w.busy_task_id is not None)})")
    lines.append(f"pending: {len(PENDING)}")
    if PENDING:
        lines.append("pending_ids: " + ", ".join([t["id"] for t in PENDING[:10]]))
    busy = [f"{w.wid}:{w.busy_task_id}" for w in WORKERS.values() if w.busy_task_id]
    if busy:
        lines.append("busy: " + ", ".join(busy))
    lines.append(f"spent_usd: {st.get('spent_usd')}")
    lines.append(f"spent_calls: {st.get('spent_calls')}")
    lines.append(f"prompt_tokens: {st.get('spent_tokens_prompt')}, completion_tokens: {st.get('spent_tokens_completion')}")
    return "\n".join(lines)

def cancel_task_by_id(task_id: str) -> bool:
    for i, t in enumerate(list(PENDING)):
        if t["id"] == task_id:
            PENDING.pop(i)
            return True
    for w in WORKERS.values():
        if w.busy_task_id == task_id:
            RUNNING.pop(task_id, None)
            if w.proc.is_alive():
                w.proc.terminate()
            w.proc.join(timeout=5)
            respawn_worker(w.wid)
            return True
    return False

def handle_approval(chat_id: int, text: str) -> bool:
    parts = text.strip().split()
    if not parts:
        return False
    cmd = parts[0].lower()
    if cmd not in ("/approve", "/deny"):
        return False
    assert len(parts) >= 2, "Usage: /approve <approval_id> or /deny <approval_id>"
    approval_id = parts[1].strip()
    st = load_state()
    approvals = st.get("approvals") or {}
    assert approval_id in approvals, f"Unknown approval_id: {approval_id}"
    approvals[approval_id]["status"] = "approved" if cmd == "/approve" else "denied"
    st["approvals"] = approvals
    save_state(st)
    send_with_budget(chat_id, f"OK: {cmd} {approval_id}")
    return True

# start
kill_workers()
spawn_workers(MAX_WORKERS)

append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
})

offset = int(load_state().get("tg_offset") or 0)

while True:
    rotate_chat_log_if_needed()
    ensure_workers_healthy()

    # Drain worker events
    while EVENT_Q.qsize() > 0:
        evt = EVENT_Q.get()
        et = evt.get("type")

        if et == "llm_usage":
            update_budget_from_usage(evt.get("usage") or {})
            continue

        if et == "send_message":
            send_with_budget(int(evt["chat_id"]), str(evt.get("text") or ""))
            continue

        if et == "task_done":
            task_id = evt.get("task_id")
            wid = evt.get("worker_id")
            if task_id:
                RUNNING.pop(str(task_id), None)
            if wid in WORKERS and WORKERS[wid].busy_task_id == task_id:
                WORKERS[wid].busy_task_id = None
            continue

        if et == "restart_request":
            st = load_state()
            if st.get("owner_chat_id"):
                send_with_budget(int(st["owner_chat_id"]), f"♻️ Restart requested by agent: {evt.get('reason')}")
            checkout_and_reset(BRANCH_DEV)
            it = import_test()
            if not it["ok"]:
                checkout_and_reset(BRANCH_STABLE)
            kill_workers()
            spawn_workers(MAX_WORKERS)
            continue

    assign_tasks()

    # Poll Telegram
    updates = TG.get_updates(offset=offset, timeout=10)
    for upd in updates:
        offset = int(upd["update_id"]) + 1
        msg = upd.get("message") or upd.get("edited_message") or {}
        if not msg:
            continue

        chat_id = int(msg["chat"]["id"])
        from_user = msg.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        text = str(msg.get("text") or "")

        st = load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id
            save_state(st)
            log_chat("in", chat_id, user_id, text)
            send_with_budget(chat_id, "✅ Owner registered. Ouroboros online.")
            continue

        if user_id != int(st.get("owner_id")):
            continue

        log_chat("in", chat_id, user_id, text)

        # immutable supervisor commands
        if text.strip().lower().startswith("/panic"):
            send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
            kill_workers()
            st2 = load_state()
            st2["tg_offset"] = offset
            save_state(st2)
            raise SystemExit("PANIC")

        if text.strip().lower().startswith("/restart"):
            st2 = load_state()
            st2["session_id"] = uuid.uuid4().hex
            save_state(st2)
            send_with_budget(chat_id, "♻️ Restarting (soft).")
            checkout_and_reset(BRANCH_DEV)
            it = import_test()
            if not it["ok"]:
                checkout_and_reset(BRANCH_STABLE)
            kill_workers()
            spawn_workers(MAX_WORKERS)
            continue

        if text.strip().lower().startswith("/status"):
            send_with_budget(chat_id, status_text())
            continue

        if handle_approval(chat_id, text):
            continue

        if text.strip().lower().startswith("/cancel"):
            parts = text.strip().split()
            assert len(parts) >= 2, "Usage: /cancel <task_id>"
            ok = cancel_task_by_id(parts[1])
            send_with_budget(chat_id, f"{'✅' if ok else '❌'} cancel {parts[1]}")
            continue

        tid = uuid.uuid4().hex[:8]
        PENDING.append({"id": tid, "type": "task", "chat_id": chat_id, "text": text})
        send_with_budget(chat_id, f"🧾 Принято. В очереди: {tid}. (workers={MAX_WORKERS}, pending={len(PENDING)})")

    st = load_state()
    st["tg_offset"] = offset
    save_state(st)

    time.sleep(0.2)
