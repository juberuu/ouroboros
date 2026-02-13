# ============================
# Ouroboros — Runtime launcher (executed from repository)
# ============================

import os, sys, json, time, uuid, pathlib, subprocess, datetime, re, shutil
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

# ----------------------------
# 0) Install launcher deps
# ----------------------------
def install_launcher_deps() -> None:
    # Keep launcher bootstrap minimal and stable.
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "openai>=1.0.0", "requests"],
        check=True,
    )


install_launcher_deps()

import requests

def ensure_claude_code_cli() -> bool:
    """Best-effort install of Claude Code CLI for Anthropic-powered code edits."""
    local_bin = str(pathlib.Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"

    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    if has_cli:
        return True

    # Preferred install method (native binary installer).
    subprocess.run(["bash", "-lc", "curl -fsSL https://claude.ai/install.sh | bash"], check=False)
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    if has_cli:
        return True

    # Fallback path for environments where native installer is unavailable.
    subprocess.run(["bash", "-lc", "command -v npm >/dev/null 2>&1 && npm install -g @anthropic-ai/claude-code"], check=False)
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    return has_cli

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
# 1) Secrets + runtime config
# ----------------------------
from google.colab import userdata  # type: ignore
from google.colab import drive  # type: ignore


_LEGACY_CFG_WARNED: Set[str] = set()


def _userdata_get(name: str) -> Optional[str]:
    try:
        return userdata.get(name)
    except Exception:
        return None


def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = _userdata_get(name)
    if v is None or str(v).strip() == "":
        v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name}"
    return v


def get_cfg(name: str, default: Optional[str] = None, allow_legacy_secret: bool = False) -> Optional[str]:
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return v
    if allow_legacy_secret:
        legacy = _userdata_get(name)
        if legacy is not None and str(legacy).strip() != "":
            if name not in _LEGACY_CFG_WARNED:
                print(f"[cfg] DEPRECATED: move {name} from Colab Secrets to config cell/env.")
                _LEGACY_CFG_WARNED.add(name)
            return legacy
    return default


OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
TOTAL_BUDGET_DEFAULT = get_secret("TOTAL_BUDGET", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)

try:
    TOTAL_BUDGET_LIMIT = float(TOTAL_BUDGET_DEFAULT)
except Exception:
    TOTAL_BUDGET_LIMIT = 0.0

OPENAI_API_KEY = get_secret("OPENAI_API_KEY", default="")  # optional
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY", default="")  # optional; enables Claude Code CLI tool

GITHUB_USER = get_cfg("GITHUB_USER", default="razzant", allow_legacy_secret=True)
GITHUB_REPO = get_cfg("GITHUB_REPO", default="ouroboros", allow_legacy_secret=True)

MAX_WORKERS = int(get_cfg("OUROBOROS_MAX_WORKERS", default="5", allow_legacy_secret=True) or "5")
MODEL_MAIN = get_cfg("OUROBOROS_MODEL", default="openai/gpt-5.2", allow_legacy_secret=True)
MODEL_CODE = get_cfg("OUROBOROS_MODEL_CODE", default="openai/gpt-5.2-codex", allow_legacy_secret=True)
MODEL_REVIEW = get_cfg("OUROBOROS_MODEL_REVIEW", default="openai/gpt-5.2", allow_legacy_secret=True)
MODEL_ROUTER = get_cfg("OUROBOROS_ROUTER_MODEL", default=str(MODEL_MAIN or "openai/gpt-5.2"), allow_legacy_secret=True)
ROUTER_REASONING_EFFORT = str(get_cfg("OUROBOROS_ROUTER_REASONING_EFFORT", default="low", allow_legacy_secret=True) or "low").strip().lower()
REASONING_DEFAULT_TASK = str(get_cfg("OUROBOROS_REASONING_DEFAULT_TASK", default="medium", allow_legacy_secret=True) or "medium").strip().lower()
REASONING_CODE_TASK = str(get_cfg("OUROBOROS_REASONING_CODE_TASK", default="high", allow_legacy_secret=True) or "high").strip().lower()
REASONING_EVOLUTION_TASK = str(get_cfg("OUROBOROS_REASONING_EVOLUTION_TASK", default="high", allow_legacy_secret=True) or "high").strip().lower()
REASONING_DEEP_REVIEW = str(get_cfg("OUROBOROS_REASONING_DEEP_REVIEW", default="xhigh", allow_legacy_secret=True) or "xhigh").strip().lower()
REASONING_MEMORY_SUMMARY = str(get_cfg("OUROBOROS_REASONING_MEMORY_SUMMARY", default="low", allow_legacy_secret=True) or "low").strip().lower()
REASONING_NOTICE = str(get_cfg("OUROBOROS_REASONING_NOTICE", default="low", allow_legacy_secret=True) or "low").strip().lower()

def as_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return default

IDLE_ENABLED = as_bool(get_cfg("OUROBOROS_IDLE_ENABLED", default="1", allow_legacy_secret=True), default=True)
IDLE_COOLDOWN_SEC = max(60, int(get_cfg("OUROBOROS_IDLE_COOLDOWN_SEC", default="900", allow_legacy_secret=True) or "900"))
IDLE_BUDGET_PCT_CAP = max(1.0, min(float(get_cfg("OUROBOROS_IDLE_BUDGET_PCT_CAP", default="35", allow_legacy_secret=True) or "35"), 100.0))
IDLE_MAX_PER_DAY = max(1, int(get_cfg("OUROBOROS_IDLE_MAX_PER_DAY", default="8", allow_legacy_secret=True) or "8"))
EVOLUTION_ENABLED_BY_DEFAULT = as_bool(get_cfg("OUROBOROS_EVOLUTION_ENABLED_BY_DEFAULT", default="0", allow_legacy_secret=True), default=False)
BUDGET_REPORT_EVERY_MESSAGES = max(1, int(get_cfg("OUROBOROS_BUDGET_REPORT_EVERY_MESSAGES", default="10", allow_legacy_secret=True) or "10"))
QUEUE_SOFT_TIMEOUT_1_SEC = max(60, int(get_cfg("OUROBOROS_TASK_SOFT_TIMEOUT_1_SEC", default="300", allow_legacy_secret=True) or "300"))
QUEUE_SOFT_TIMEOUT_2_SEC = max(120, int(get_cfg("OUROBOROS_TASK_SOFT_TIMEOUT_2_SEC", default="600", allow_legacy_secret=True) or "600"))
QUEUE_HARD_TIMEOUT_SEC = max(180, int(get_cfg("OUROBOROS_TASK_HARD_TIMEOUT_SEC", default="900", allow_legacy_secret=True) or "900"))
QUEUE_MAX_RETRIES = max(0, int(get_cfg("OUROBOROS_TASK_MAX_RETRIES", default="1", allow_legacy_secret=True) or "1"))
HEARTBEAT_STALE_SEC = max(30, int(get_cfg("OUROBOROS_TASK_HEARTBEAT_STALE_SEC", default="120", allow_legacy_secret=True) or "120"))
AUTO_REVIEW_MIN_GAP_SEC = max(60, int(get_cfg("OUROBOROS_AUTO_REVIEW_MIN_GAP_SEC", default="300", allow_legacy_secret=True) or "300"))
REVIEW_COMPLEX_MIN_DURATION_SEC = max(60, int(get_cfg("OUROBOROS_REVIEW_COMPLEX_MIN_DURATION_SEC", default="180", allow_legacy_secret=True) or "180"))
REVIEW_COMPLEX_MIN_TOOL_CALLS = max(2, int(get_cfg("OUROBOROS_REVIEW_COMPLEX_MIN_TOOL_CALLS", default="8", allow_legacy_secret=True) or "8"))
REVIEW_COMPLEX_MIN_TOOL_ERRORS = max(1, int(get_cfg("OUROBOROS_REVIEW_COMPLEX_MIN_TOOL_ERRORS", default="2", allow_legacy_secret=True) or "2"))
TASK_HEARTBEAT_SEC = max(10, int(get_cfg("OUROBOROS_TASK_HEARTBEAT_SEC", default="30", allow_legacy_secret=True) or "30"))

# expose needed env to workers (do not print)
os.environ["OPENROUTER_API_KEY"] = str(OPENROUTER_API_KEY)
os.environ["OPENAI_API_KEY"] = str(OPENAI_API_KEY or "")
os.environ["ANTHROPIC_API_KEY"] = str(ANTHROPIC_API_KEY or "")
os.environ["GITHUB_USER"] = str(GITHUB_USER or "razzant")
os.environ["GITHUB_REPO"] = str(GITHUB_REPO or "ouroboros")
os.environ["OUROBOROS_MODEL"] = str(MODEL_MAIN or "openai/gpt-5.2")
os.environ["OUROBOROS_MODEL_CODE"] = str(MODEL_CODE or "openai/gpt-5.2-codex")
os.environ["OUROBOROS_MODEL_REVIEW"] = str(MODEL_REVIEW or "openai/gpt-5.2")
os.environ["OUROBOROS_ROUTER_MODEL"] = str(MODEL_ROUTER or "openai/gpt-5.2")
os.environ["OUROBOROS_ROUTER_REASONING_EFFORT"] = str(ROUTER_REASONING_EFFORT or "low")
os.environ["OUROBOROS_REASONING_DEFAULT_TASK"] = str(REASONING_DEFAULT_TASK or "medium")
os.environ["OUROBOROS_REASONING_CODE_TASK"] = str(REASONING_CODE_TASK or "high")
os.environ["OUROBOROS_REASONING_EVOLUTION_TASK"] = str(REASONING_EVOLUTION_TASK or "high")
os.environ["OUROBOROS_REASONING_DEEP_REVIEW"] = str(REASONING_DEEP_REVIEW or "xhigh")
os.environ["OUROBOROS_REASONING_MEMORY_SUMMARY"] = str(REASONING_MEMORY_SUMMARY or "low")
os.environ["OUROBOROS_REASONING_NOTICE"] = str(REASONING_NOTICE or "low")
os.environ["OUROBOROS_TASK_HEARTBEAT_SEC"] = str(TASK_HEARTBEAT_SEC)
os.environ["OUROBOROS_REVIEW_COMPLEX_MIN_DURATION_SEC"] = str(REVIEW_COMPLEX_MIN_DURATION_SEC)
os.environ["OUROBOROS_REVIEW_COMPLEX_MIN_TOOL_CALLS"] = str(REVIEW_COMPLEX_MIN_TOOL_CALLS)
os.environ["OUROBOROS_REVIEW_COMPLEX_MIN_TOOL_ERRORS"] = str(REVIEW_COMPLEX_MIN_TOOL_ERRORS)
os.environ["TELEGRAM_BOT_TOKEN"] = str(TELEGRAM_BOT_TOKEN)  # to support agent-side UX like typing indicator

# Install Claude Code CLI only when Anthropic API access is configured.
if str(ANTHROPIC_API_KEY or "").strip():
    ensure_claude_code_cli()

# ----------------------------
# 2) Mount Drive (quietly)
# ----------------------------
if not pathlib.Path("/content/drive/MyDrive").exists():
    drive.mount("/content/drive")

DRIVE_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros").resolve()
REPO_DIR = pathlib.Path("/content/ouroboros_repo").resolve()

for sub in ["state", "logs", "memory", "index", "locks", "archive"]:
    (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)
REPO_DIR.mkdir(parents=True, exist_ok=True)

STATE_PATH = DRIVE_ROOT / "state" / "state.json"
STATE_LAST_GOOD_PATH = DRIVE_ROOT / "state" / "state.last_good.json"
STATE_LOCK_PATH = DRIVE_ROOT / "locks" / "state.lock"
QUEUE_SNAPSHOT_PATH = DRIVE_ROOT / "state" / "queue_snapshot.json"

def ensure_state_defaults(st: Dict[str, Any]) -> Dict[str, Any]:
    st.setdefault("created_at", datetime.datetime.now(datetime.timezone.utc).isoformat())
    st.setdefault("owner_id", None)
    st.setdefault("owner_chat_id", None)
    st.setdefault("tg_offset", 0)
    st.setdefault("spent_usd", 0.0)
    st.setdefault("spent_calls", 0)
    st.setdefault("spent_tokens_prompt", 0)
    st.setdefault("spent_tokens_completion", 0)
    st.setdefault("approvals", {})
    st.setdefault("session_id", uuid.uuid4().hex)
    st.setdefault("current_branch", None)
    st.setdefault("current_sha", None)
    st.setdefault("last_owner_message_at", "")
    st.setdefault("last_idle_task_at", "")
    st.setdefault("last_evolution_task_at", "")
    st.setdefault("idle_cursor", 0)
    st.setdefault("budget_messages_since_report", 0)
    st.setdefault("evolution_mode_enabled", EVOLUTION_ENABLED_BY_DEFAULT)
    st.setdefault("evolution_cycle", 0)
    st.setdefault("last_auto_review_at", "")
    st.setdefault("last_review_task_id", "")
    st.setdefault("queue_seq", 0)
    if not isinstance(st.get("idle_stats"), dict):
        st["idle_stats"] = {}
    return st

def _default_state_dict() -> Dict[str, Any]:
    return {
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
        "last_owner_message_at": "",
        "last_idle_task_at": "",
        "last_evolution_task_at": "",
        "idle_cursor": 0,
        "budget_messages_since_report": 0,
        "evolution_mode_enabled": EVOLUTION_ENABLED_BY_DEFAULT,
        "evolution_cycle": 0,
        "idle_stats": {},
        "last_auto_review_at": "",
        "last_review_task_id": "",
        "queue_seq": 0,
    }

def _acquire_file_lock(lock_path: pathlib.Path, timeout_sec: float = 4.0, stale_sec: float = 90.0) -> Optional[int]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    while (time.time() - started) < timeout_sec:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            try:
                os.write(fd, f"pid={os.getpid()} ts={datetime.datetime.now(datetime.timezone.utc).isoformat()}\n".encode("utf-8"))
            except Exception:
                pass
            return fd
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_sec:
                    lock_path.unlink()
                    continue
            except Exception:
                pass
            time.sleep(0.05)
        except Exception:
            break
    return None

def _release_file_lock(lock_path: pathlib.Path, lock_fd: Optional[int]) -> None:
    if lock_fd is None:
        return
    try:
        os.close(lock_fd)
    except Exception:
        pass
    try:
        if lock_path.exists():
            lock_path.unlink()
    except Exception:
        pass

def _atomic_write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        data = content.encode("utf-8")
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(str(tmp), str(path))

def _json_load_file(path: pathlib.Path) -> Optional[Dict[str, Any]]:
    try:
        if not path.exists():
            return None
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None

def load_state() -> Dict[str, Any]:
    lock_fd = _acquire_file_lock(STATE_LOCK_PATH)
    try:
        recovered = False
        st_obj = _json_load_file(STATE_PATH)
        if st_obj is None:
            st_obj = _json_load_file(STATE_LAST_GOOD_PATH)
            recovered = st_obj is not None

        if st_obj is None:
            st = ensure_state_defaults(_default_state_dict())
            payload = json.dumps(st, ensure_ascii=False, indent=2)
            _atomic_write_text(STATE_PATH, payload)
            _atomic_write_text(STATE_LAST_GOOD_PATH, payload)
            return st

        st = ensure_state_defaults(st_obj)
        if recovered:
            payload = json.dumps(st, ensure_ascii=False, indent=2)
            _atomic_write_text(STATE_PATH, payload)
            _atomic_write_text(STATE_LAST_GOOD_PATH, payload)
        return st
    finally:
        _release_file_lock(STATE_LOCK_PATH, lock_fd)

def save_state(st: Dict[str, Any]) -> None:
    st = ensure_state_defaults(st)
    lock_fd = _acquire_file_lock(STATE_LOCK_PATH)
    try:
        payload = json.dumps(st, ensure_ascii=False, indent=2)
        _atomic_write_text(STATE_PATH, payload)
        _atomic_write_text(STATE_LAST_GOOD_PATH, payload)
    finally:
        _release_file_lock(STATE_LOCK_PATH, lock_fd)

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

def _git_capture(cmd: List[str]) -> Tuple[int, str, str]:
    r = subprocess.run(cmd, cwd=str(REPO_DIR), capture_output=True, text=True)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()

def _collect_repo_sync_state() -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "current_branch": "unknown",
        "dirty_lines": [],
        "unpushed_lines": [],
        "warnings": [],
    }

    rc, branch, err = _git_capture(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if rc == 0 and branch:
        state["current_branch"] = branch
    elif err:
        state["warnings"].append(f"branch_error:{err}")

    rc, dirty, err = _git_capture(["git", "status", "--porcelain"])
    if rc == 0 and dirty:
        state["dirty_lines"] = [ln for ln in dirty.splitlines() if ln.strip()]
    elif rc != 0 and err:
        state["warnings"].append(f"status_error:{err}")

    upstream = ""
    rc, up, err = _git_capture(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if rc == 0 and up:
        upstream = up
    else:
        current_branch = str(state.get("current_branch") or "")
        if current_branch not in ("", "HEAD", "unknown"):
            upstream = f"origin/{current_branch}"
        elif err:
            state["warnings"].append(f"upstream_error:{err}")

    if upstream:
        rc, unpushed, err = _git_capture(["git", "log", "--oneline", f"{upstream}..HEAD"])
        if rc == 0 and unpushed:
            state["unpushed_lines"] = [ln for ln in unpushed.splitlines() if ln.strip()]
        elif rc != 0 and err:
            state["warnings"].append(f"unpushed_error:{err}")

    return state

def _copy_untracked_for_rescue(dst_root: pathlib.Path, max_files: int = 200, max_total_bytes: int = 12_000_000) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "copied_files": 0,
        "skipped_files": 0,
        "copied_bytes": 0,
        "truncated": False,
    }
    rc, txt, err = _git_capture(["git", "ls-files", "--others", "--exclude-standard"])
    if rc != 0:
        out["error"] = err or "git ls-files failed"
        return out

    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    if not lines:
        return out

    dst_root.mkdir(parents=True, exist_ok=True)
    for rel in lines:
        if out["copied_files"] >= max_files:
            out["truncated"] = True
            break
        src = (REPO_DIR / rel).resolve()
        try:
            # Avoid path traversal and weird symlink surprises.
            src.relative_to(REPO_DIR.resolve())
        except Exception:
            out["skipped_files"] += 1
            continue
        if not src.exists() or not src.is_file():
            out["skipped_files"] += 1
            continue
        try:
            size = int(src.stat().st_size)
        except Exception:
            out["skipped_files"] += 1
            continue
        if (out["copied_bytes"] + size) > max_total_bytes:
            out["truncated"] = True
            break
        dst = dst_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(src, dst)
            out["copied_files"] += 1
            out["copied_bytes"] += size
        except Exception:
            out["skipped_files"] += 1
    return out


def _create_rescue_snapshot(branch: str, reason: str, repo_state: Dict[str, Any]) -> Dict[str, Any]:
    now = datetime.datetime.now(datetime.timezone.utc)
    ts = now.strftime("%Y%m%d_%H%M%S")
    rescue_dir = DRIVE_ROOT / "archive" / "rescue" / f"{ts}_{uuid.uuid4().hex[:8]}"
    rescue_dir.mkdir(parents=True, exist_ok=True)

    info: Dict[str, Any] = {
        "ts": now.isoformat(),
        "target_branch": branch,
        "reason": reason,
        "current_branch": repo_state.get("current_branch"),
        "dirty_count": len(repo_state.get("dirty_lines") or []),
        "unpushed_count": len(repo_state.get("unpushed_lines") or []),
        "warnings": list(repo_state.get("warnings") or []),
        "path": str(rescue_dir),
    }

    rc_status, status_txt, _ = _git_capture(["git", "status", "--porcelain"])
    if rc_status == 0:
        _atomic_write_text(rescue_dir / "status.porcelain.txt", status_txt + ("\n" if status_txt else ""))

    rc_diff, diff_txt, diff_err = _git_capture(["git", "diff", "--binary", "HEAD"])
    if rc_diff == 0:
        _atomic_write_text(rescue_dir / "changes.diff", diff_txt + ("\n" if diff_txt else ""))
    else:
        info["diff_error"] = diff_err or "git diff failed"

    untracked_meta = _copy_untracked_for_rescue(rescue_dir / "untracked")
    info["untracked"] = untracked_meta

    unpushed_lines = [ln for ln in (repo_state.get("unpushed_lines") or []) if str(ln).strip()]
    if unpushed_lines:
        _atomic_write_text(rescue_dir / "unpushed_commits.txt", "\n".join(unpushed_lines) + "\n")

    _atomic_write_text(rescue_dir / "rescue_meta.json", json.dumps(info, ensure_ascii=False, indent=2))
    return info


def checkout_and_reset(branch: str, reason: str = "unspecified", unsynced_policy: str = "ignore") -> Tuple[bool, str]:
    # Always refresh refs before any reset-to-origin action.
    rc, _, err = _git_capture(["git", "fetch", "origin"])
    if rc != 0:
        msg = f"git fetch failed: {err or 'unknown error'}"
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "reset_fetch_failed",
                "target_branch": branch,
                "reason": reason,
                "error": msg,
            },
        )
        return False, msg

    policy = str(unsynced_policy or "ignore").strip().lower()
    if policy not in {"ignore", "block", "rescue_and_block", "rescue_and_reset"}:
        policy = "ignore"

    if policy != "ignore":
        repo_state = _collect_repo_sync_state()
        dirty_lines = list(repo_state.get("dirty_lines") or [])
        unpushed_lines = list(repo_state.get("unpushed_lines") or [])
        if dirty_lines or unpushed_lines:
            rescue_info: Dict[str, Any] = {}
            if policy in {"rescue_and_block", "rescue_and_reset"}:
                try:
                    rescue_info = _create_rescue_snapshot(branch=branch, reason=reason, repo_state=repo_state)
                except Exception as e:
                    rescue_info = {"error": repr(e)}
            bits: List[str] = []
            if unpushed_lines:
                bits.append(f"unpushed={len(unpushed_lines)}")
            if dirty_lines:
                bits.append(f"dirty={len(dirty_lines)}")
            detail = ", ".join(bits) if bits else "unsynced"
            rescue_suffix = ""
            rescue_path = str(rescue_info.get("path") or "").strip()
            if rescue_path:
                rescue_suffix = f" Rescue saved to {rescue_path}."
            elif policy in {"rescue_and_block", "rescue_and_reset"} and rescue_info.get("error"):
                rescue_suffix = f" Rescue failed: {rescue_info.get('error')}."

            if policy in {"block", "rescue_and_block"}:
                msg = f"Reset blocked ({detail}) to protect local changes.{rescue_suffix}"
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "reset_blocked_unsynced_state",
                        "target_branch": branch,
                        "reason": reason,
                        "policy": policy,
                        "current_branch": repo_state.get("current_branch"),
                        "dirty_count": len(dirty_lines),
                        "unpushed_count": len(unpushed_lines),
                        "dirty_preview": dirty_lines[:20],
                        "unpushed_preview": unpushed_lines[:20],
                        "warnings": list(repo_state.get("warnings") or []),
                        "rescue": rescue_info,
                    },
                )
                return False, msg

            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "reset_unsynced_rescued_then_reset",
                    "target_branch": branch,
                    "reason": reason,
                    "policy": policy,
                    "current_branch": repo_state.get("current_branch"),
                    "dirty_count": len(dirty_lines),
                    "unpushed_count": len(unpushed_lines),
                    "dirty_preview": dirty_lines[:20],
                    "unpushed_preview": unpushed_lines[:20],
                    "warnings": list(repo_state.get("warnings") or []),
                    "rescue": rescue_info,
                },
            )

    subprocess.run(["git", "checkout", branch], cwd=str(REPO_DIR), check=True)
    subprocess.run(["git", "reset", "--hard", f"origin/{branch}"], cwd=str(REPO_DIR), check=True)
    st = load_state()
    st["current_branch"] = branch
    st["current_sha"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(REPO_DIR), capture_output=True, text=True, check=True).stdout.strip()
    save_state(st)
    return True, "ok"


def sync_runtime_dependencies(reason: str) -> Tuple[bool, str]:
    req_path = REPO_DIR / "requirements.txt"
    cmd: List[str] = [sys.executable, "-m", "pip", "install", "-q"]
    source = ""
    if req_path.exists():
        cmd += ["-r", str(req_path)]
        source = f"requirements:{req_path}"
    else:
        cmd += ["openai>=1.0.0", "requests"]
        source = "fallback:minimal"
    try:
        subprocess.run(cmd, cwd=str(REPO_DIR), check=True)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "deps_sync_ok",
                "reason": reason,
                "source": source,
            },
        )
        return True, source
    except Exception as e:
        msg = repr(e)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "deps_sync_error",
                "reason": reason,
                "source": source,
                "error": msg,
            },
        )
        return False, msg

def import_test() -> Dict[str, Any]:
    r = subprocess.run(
        ["python3", "-c", "import ouroboros, ouroboros.agent; print('import_ok')"],
        cwd=str(REPO_DIR),
        capture_output=True,
        text=True,
    )
    return {"ok": (r.returncode == 0), "stdout": r.stdout, "stderr": r.stderr, "returncode": r.returncode}

ensure_repo_present()
ok_dev, err_dev = checkout_and_reset(BRANCH_DEV, reason="bootstrap_dev", unsynced_policy="rescue_and_reset")
assert ok_dev, f"Failed to prepare {BRANCH_DEV}: {err_dev}"
deps_ok, deps_msg = sync_runtime_dependencies(reason="bootstrap_dev")
assert deps_ok, f"Failed to install runtime dependencies for {BRANCH_DEV}: {deps_msg}"
t = import_test()
if not t["ok"]:
    append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "import_fail_dev",
        "stdout": t["stdout"],
        "stderr": t["stderr"],
    })
    ok_stable, err_stable = checkout_and_reset(BRANCH_STABLE, reason="bootstrap_fallback_stable", unsynced_policy="rescue_and_reset")
    assert ok_stable, f"Failed to prepare {BRANCH_STABLE}: {err_stable}"
    deps_ok_stable, deps_msg_stable = sync_runtime_dependencies(reason="bootstrap_fallback_stable")
    assert deps_ok_stable, f"Failed to install runtime dependencies for {BRANCH_STABLE}: {deps_msg_stable}"
    t2 = import_test()
    assert t2["ok"], f"Stable branch also failed import.\n\nSTDOUT:\n{t2['stdout']}\n\nSTDERR:\n{t2['stderr']}"

# ----------------------------
# 4) Telegram (long polling)
# ----------------------------
class TelegramClient:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}"

    def get_updates(self, offset: int, timeout: int = 10) -> List[Dict[str, Any]]:
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = requests.get(
                    f"{self.base}/getUpdates",
                    params={"offset": offset, "timeout": timeout, "allowed_updates": ["message", "edited_message"]},
                    timeout=timeout + 5,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("ok") is not True:
                    raise RuntimeError(f"Telegram getUpdates failed: {data}")
                return data.get("result") or []
            except Exception as e:
                last_err = repr(e)
                if attempt < 2:
                    time.sleep(0.8 * (attempt + 1))
        raise RuntimeError(f"Telegram getUpdates failed after retries: {last_err}")

    def send_message(self, chat_id: int, text: str) -> Tuple[bool, str]:
        last_err = "unknown"
        for attempt in range(3):
            try:
                r = requests.post(
                    f"{self.base}/sendMessage",
                    data={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
                    timeout=30,
                )
                r.raise_for_status()
                data = r.json()
                if data.get("ok") is True:
                    return True, "ok"
                last_err = f"telegram_api_error: {data}"
            except Exception as e:
                last_err = repr(e)

            if attempt < 2:
                time.sleep(0.8 * (attempt + 1))

        return False, last_err

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

def _format_budget_line(st: Dict[str, Any]) -> str:
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    pct = (spent / total * 100.0) if total > 0 else 0.0
    sha = (st.get("current_sha") or "")[:8]
    branch = st.get("current_branch") or "?"
    return f"—\nBudget: ${spent:.4f} / ${total:.2f} ({pct:.2f}%) | {branch}@{sha}"


def budget_line(force: bool = False) -> str:
    """Return budget line every N outgoing messages.

    - force=True always prints and resets the message counter.
    - default cadence comes from OUROBOROS_BUDGET_REPORT_EVERY_MESSAGES (default: 10).
    """
    try:
        st = load_state()
        every = max(1, int(BUDGET_REPORT_EVERY_MESSAGES))
        if force:
            st["budget_messages_since_report"] = 0
            save_state(st)
            return _format_budget_line(st)

        counter = int(st.get("budget_messages_since_report") or 0) + 1
        if counter < every:
            st["budget_messages_since_report"] = counter
            save_state(st)
            return ""

        st["budget_messages_since_report"] = 0
        save_state(st)
        return _format_budget_line(st)
    except Exception:
        # Never fail message sending because of budget reporting.
        return ""

def log_chat(direction: str, chat_id: int, user_id: int, text: str) -> None:
    append_jsonl(DRIVE_ROOT / "logs" / "chat.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "session_id": load_state().get("session_id"),
        "direction": direction,
        "chat_id": chat_id,
        "user_id": user_id,
        "text": text,
    })

def send_with_budget(chat_id: int, text: str, log_text: Optional[str] = None, force_budget: bool = False) -> None:
    st = load_state()
    owner_id = int(st.get("owner_id") or 0)
    log_chat("out", chat_id, owner_id, text if log_text is None else log_text)
    budget = budget_line(force=force_budget)
    _text = str(text or "")
    # If we already sent the main message directly from the worker, it may pass a zero-width space (\u200b)
    # to ask the supervisor to send only the budget line. If budget is not due, skip sending to avoid blank messages.
    if not budget:
        if _text.strip() in ("", "\u200b"):
            return
        full = _text
    else:
        base = _text.rstrip()
        if base in ("", "\u200b"):
            full = budget
        else:
            full = base + "\n\n" + budget
    for idx, part in enumerate(split_telegram(full)):
        ok, err = TG.send_message(chat_id, part)
        if not ok:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "telegram_send_error",
                    "chat_id": chat_id,
                    "part_index": idx,
                    "error": err,
                },
            )
            break

# ----------------------------
# 4.5) Router: direct answer vs full agent task
# ----------------------------
ROUTER_SYSTEM_PROMPT = """You are Ouroboros (Уроборос), a self-modifying AI agent.
Your job RIGHT NOW: decide whether to answer the user directly or route to the full agent pipeline.

ANSWER DIRECTLY (just write the answer) if:
- Simple questions (math, facts, definitions, opinions)
- Casual conversation, greetings, thanks
- General knowledge questions
- Explaining concepts
- Questions about yourself ONLY when they are generic and don't require checking runtime state

RESPOND WITH EXACTLY "NEEDS_TASK" on the FIRST LINE if the message requires:
- Reading or writing files, code, configs
- Git operations (commit, push, diff, status)
- Web search for fresh/current information
- Log analysis, examining Drive files
- Code changes or self-modification
- Running shell commands
- Any tool or system access
- Analyzing repository contents
- Requesting deep system review / health audit / full context inspection
- Checking current runtime state/capabilities (available tools, CLI presence, current branch/version, recent action results)
- Anything you're unsure about

Use available conversation context to avoid mistaken direct answers when tool access is required.
When answering directly, respond in the user's language. Be concise and helpful.
When routing to task, write NEEDS_TASK on the first line, then optionally a brief reason."""

def _normalize_reasoning_effort(v: str, default: str = "medium") -> str:
    allowed = {"none", "minimal", "low", "medium", "high", "xhigh"}
    s = str(v or "").strip().lower()
    return s if s in allowed else default

def route_and_maybe_answer(text: str) -> Optional[str]:
    """Quick LLM call: return direct answer or None (meaning 'create a full task')."""
    try:
        from openai import OpenAI
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ["OPENROUTER_API_KEY"],
            default_headers={"HTTP-Referer": "https://colab.research.google.com/", "X-Title": "Ouroboros-Router"},
        )

        # Minimal context: last ~10 chat messages for conversational continuity
        recent_chat = ""
        if CHAT_LOG_PATH.exists():
            try:
                lines = CHAT_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
                recent_lines = lines[-10:] if len(lines) > 10 else lines
                recent_chat = "\n".join(recent_lines)
            except Exception:
                pass

        messages = [
            {"role": "system", "content": ROUTER_SYSTEM_PROMPT},
        ]
        if recent_chat:
            messages.append({"role": "system", "content": f"Recent chat context (JSONL):\n{recent_chat}"})
        messages.append({"role": "user", "content": text})

        router_model = os.environ.get("OUROBOROS_ROUTER_MODEL", os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2"))
        router_effort = _normalize_reasoning_effort(
            os.environ.get("OUROBOROS_ROUTER_REASONING_EFFORT", ROUTER_REASONING_EFFORT),
            default="low",
        )

        resp = client.chat.completions.create(
            model=router_model,
            messages=messages,
            max_tokens=2000,
            extra_body={"reasoning": {"effort": router_effort, "exclude": True}},
        )
        resp_dict = resp.model_dump()

        # Track router cost
        usage = (resp_dict.get("usage") or {})
        update_budget_from_usage(usage)

        answer = (resp.choices[0].message.content or "").strip()
        append_jsonl(
            DRIVE_ROOT / "logs" / "events.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "router_profile_used",
                "model": router_model,
                "reasoning_effort": router_effort,
                "answered_directly": (not answer.startswith("NEEDS_TASK")),
            },
        )
        if answer.startswith("NEEDS_TASK"):
            return None
        return answer
    except Exception as e:
        # On any error, fall through to task creation
        append_jsonl(DRIVE_ROOT / "logs" / "events.jsonl", {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "router_error", "error": repr(e),
        })
        return None

# ----------------------------
# 5) Workers + prioritized queue
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
LAST_EVOLUTION_SKIP_SIGNATURE: Optional[Tuple[int, int]] = None
QUEUE_SEQ_COUNTER = 0

def _task_priority(task_type: str) -> int:
    t = str(task_type or "").strip().lower()
    if t in ("task", "review"):
        return 0
    if t == "evolution":
        return 1
    if t == "idle":
        return 2
    return 3

def _queue_sort_key(task: Dict[str, Any]) -> Tuple[int, int]:
    pr = int(task.get("priority") or _task_priority(str(task.get("type") or "")))
    seq = int(task.get("_queue_seq") or 0)
    return pr, seq

def _sort_pending() -> None:
    PENDING.sort(key=_queue_sort_key)

def enqueue_task(task: Dict[str, Any], front: bool = False) -> Dict[str, Any]:
    global QUEUE_SEQ_COUNTER
    t = dict(task)
    QUEUE_SEQ_COUNTER += 1
    t.setdefault("priority", _task_priority(str(t.get("type") or "")))
    t.setdefault("_attempt", int(t.get("_attempt") or 1))
    t["_queue_seq"] = -QUEUE_SEQ_COUNTER if front else QUEUE_SEQ_COUNTER
    t["queued_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    PENDING.append(t)
    _sort_pending()
    return t

def _queue_has_task_type(task_type: str) -> bool:
    tt = str(task_type or "")
    if any(str(t.get("type") or "") == tt for t in PENDING):
        return True
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        if isinstance(task, dict) and str(task.get("type") or "") == tt:
            return True
    return False

def _running_task_type_counts() -> Dict[str, int]:
    out: Dict[str, int] = {}
    for meta in RUNNING.values():
        task = meta.get("task") if isinstance(meta, dict) else None
        tt = str((task or {}).get("type") or "")
        out[tt] = int(out.get(tt) or 0) + 1
    return out

def persist_queue_snapshot(reason: str = "") -> None:
    pending_rows = []
    for t in PENDING:
        pending_rows.append(
            {
                "id": t.get("id"),
                "type": t.get("type"),
                "priority": t.get("priority"),
                "attempt": t.get("_attempt"),
                "queued_at": t.get("queued_at"),
                "queue_seq": t.get("_queue_seq"),
                "task": {
                    "id": t.get("id"),
                    "type": t.get("type"),
                    "chat_id": t.get("chat_id"),
                    "text": t.get("text"),
                    "priority": t.get("priority"),
                    "_attempt": t.get("_attempt"),
                    "review_reason": t.get("review_reason"),
                    "review_source_task_id": t.get("review_source_task_id"),
                },
            }
        )
    running_rows = []
    now = time.time()
    for task_id, meta in RUNNING.items():
        task = meta.get("task") if isinstance(meta, dict) else {}
        started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
        hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
        running_rows.append(
            {
                "id": task_id,
                "type": task.get("type"),
                "priority": task.get("priority"),
                "attempt": meta.get("attempt"),
                "worker_id": meta.get("worker_id"),
                "runtime_sec": round(max(0.0, now - started), 2) if started > 0 else 0.0,
                "heartbeat_lag_sec": round(max(0.0, now - hb), 2) if hb > 0 else None,
                "soft5_sent": bool(meta.get("soft5_sent")),
                "soft10_sent": bool(meta.get("soft10_sent")),
                "task": task,
            }
        )
    payload = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "reason": reason,
        "pending_count": len(PENDING),
        "running_count": len(RUNNING),
        "pending": pending_rows,
        "running": running_rows,
    }
    try:
        _atomic_write_text(QUEUE_SNAPSHOT_PATH, json.dumps(payload, ensure_ascii=False, indent=2))
    except Exception:
        pass

def restore_pending_from_snapshot(max_age_sec: int = 900) -> int:
    if PENDING:
        return 0
    try:
        if not QUEUE_SNAPSHOT_PATH.exists():
            return 0
        snap = json.loads(QUEUE_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        if not isinstance(snap, dict):
            return 0
        ts = str(snap.get("ts") or "")
        ts_unix = parse_iso_to_ts(ts)
        if ts_unix is None:
            return 0
        if (time.time() - ts_unix) > max_age_sec:
            return 0
        restored = 0
        for row in (snap.get("pending") or []):
            task = row.get("task") if isinstance(row, dict) else None
            if not isinstance(task, dict):
                continue
            if not task.get("id") or not task.get("chat_id"):
                continue
            enqueue_task(task)
            restored += 1
        if restored > 0:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "queue_restored_from_snapshot",
                    "restored_pending": restored,
                },
            )
            persist_queue_snapshot(reason="queue_restored")
        return restored
    except Exception:
        return 0

def worker_main(wid: int, in_q: Any, out_q: Any, repo_dir: str, drive_root: str) -> None:
    import sys as _sys
    _sys.path.insert(0, repo_dir)
    from ouroboros.agent import make_agent  # type: ignore
    agent = make_agent(repo_dir=repo_dir, drive_root=drive_root, event_queue=out_q)
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
    cleared_running = len(RUNNING)
    for w in WORKERS.values():
        if w.proc.is_alive():
            w.proc.terminate()
    for w in WORKERS.values():
        w.proc.join(timeout=5)
    WORKERS.clear()
    RUNNING.clear()
    persist_queue_snapshot(reason="kill_workers")
    if cleared_running:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "running_cleared_on_kill",
                "count": cleared_running,
            },
        )

def assign_tasks() -> None:
    for w in WORKERS.values():
        if w.busy_task_id is None and PENDING:
            _sort_pending()
            task = PENDING.pop(0)
            w.busy_task_id = task["id"]
            w.in_q.put(task)
            now_ts = time.time()
            RUNNING[task["id"]] = {
                "task": dict(task),
                "worker_id": w.wid,
                "started_at": now_ts,
                "last_heartbeat_at": now_ts,
                "soft5_sent": False,
                "soft10_sent": False,
                "attempt": int(task.get("_attempt") or 1),
            }
            st = load_state()
            if st.get("owner_chat_id"):
                pr = int(task.get("priority") or _task_priority(str(task.get("type") or "")))
                send_with_budget(
                    int(st["owner_chat_id"]),
                    (
                        f"▶️ Стартую задачу {task['id']} (worker {w.wid}, type={task.get('type')}, "
                        f"priority={pr}, attempt={int(task.get('_attempt') or 1)})"
                    ),
                )
            persist_queue_snapshot(reason="assign_task")

def update_budget_from_usage(usage: Dict[str, Any]) -> None:
    def _to_float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except Exception:
            return default

    def _to_int(v: Any, default: int = 0) -> int:
        try:
            return int(v)
        except Exception:
            return default

    st = load_state()
    cost = usage.get("cost") if isinstance(usage, dict) else None
    if cost is None:
        cost = 0.0
    st["spent_usd"] = _to_float(st.get("spent_usd") or 0.0) + _to_float(cost)
    st["spent_calls"] = int(st.get("spent_calls") or 0) + 1
    st["spent_tokens_prompt"] = _to_int(st.get("spent_tokens_prompt") or 0) + _to_int(usage.get("prompt_tokens") if isinstance(usage, dict) else 0)
    st["spent_tokens_completion"] = _to_int(st.get("spent_tokens_completion") or 0) + _to_int(usage.get("completion_tokens") if isinstance(usage, dict) else 0)
    save_state(st)

def parse_iso_to_ts(iso_ts: str) -> Optional[float]:
    txt = str(iso_ts or "").strip()
    if not txt:
        return None
    try:
        return datetime.datetime.fromisoformat(txt.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None

def budget_pct(st: Dict[str, Any]) -> float:
    spent = float(st.get("spent_usd") or 0.0)
    total = float(TOTAL_BUDGET_LIMIT or 0.0)
    if total <= 0:
        return 0.0
    return (spent / total) * 100.0

def _load_evolution_prompt_text() -> str:
    p = REPO_DIR / "prompts" / "evolution.md"
    try:
        txt = p.read_text(encoding="utf-8").strip()
        if txt:
            return txt
    except Exception:
        pass
    return (
        "Endless evolution mode is active.\n"
        "- Do one high-impact self-improvement step per cycle.\n"
        "- Use LLM-based reasoning over hardcoded response templates.\n"
        "- Commit+push only to branch ouroboros, then request_restart.\n"
        "- Report: done/result/next.\n"
    )

def build_evolution_task_text(cycle: int) -> str:
    return (
        f"ENDLESS EVOLUTION CYCLE #{cycle}\n\n"
        "Mode is active until owner asks to stop.\n"
        "Start with `repo_read('prompts/evolution.md')` and follow it exactly.\n"
        "Before choosing the next change, check latest deep-review findings in chat/logs/scratchpad and adjust plan.\n"
        "Do one high-leverage self-improvement step now.\n"
        "Strict branch rule: only `ouroboros` for any write/commit/push; never touch `main` or `ouroboros-stable`.\n"
        "After changes: verify, commit+push, request_restart, then report concise Done/Result/Next.\n\n"
        "Prompt snapshot:\n"
        + _load_evolution_prompt_text()
    )

def build_review_task_text(reason: str, source_task_id: str = "", source_text: str = "") -> str:
    hint = str(source_text or "").strip()
    if len(hint) > 800:
        hint = hint[:800].rstrip() + "..."
    return (
        "SYSTEM REVIEW TASK\n\n"
        "Run deep full-system review in fit-or-chunk mode.\n"
        "Include repository files, prompts, state, memory, logs, and runtime context.\n"
        "Before analysis: estimate input tokens and report the estimate.\n"
        "After analysis: report total review cost and key risks.\n"
        "Output sections:\n"
        "1) Health verdict\n"
        "2) Major problems\n"
        "3) Drift/hanging risks\n"
        "4) Action plan\n"
        "5) Optional immediate follow-ups\n\n"
        f"Reason: {reason or 'unspecified'}\n"
        f"Source task id: {source_task_id or '-'}\n"
        f"Source text: {hint or '-'}\n"
    )

def _is_review_request_text(text: str) -> bool:
    s = str(text or "").strip().lower()
    if not s:
        return False
    phrases = (
        "сделай ревью",
        "сделай review",
        "проведи ревью",
        "прогони ревью",
        "проверь всё ли ок",
        "проверь все ли ок",
        "аудит системы",
        "system review",
        "deep review",
        "health check",
        "review всей системы",
    )
    return any(p in s for p in phrases)

def queue_review_task(
    reason: str,
    source_task_id: str = "",
    source_text: str = "",
    force: bool = False,
    notify: bool = True,
) -> Optional[str]:
    st = load_state()
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return None
    if (not force) and _queue_has_task_type("review"):
        return None

    tid = uuid.uuid4().hex[:8]
    queued = enqueue_task(
        {
            "id": tid,
            "type": "review",
            "chat_id": int(owner_chat_id),
            "text": build_review_task_text(reason=reason, source_task_id=source_task_id, source_text=source_text),
            "review_reason": reason,
            "review_source_task_id": source_task_id,
        }
    )
    st["last_review_task_id"] = tid
    st["last_auto_review_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_state(st)

    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": "review_task_enqueued",
            "task_id": tid,
            "reason": reason,
            "source_task_id": source_task_id,
            "priority": queued.get("priority"),
            "force": bool(force),
        },
    )
    persist_queue_snapshot(reason="review_enqueued")
    if notify:
        send_with_budget(
            int(owner_chat_id),
            (
                f"🔎 Review queued: {tid}\n"
                f"reason={reason or '-'}; source_task_id={source_task_id or '-'}; "
                f"priority={queued.get('priority')}"
            ),
        )
    return tid

def enqueue_evolution_task_if_needed() -> None:
    global LAST_EVOLUTION_SKIP_SIGNATURE
    if PENDING or RUNNING:
        st = load_state()
        if bool(st.get("evolution_mode_enabled")):
            sig = (len(PENDING), len(RUNNING))
            if LAST_EVOLUTION_SKIP_SIGNATURE != sig:
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "evolution_enqueue_skipped",
                        "reason": "pending_or_running",
                        "pending": len(PENDING),
                        "running": len(RUNNING),
                    },
                )
                LAST_EVOLUTION_SKIP_SIGNATURE = sig
        return
    LAST_EVOLUTION_SKIP_SIGNATURE = None

    st = load_state()
    if not bool(st.get("evolution_mode_enabled")):
        return

    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    if budget_pct(st) >= 100.0:
        st["evolution_mode_enabled"] = False
        save_state(st)
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "evolution_mode_auto_stopped",
                "reason": "budget_exhausted",
                "budget_pct": budget_pct(st),
            },
        )
        send_with_budget(int(owner_chat_id), "💸 Endless evolution остановлен: бюджет достиг лимита.")
        return

    cycle = int(st.get("evolution_cycle") or 0) + 1
    tid = uuid.uuid4().hex[:8]
    queued = enqueue_task(
        {
            "id": tid,
            "type": "evolution",
            "chat_id": int(owner_chat_id),
            "text": build_evolution_task_text(cycle),
        }
    )

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    st["evolution_cycle"] = cycle
    st["last_evolution_task_at"] = now_iso
    save_state(st)

    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": now_iso,
            "type": "evolution_task_enqueued",
            "task_id": tid,
            "cycle": cycle,
            "budget_pct": budget_pct(st),
            "priority": queued.get("priority"),
        },
    )
    send_with_budget(int(owner_chat_id), f"🧬 Evolution task queued: {tid} (cycle {cycle})")

def idle_task_catalog() -> List[Tuple[str, str]]:
    return [
        (
            "memory_consolidation",
            "Idle internal task: consolidate working memory. Update memory/scratchpad.md from recent logs and add compact evidence quotes.",
        ),
        (
            "performance_analysis",
            "Idle internal task: analyze recent tools/events logs and report key bottlenecks, recurring failures, and optimization opportunities.",
        ),
        (
            "code_improvement_idea",
            "Idle internal task: inspect your own codebase and propose one high-impact improvement with rationale and validation plan.",
        ),
        (
            "web_learning",
            "Idle internal task: use web_search for one focused topic that can improve reliability/efficiency of this system, then summarize practical takeaways.",
        ),
        (
            "owner_idea_proposal",
            "Idle internal task: prepare one concise proactive idea for the owner based on current priorities and unresolved threads.",
        ),
    ]

def enqueue_idle_task_if_needed() -> None:
    if not IDLE_ENABLED:
        return
    if PENDING or RUNNING:
        return

    st = load_state()
    if bool(st.get("evolution_mode_enabled")):
        return
    owner_chat_id = st.get("owner_chat_id")
    if not owner_chat_id:
        return

    now = time.time()
    last_owner_ts = parse_iso_to_ts(str(st.get("last_owner_message_at") or ""))
    if last_owner_ts is not None and (now - last_owner_ts) < IDLE_COOLDOWN_SEC:
        return

    last_idle_ts = parse_iso_to_ts(str(st.get("last_idle_task_at") or ""))
    if last_idle_ts is not None and (now - last_idle_ts) < IDLE_COOLDOWN_SEC:
        return

    if budget_pct(st) >= IDLE_BUDGET_PCT_CAP:
        return

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    idle_stats = st.get("idle_stats") if isinstance(st.get("idle_stats"), dict) else {}
    day_stat = idle_stats.get(today) if isinstance(idle_stats.get(today), dict) else {}
    day_count = int(day_stat.get("count") or 0)
    if day_count >= IDLE_MAX_PER_DAY:
        return

    catalog = idle_task_catalog()
    cursor = int(st.get("idle_cursor") or 0)
    kind, text = catalog[cursor % len(catalog)]
    tid = uuid.uuid4().hex[:8]
    queued = enqueue_task({"id": tid, "type": "idle", "chat_id": int(owner_chat_id), "text": text})

    now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()
    st["idle_cursor"] = cursor + 1
    st["last_idle_task_at"] = now_iso
    idle_stats[today] = {
        "count": day_count + 1,
        "last_task_id": tid,
        "last_kind": kind,
        "last_at": now_iso,
    }
    # Keep recent days only.
    if len(idle_stats) > 14:
        for d in sorted(idle_stats.keys())[:-14]:
            idle_stats.pop(d, None)
    st["idle_stats"] = idle_stats
    save_state(st)

    append_jsonl(
        DRIVE_ROOT / "logs" / "supervisor.jsonl",
        {
            "ts": now_iso,
            "type": "idle_task_enqueued",
            "task_id": tid,
            "kind": kind,
            "budget_pct": budget_pct(st),
            "priority": queued.get("priority"),
        },
    )
    send_with_budget(int(owner_chat_id), f"🧠 Idle task queued: {tid} ({kind})")

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
                meta = RUNNING.pop(w.busy_task_id) or {}
                task = meta.get("task") if isinstance(meta, dict) else None
                if isinstance(task, dict):
                    enqueue_task(task, front=True)
            respawn_worker(wid)
            persist_queue_snapshot(reason="worker_respawn_after_crash")

    now = time.time()
    CRASH_TS[:] = [t for t in CRASH_TS if (now - t) < 60.0]
    # if crash storm, fallback to stable branch (import must work)
    if len(CRASH_TS) >= 3:
        st = load_state()
        if st.get("owner_chat_id"):
            send_with_budget(int(st["owner_chat_id"]), "⚠️ Частые падения воркеров. Переключаюсь на ouroboros-stable и перезапускаюсь.")
        ok_reset, msg_reset = checkout_and_reset(
            BRANCH_STABLE,
            reason="crash_storm_fallback",
            unsynced_policy="rescue_and_reset",
        )
        if not ok_reset:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "crash_storm_reset_blocked",
                    "error": msg_reset,
                },
            )
            if st.get("owner_chat_id"):
                send_with_budget(
                    int(st["owner_chat_id"]),
                    f"⚠️ Fallback reset в {BRANCH_STABLE} пропущен: {msg_reset}",
                )
            CRASH_TS.clear()
            return
        deps_ok, deps_msg = sync_runtime_dependencies(reason="crash_storm_fallback")
        if not deps_ok:
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "crash_storm_deps_sync_failed",
                    "error": deps_msg,
                },
            )
            if st.get("owner_chat_id"):
                send_with_budget(
                    int(st["owner_chat_id"]),
                    f"⚠️ Fallback в {BRANCH_STABLE} применён, но установка зависимостей упала: {deps_msg}",
                )
            CRASH_TS.clear()
            return
        kill_workers()
        spawn_workers(MAX_WORKERS)
        CRASH_TS.clear()

def enforce_task_timeouts() -> None:
    if not RUNNING:
        return
    now = time.time()
    st = load_state()
    owner_chat_id = int(st.get("owner_chat_id") or 0)

    for task_id, meta in list(RUNNING.items()):
        if not isinstance(meta, dict):
            continue
        task = meta.get("task") if isinstance(meta.get("task"), dict) else {}
        started_at = float(meta.get("started_at") or 0.0)
        if started_at <= 0:
            continue
        last_hb = float(meta.get("last_heartbeat_at") or started_at)
        runtime_sec = max(0.0, now - started_at)
        hb_lag_sec = max(0.0, now - last_hb)
        hb_stale = hb_lag_sec >= HEARTBEAT_STALE_SEC
        worker_id = int(meta.get("worker_id") or -1)
        task_type = str(task.get("type") or "")
        attempt = int(meta.get("attempt") or task.get("_attempt") or 1)

        if runtime_sec >= QUEUE_SOFT_TIMEOUT_1_SEC and not bool(meta.get("soft5_sent")):
            meta["soft5_sent"] = True
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "task_soft_timeout",
                    "level": 1,
                    "task_id": task_id,
                    "task_type": task_type,
                    "worker_id": worker_id,
                    "runtime_sec": round(runtime_sec, 2),
                    "heartbeat_lag_sec": round(hb_lag_sec, 2),
                    "heartbeat_stale": hb_stale,
                    "attempt": attempt,
                },
            )
            if owner_chat_id:
                send_with_budget(
                    owner_chat_id,
                    (
                        f"⏱️ Задача {task_id} выполняется {int(runtime_sec)}с (soft-5).\n"
                        f"worker={worker_id}, type={task_type}, attempt={attempt}, "
                        f"heartbeat_lag={int(hb_lag_sec)}с, stale={int(hb_stale)}.\n"
                        "Пока продолжаю выполнение и наблюдение."
                    ),
                )

        if runtime_sec >= QUEUE_SOFT_TIMEOUT_2_SEC and not bool(meta.get("soft10_sent")):
            meta["soft10_sent"] = True
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "task_soft_timeout",
                    "level": 2,
                    "task_id": task_id,
                    "task_type": task_type,
                    "worker_id": worker_id,
                    "runtime_sec": round(runtime_sec, 2),
                    "heartbeat_lag_sec": round(hb_lag_sec, 2),
                    "heartbeat_stale": hb_stale,
                    "attempt": attempt,
                },
            )
            if owner_chat_id:
                send_with_budget(
                    owner_chat_id,
                    (
                        f"⏱️ Задача {task_id} выполняется {int(runtime_sec)}с (soft-10).\n"
                        f"worker={worker_id}, type={task_type}, attempt={attempt}, "
                        f"heartbeat_lag={int(hb_lag_sec)}с, stale={int(hb_stale)}.\n"
                        "Готовлюсь к принудительному restart worker на hard-timeout."
                    ),
                )

        if runtime_sec < QUEUE_HARD_TIMEOUT_SEC:
            continue

        # Hard timeout: force-kill worker, optionally requeue with bounded retries.
        RUNNING.pop(task_id, None)
        if worker_id in WORKERS and WORKERS[worker_id].busy_task_id == task_id:
            WORKERS[worker_id].busy_task_id = None

        if worker_id in WORKERS:
            w = WORKERS[worker_id]
            try:
                if w.proc.is_alive():
                    w.proc.terminate()
                w.proc.join(timeout=5)
            except Exception:
                pass
            respawn_worker(worker_id)

        requeued = False
        new_attempt = attempt
        if attempt <= QUEUE_MAX_RETRIES and isinstance(task, dict):
            retried = dict(task)
            retried["_attempt"] = attempt + 1
            retried["timeout_retry_from"] = task_id
            retried["timeout_retry_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            enqueue_task(retried, front=True)
            requeued = True
            new_attempt = attempt + 1

        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "task_hard_timeout",
                "task_id": task_id,
                "task_type": task_type,
                "worker_id": worker_id,
                "runtime_sec": round(runtime_sec, 2),
                "heartbeat_lag_sec": round(hb_lag_sec, 2),
                "heartbeat_stale": hb_stale,
                "attempt": attempt,
                "requeued": requeued,
                "new_attempt": new_attempt,
                "max_retries": QUEUE_MAX_RETRIES,
            },
        )

        if owner_chat_id:
            if requeued:
                send_with_budget(
                    owner_chat_id,
                    (
                        f"🛑 Hard-timeout: задача {task_id} убита после {int(runtime_sec)}с.\n"
                        f"Worker {worker_id} перезапущен. Задача поставлена на retry attempt={new_attempt}."
                    ),
                )
            else:
                send_with_budget(
                    owner_chat_id,
                    (
                        f"🛑 Hard-timeout: задача {task_id} убита после {int(runtime_sec)}с.\n"
                        f"Worker {worker_id} перезапущен. Лимит retry исчерпан, задача остановлена."
                    ),
                )

        persist_queue_snapshot(reason="task_hard_timeout")

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
    now = time.time()
    lines = []
    lines.append(f"owner_id: {st.get('owner_id')}")
    lines.append(f"session_id: {st.get('session_id')}")
    lines.append(f"version: {st.get('current_branch')}@{(st.get('current_sha') or '')[:8]}")
    busy_count = sum(1 for w in WORKERS.values() if w.busy_task_id is not None)
    lines.append(f"workers: {len(WORKERS)} (busy: {busy_count})")
    lines.append(f"pending: {len(PENDING)}")
    lines.append(f"running: {len(RUNNING)}")
    if PENDING:
        preview = []
        for t in PENDING[:10]:
            preview.append(
                f"{t.get('id')}:{t.get('type')}:pr{t.get('priority')}:a{int(t.get('_attempt') or 1)}"
            )
        lines.append("pending_queue: " + ", ".join(preview))
    if RUNNING:
        lines.append("running_ids: " + ", ".join(list(RUNNING.keys())[:10]))
    busy = [f"{w.wid}:{w.busy_task_id}" for w in WORKERS.values() if w.busy_task_id]
    if busy:
        lines.append("busy: " + ", ".join(busy))
    if RUNNING:
        details: List[str] = []
        for task_id, meta in list(RUNNING.items())[:10]:
            task = meta.get("task") if isinstance(meta, dict) else {}
            started = float(meta.get("started_at") or 0.0) if isinstance(meta, dict) else 0.0
            hb = float(meta.get("last_heartbeat_at") or 0.0) if isinstance(meta, dict) else 0.0
            runtime_sec = int(max(0.0, now - started)) if started > 0 else 0
            hb_lag_sec = int(max(0.0, now - hb)) if hb > 0 else -1
            details.append(
                (
                    f"{task_id}:type={task.get('type')} pr={task.get('priority')} "
                    f"attempt={meta.get('attempt')} runtime={runtime_sec}s hb_lag={hb_lag_sec}s"
                )
            )
        if details:
            lines.append("running_details:")
            lines.extend([f"  - {d}" for d in details])
    if RUNNING and busy_count == 0:
        lines.append("queue_warning: running>0 while busy=0")
    lines.append(f"spent_usd: {st.get('spent_usd')}")
    lines.append(f"spent_calls: {st.get('spent_calls')}")
    lines.append(f"prompt_tokens: {st.get('spent_tokens_prompt')}, completion_tokens: {st.get('spent_tokens_completion')}")
    lines.append(f"budget_report_every_messages: {BUDGET_REPORT_EVERY_MESSAGES}")
    lines.append(
        "idle: "
        + f"enabled={int(IDLE_ENABLED)}, cooldown_sec={IDLE_COOLDOWN_SEC}, "
        + f"budget_cap_pct={IDLE_BUDGET_PCT_CAP:.1f}, max_per_day={IDLE_MAX_PER_DAY}"
    )
    lines.append(
        "evolution: "
        + f"enabled={int(bool(st.get('evolution_mode_enabled')))}, "
        + f"cycle={int(st.get('evolution_cycle') or 0)}"
    )
    lines.append(f"last_owner_message_at: {st.get('last_owner_message_at') or '-'}")
    lines.append(f"last_idle_task_at: {st.get('last_idle_task_at') or '-'}")
    lines.append(f"last_evolution_task_at: {st.get('last_evolution_task_at') or '-'}")
    lines.append(
        "timeouts: "
        + f"soft1={QUEUE_SOFT_TIMEOUT_1_SEC}s, soft2={QUEUE_SOFT_TIMEOUT_2_SEC}s, "
        + f"hard={QUEUE_HARD_TIMEOUT_SEC}s, max_retries={QUEUE_MAX_RETRIES}, hb_stale={HEARTBEAT_STALE_SEC}s"
    )
    lines.append(f"queue_priority_counts_running: {json.dumps(_running_task_type_counts(), ensure_ascii=False)}")
    return "\n".join(lines)

def cancel_task_by_id(task_id: str) -> bool:
    for i, t in enumerate(list(PENDING)):
        if t["id"] == task_id:
            PENDING.pop(i)
            persist_queue_snapshot(reason="cancel_pending")
            return True
    for w in WORKERS.values():
        if w.busy_task_id == task_id:
            RUNNING.pop(task_id, None)
            if w.proc.is_alive():
                w.proc.terminate()
            w.proc.join(timeout=5)
            respawn_worker(w.wid)
            persist_queue_snapshot(reason="cancel_running")
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

    # Execute approved actions
    if cmd == "/approve" and approvals[approval_id].get("type") == "stable_promotion":
        try:
            subprocess.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)
            subprocess.run(["git", "push", "origin", f"{BRANCH_DEV}:{BRANCH_STABLE}"], cwd=str(REPO_DIR), check=True)
            new_sha = subprocess.run(["git", "rev-parse", f"origin/{BRANCH_STABLE}"], cwd=str(REPO_DIR), capture_output=True, text=True, check=True).stdout.strip()
            send_with_budget(chat_id, f"✅ Промоут выполнен: {BRANCH_DEV} → {BRANCH_STABLE} ({new_sha[:8]})")
        except Exception as e:
            send_with_budget(chat_id, f"❌ Ошибка промоута в stable: {e}")

    if cmd == "/approve" and approvals[approval_id].get("type") == "reindex":
        reason = str(approvals[approval_id].get("reason") or "").strip()
        tid = uuid.uuid4().hex[:8]
        enqueue_task(
            {
                "id": tid,
                "type": "task",
                "chat_id": chat_id,
                "text": (
                    "Approved internal task: run full reindex of drive/index/summaries.json. "
                    "Rebuild summaries carefully, report what changed, and include validation checks. "
                    f"Reason: {reason}"
                ).strip(),
            }
        )
        persist_queue_snapshot(reason="reindex_approved")
        send_with_budget(chat_id, f"✅ Reindex approval accepted. Queued task {tid}.")

    return True

# start
kill_workers()
spawn_workers(MAX_WORKERS)
restored_pending = restore_pending_from_snapshot()
persist_queue_snapshot(reason="startup")
if restored_pending > 0:
    st_boot = load_state()
    if st_boot.get("owner_chat_id"):
        send_with_budget(
            int(st_boot["owner_chat_id"]),
            f"♻️ Восстановил pending queue из snapshot: {restored_pending} задач.",
        )

append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
    "idle_enabled": IDLE_ENABLED,
    "idle_cooldown_sec": IDLE_COOLDOWN_SEC,
    "idle_budget_pct_cap": IDLE_BUDGET_PCT_CAP,
    "idle_max_per_day": IDLE_MAX_PER_DAY,
    "evolution_enabled_by_default": int(EVOLUTION_ENABLED_BY_DEFAULT),
    "budget_report_every_messages": BUDGET_REPORT_EVERY_MESSAGES,
    "model_default": MODEL_MAIN,
    "model_code": MODEL_CODE,
    "model_review": MODEL_REVIEW,
    "model_router": MODEL_ROUTER,
    "router_reasoning_effort": ROUTER_REASONING_EFFORT,
    "reasoning_default_task": REASONING_DEFAULT_TASK,
    "reasoning_code_task": REASONING_CODE_TASK,
    "reasoning_evolution_task": REASONING_EVOLUTION_TASK,
    "reasoning_deep_review": REASONING_DEEP_REVIEW,
    "reasoning_memory_summary": REASONING_MEMORY_SUMMARY,
    "task_soft_timeout_1_sec": QUEUE_SOFT_TIMEOUT_1_SEC,
    "task_soft_timeout_2_sec": QUEUE_SOFT_TIMEOUT_2_SEC,
    "task_hard_timeout_sec": QUEUE_HARD_TIMEOUT_SEC,
    "task_max_retries": QUEUE_MAX_RETRIES,
    "task_heartbeat_sec": TASK_HEARTBEAT_SEC,
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

        if et == "task_heartbeat":
            task_id = str(evt.get("task_id") or "")
            if task_id and task_id in RUNNING:
                meta = RUNNING.get(task_id) or {}
                meta["last_heartbeat_at"] = time.time()
                phase = str(evt.get("phase") or "")
                if phase:
                    meta["heartbeat_phase"] = phase
                RUNNING[task_id] = meta
            continue

        if et == "send_message":
            try:
                _log_text = evt.get("log_text")
                send_with_budget(
                    int(evt["chat_id"]),
                    str(evt.get("text") or ""),
                    log_text=(str(_log_text) if isinstance(_log_text, str) else None),
                )
            except Exception as e:
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "send_message_event_error",
                        "error": repr(e),
                    },
                )
            continue

        if et == "task_done":
            task_id = evt.get("task_id")
            wid = evt.get("worker_id")
            done_meta: Dict[str, Any] = {}
            task_type = ""
            task_text = ""
            if task_id:
                done_meta = RUNNING.pop(str(task_id), None) or {}
                done_task = done_meta.get("task") if isinstance(done_meta, dict) else {}
                if isinstance(done_task, dict):
                    task_type = str(done_task.get("type") or "")
                    task_text = str(done_task.get("text") or "")
            if wid in WORKERS and WORKERS[wid].busy_task_id == task_id:
                WORKERS[wid].busy_task_id = None
            persist_queue_snapshot(reason="task_done")

            if task_type == "evolution":
                queue_review_task(
                    reason="post_evolution_cycle",
                    source_task_id=str(task_id or ""),
                    source_text=task_text,
                    force=False,
                    notify=True,
                )
            continue

        if et == "task_metrics":
            task_id = str(evt.get("task_id") or "")
            task_type = str(evt.get("task_type") or "")
            duration_sec = float(evt.get("duration_sec") or 0.0)
            tool_calls = int(evt.get("tool_calls") or 0)
            tool_errors = int(evt.get("tool_errors") or 0)
            complexity_trigger_review = bool(evt.get("complexity_trigger_review"))
            reason = str(evt.get("complexity_reason") or "").strip()
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "task_metrics_event",
                    "task_id": task_id,
                    "task_type": task_type,
                    "duration_sec": round(duration_sec, 3),
                    "tool_calls": tool_calls,
                    "tool_errors": tool_errors,
                    "complexity_trigger_review": complexity_trigger_review,
                    "complexity_reason": reason,
                },
            )
            if complexity_trigger_review and task_type not in ("review", "evolution"):
                st2 = load_state()
                last_auto = parse_iso_to_ts(str(st2.get("last_auto_review_at") or ""))
                now_ts = time.time()
                if (last_auto is None) or ((now_ts - last_auto) >= AUTO_REVIEW_MIN_GAP_SEC):
                    queue_review_task(
                        reason=("complex_task:" + (reason or "auto")),
                        source_task_id=task_id,
                        source_text=str(evt.get("task_text") or ""),
                        force=False,
                        notify=True,
                    )
            continue

        if et == "review_request":
            queue_review_task(
                reason=str(evt.get("reason") or "agent_review_request"),
                source_task_id=str(evt.get("source_task_id") or ""),
                source_text=str(evt.get("source_text") or ""),
                force=False,
                notify=True,
            )
            continue

        if et == "restart_request":
            st = load_state()
            if st.get("owner_chat_id"):
                send_with_budget(int(st["owner_chat_id"]), f"♻️ Restart requested by agent: {evt.get('reason')}")
            ok_reset, msg_reset = checkout_and_reset(
                BRANCH_DEV,
                reason="agent_restart_request",
                unsynced_policy="rescue_and_block",
            )
            if not ok_reset:
                if st.get("owner_chat_id"):
                    send_with_budget(
                        int(st["owner_chat_id"]),
                        f"⚠️ Restart пропущен: {msg_reset} Сначала закоммить/запушь изменения или очисти repo.",
                    )
                continue
            deps_ok, deps_msg = sync_runtime_dependencies(reason="agent_restart_request")
            if not deps_ok:
                if st.get("owner_chat_id"):
                    send_with_budget(
                        int(st["owner_chat_id"]),
                        f"⚠️ Restart отменен: не удалось установить зависимости ({deps_msg}).",
                    )
                continue
            it = import_test()
            if not it["ok"]:
                ok_stable, msg_stable = checkout_and_reset(
                    BRANCH_STABLE,
                    reason="agent_restart_import_fail",
                    unsynced_policy="rescue_and_reset",
                )
                if not ok_stable:
                    if st.get("owner_chat_id"):
                        send_with_budget(
                            int(st["owner_chat_id"]),
                            f"⚠️ Не удалось переключиться на {BRANCH_STABLE}: {msg_stable}",
                        )
                    continue
                deps_ok_stable, deps_msg_stable = sync_runtime_dependencies(reason="agent_restart_import_fail_stable")
                if not deps_ok_stable:
                    if st.get("owner_chat_id"):
                        send_with_budget(
                            int(st["owner_chat_id"]),
                            f"⚠️ Не удалось установить зависимости в {BRANCH_STABLE}: {deps_msg_stable}",
                        )
                    continue
            kill_workers()
            spawn_workers(MAX_WORKERS)
            continue

        if et == "stable_promotion_request":
            approval_id = uuid.uuid4().hex[:8]
            st = load_state()
            approvals = st.get("approvals") or {}
            approvals[approval_id] = {
                "type": "stable_promotion",
                "reason": evt.get("reason", ""),
                "status": "pending",
                "requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            st["approvals"] = approvals
            save_state(st)
            if st.get("owner_chat_id"):
                send_with_budget(
                    int(st["owner_chat_id"]),
                    f"🔄 Запрос на промоут в stable:\n{evt.get('reason', '')}\n\n"
                    f"Подтвердить: /approve {approval_id}\n"
                    f"Отклонить: /deny {approval_id}"
                )
            continue

        if et == "schedule_task":
            st = load_state()
            owner_chat_id = st.get("owner_chat_id")
            desc = str(evt.get("description") or "").strip()
            if owner_chat_id and desc:
                tid = uuid.uuid4().hex[:8]
                enqueue_task(
                    {
                        "id": tid,
                        "type": "task",
                        "chat_id": int(owner_chat_id),
                        "text": desc,
                    }
                )
                send_with_budget(int(owner_chat_id), f"🗓️ Запланировал задачу {tid}: {desc}")
                persist_queue_snapshot(reason="schedule_task_event")
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "schedule_task_event",
                    "description": desc,
                },
            )
            continue

        if et == "cancel_task":
            task_id = str(evt.get("task_id") or "").strip()
            st = load_state()
            owner_chat_id = st.get("owner_chat_id")
            ok = cancel_task_by_id(task_id) if task_id else False
            if owner_chat_id:
                send_with_budget(int(owner_chat_id), f"{'✅' if ok else '❌'} cancel {task_id or '?'} (event)")
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "cancel_task_event",
                    "task_id": task_id,
                    "ok": ok,
                },
            )
            continue

        if et == "reindex_request":
            approval_id = uuid.uuid4().hex[:8]
            st = load_state()
            approvals = st.get("approvals") or {}
            approvals[approval_id] = {
                "type": "reindex",
                "reason": evt.get("reason", ""),
                "status": "pending",
                "requested_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            st["approvals"] = approvals
            save_state(st)
            if st.get("owner_chat_id"):
                send_with_budget(
                    int(st["owner_chat_id"]),
                    f"🗂️ Запрос на полную реиндексацию:\n{evt.get('reason', '')}\n\n"
                    f"Подтвердить: /approve {approval_id}\n"
                    f"Отклонить: /deny {approval_id}",
                )
            continue

    enforce_task_timeouts()
    enqueue_evolution_task_if_needed()
    enqueue_idle_task_if_needed()
    assign_tasks()
    persist_queue_snapshot(reason="main_loop")

    # Poll Telegram
    try:
        updates = TG.get_updates(offset=offset, timeout=10)
    except Exception as e:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "telegram_poll_error",
                "offset": offset,
                "error": repr(e),
            },
        )
        time.sleep(1.5)
        continue
    for upd in updates:
        offset = int(upd["update_id"]) + 1
        msg = upd.get("message") or upd.get("edited_message") or {}
        if not msg:
            continue

        chat_id = int(msg["chat"]["id"])
        from_user = msg.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        text = str(msg.get("text") or "")
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        st = load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id
            st["last_owner_message_at"] = now_iso
            save_state(st)
            log_chat("in", chat_id, user_id, text)
            send_with_budget(chat_id, "✅ Owner registered. Ouroboros online.")
            continue

        if user_id != int(st.get("owner_id")):
            continue

        log_chat("in", chat_id, user_id, text)
        st["last_owner_message_at"] = now_iso
        save_state(st)

        # core supervisor commands
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
            ok_reset, msg_reset = checkout_and_reset(
                BRANCH_DEV,
                reason="owner_restart",
                unsynced_policy="rescue_and_block",
            )
            if not ok_reset:
                send_with_budget(chat_id, f"⚠️ Restart отменен: {msg_reset} Сначала закоммить/запушь изменения или очисти repo.")
                continue
            deps_ok, deps_msg = sync_runtime_dependencies(reason="owner_restart")
            if not deps_ok:
                send_with_budget(chat_id, f"⚠️ Restart отменен: не удалось установить зависимости ({deps_msg}).")
                continue
            it = import_test()
            if not it["ok"]:
                ok_stable, msg_stable = checkout_and_reset(
                    BRANCH_STABLE,
                    reason="owner_restart_import_fail",
                    unsynced_policy="rescue_and_reset",
                )
                if not ok_stable:
                    send_with_budget(chat_id, f"⚠️ Не удалось переключиться на {BRANCH_STABLE}: {msg_stable}")
                    continue
                deps_ok_stable, deps_msg_stable = sync_runtime_dependencies(reason="owner_restart_import_fail_stable")
                if not deps_ok_stable:
                    send_with_budget(chat_id, f"⚠️ Не удалось установить зависимости в {BRANCH_STABLE}: {deps_msg_stable}")
                    continue
            kill_workers()
            spawn_workers(MAX_WORKERS)
            continue

        if text.strip().lower().startswith("/status"):
            send_with_budget(chat_id, status_text(), force_budget=True)
            continue

        if text.strip().lower().startswith("/review"):
            queued_id = queue_review_task(
                reason="owner_command:/review",
                source_task_id="",
                source_text=text,
                force=True,
                notify=True,
            )
            if queued_id is None:
                send_with_budget(chat_id, "⚠️ Не удалось поставить review в очередь (owner_chat_id не установлен).")
            continue

        lowered = text.strip().lower()
        if lowered.startswith("/evolve"):
            parts = lowered.split()
            action = parts[1] if len(parts) > 1 else "on"
            turn_on = action not in ("off", "stop", "0")

            st2 = load_state()
            st2["evolution_mode_enabled"] = bool(turn_on)
            save_state(st2)

            removed_pending = 0
            if not turn_on:
                before = len(PENDING)
                PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
                _sort_pending()
                removed_pending = before - len(PENDING)
                persist_queue_snapshot(reason="evolve_off_remove_pending")

            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "evolution_mode_toggle",
                    "enabled": bool(turn_on),
                    "removed_pending": removed_pending,
                    "source_text": text,
                },
            )

            if turn_on:
                send_with_budget(
                    chat_id,
                    "🧬 Endless evolution: ON.\n"
                    "Буду крутить self-improvement циклы до твоей остановки или конца бюджета.\n"
                    "Отключить: /evolve stop",
                )
            else:
                send_with_budget(
                    chat_id,
                    f"🛑 Endless evolution: OFF. Снято pending evolution tasks: {removed_pending}.",
                )
            continue

        if lowered in ("останови эволюцию", "остановить эволюцию", "стоп эволюции", "stop evolution"):
            st2 = load_state()
            st2["evolution_mode_enabled"] = False
            save_state(st2)
            before = len(PENDING)
            PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
            _sort_pending()
            removed_pending = before - len(PENDING)
            persist_queue_snapshot(reason="evolution_stop_natural")
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "evolution_mode_toggle",
                    "enabled": False,
                    "removed_pending": removed_pending,
                    "source_text": text,
                },
            )
            send_with_budget(
                chat_id,
                f"🛑 Endless evolution остановлен. Снято pending evolution tasks: {removed_pending}.",
            )
            continue

        if handle_approval(chat_id, text):
            continue

        if _is_review_request_text(text):
            queued_id = queue_review_task(
                reason="owner_natural_review_request",
                source_task_id="",
                source_text=text,
                force=False,
                notify=True,
            )
            if queued_id is None:
                send_with_budget(chat_id, "ℹ️ Review уже в очереди или выполняется.")
            continue

        if text.strip().lower().startswith("/cancel"):
            parts = text.strip().split()
            assert len(parts) >= 2, "Usage: /cancel <task_id>"
            ok = cancel_task_by_id(parts[1])
            send_with_budget(chat_id, f"{'✅' if ok else '❌'} cancel {parts[1]}")
            continue

        # Route: direct answer or full agent task
        direct = route_and_maybe_answer(text)
        if direct is not None:
            send_with_budget(chat_id, direct)
        else:
            tid = uuid.uuid4().hex[:8]
            queued = enqueue_task({"id": tid, "type": "task", "chat_id": chat_id, "text": text})
            persist_queue_snapshot(reason="owner_task_enqueued")
            send_with_budget(
                chat_id,
                (
                    f"🧾 Принято. В очереди: {tid}. "
                    f"(workers={MAX_WORKERS}, pending={len(PENDING)}, priority={queued.get('priority')})"
                ),
            )

    st = load_state()
    st["tg_offset"] = offset
    save_state(st)

    time.sleep(0.2)
