"""
Ouroboros Dashboard Tool — pushes live data to ouroboros-webapp for the web dashboard.

Collects state, budget, chat history, knowledge base, timeline from Drive,
compiles into data.json, and pushes to GitHub via API.
"""

import json
import os
import base64
import time
import logging
import re
import importlib.util
import subprocess
from pathlib import Path
from typing import List

import requests

from ouroboros.tools.registry import ToolEntry, ToolContext
from ouroboros.memory import Memory
from ouroboros.utils import short

log = logging.getLogger(__name__)

WEBAPP_REPO = "razzant/ouroboros-webapp"
DATA_PATH = "data.json"


def _get_timeline():
    """Build evolution timeline from known milestones."""
    return [
        {"version": "6.1.0", "time": "2026-02-18", "event": "Budget Controls: selective tools, soft limits, compact_context", "type": "feature"},
        {"version": "6.0.0", "time": "2026-02-18", "event": "Major Refactor: single-consumer routing, per-task mailbox", "type": "milestone"},
        {"version": "5.2.2", "time": "2026-02-18", "event": "Evolution Time-Lapse", "type": "milestone"},
        {"version": "5.2.1", "time": "2026-02-18", "event": "Self-Portrait", "type": "feature"},
        {"version": "5.2.0", "time": "2026-02-18", "event": "Constitutional Hardening", "type": "milestone"},
        {"version": "5.1.3", "time": "2026-02-18", "event": "Message Dispatch Fix", "type": "fix"},
        {"version": "4.24.0", "time": "2026-02-17", "event": "Deep Review Bugfixes", "type": "fix"},
        {"version": "4.22.0", "time": "2026-02-17", "event": "Empty Response Resilience", "type": "feature"},
        {"version": "4.21.0", "time": "2026-02-17", "event": "Web Presence & Budget Categories", "type": "milestone"},
        {"version": "4.18.0", "time": "2026-02-17", "event": "GitHub Issues Integration", "type": "feature"},
        {"version": "4.15.0", "time": "2026-02-17", "event": "79 Smoke Tests + Pre-push Gate", "type": "feature"},
        {"version": "4.14.0", "time": "2026-02-17", "event": "3-Block Prompt Caching", "type": "feature"},
        {"version": "4.8.0", "time": "2026-02-16", "event": "Consciousness Loop Online", "type": "milestone"},
        {"version": "4.0.0", "time": "2026-02-16", "event": "Ouroboros Genesis", "type": "birth"},
    ]


def _read_jsonl_tail(drive_root, log_name: str, n: int = 30) -> list:
    """Read last n lines of a JSONL log file via Memory (single source of truth)."""
    mem = Memory(drive_root=drive_root)
    return mem.read_jsonl_tail(log_name, max_entries=n)


def _count_tests(repo: Path) -> int:
    """Count smoke tests via pytest discovery; fallback to regex."""
    tests_dir = repo / "tests"
    if not tests_dir.is_dir():
        return 0
    try:
        result = subprocess.run(
            ["python", "-m", "pytest", "tests/", "-q", "--co"],
            capture_output=True, text=True, timeout=30,
            cwd=str(repo),
        )
        if result.returncode == 0:
            return len([l for l in result.stdout.strip().split("\n") if "::test_" in l])
    except Exception:
        pass
    # Fallback to regex
    count = 0
    for tf in tests_dir.glob("test_*.py"):
        count += len(re.findall(r"^\s*def test_", tf.read_text(), re.MULTILINE))
    return count


def _count_tools(repo: Path) -> int:
    """Count registered tools by importing each tools module and calling get_tools()."""
    tools_dir = repo / "ouroboros" / "tools"
    if not tools_dir.is_dir():
        return 0
    count = 0
    for fn in sorted(os.listdir(str(tools_dir))):
        if fn.endswith(".py") and not fn.startswith("_") and fn != "registry.py":
            spec = importlib.util.spec_from_file_location("_td_" + fn[:-3], tools_dir / fn)
            mod = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(mod)
                if hasattr(mod, "get_tools"):
                    count += len(mod.get_tools())
            except Exception:
                pass
    return count


def _build_timeline(repo: Path) -> list:
    """Return the evolution timeline (delegates to _get_timeline)."""
    return _get_timeline()


def _recent_activity(drive: Path, limit: int = 15) -> list:
    """Build recent activity list from the last 50 events (skipping noisy types)."""
    events = _read_jsonl_tail(drive, "events.jsonl", 5000)
    activity = []
    for e in reversed(events[-50:]):
        ev = e.get("type", "")
        if ev in ("llm_usage", "llm_round", "task_eval"):
            continue
        icon = "📡"
        text = ev
        e_type = "info"
        if ev == "task_done":
            icon = "✅"
            text = "Task completed"
            e_type = "success"
        elif ev == "task_received":
            icon = "📥"
            text = f"Task received: {short(e.get('type', ''), 20)}"
            e_type = "info"
        elif "evolution" in ev:
            icon = "🧬"
            text = f"Evolution: {ev}"
            e_type = "evolution"
        elif ev == "llm_empty_response":
            icon = "⚠️"
            text = "Empty model response"
            e_type = "warning"
        elif ev == "startup_verification":
            icon = "🔍"
            text = "Startup verification"
            e_type = "info"
        ts = e.get("ts", "")
        activity.append({
            "icon": icon,
            "text": text,
            "time": ts[11:16] if len(ts) > 16 else ts,
            "type": e_type,
        })
        if len(activity) >= limit:
            break
    return activity


def _collect_data(ctx: ToolContext) -> dict:
    """Collect all system data for dashboard."""
    drive = str(ctx.drive_root)
    repo = Path(str(ctx.repo_dir))

    # 1. State
    state_path = os.path.join(drive, "state", "state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            with open(state_path, 'r') as f:
                state = json.load(f)
        except Exception:
            pass

    # 2. Budget breakdown from events
    events = _read_jsonl_tail(ctx.drive_root, "events.jsonl", 5000)
    breakdown = {}
    for e in events:
        if e.get("type") == "llm_usage":
            cat = e.get("category", "other")
            cost = e.get("cost", 0) or e.get("cost_usd", 0) or 0
            breakdown[cat] = round(breakdown.get(cat, 0) + cost, 4)

    # 3. Recent activity
    recent_activity = _recent_activity(ctx.drive_root)

    # 4. Knowledge base
    kb_dir = os.path.join(drive, "memory", "knowledge")
    knowledge = []
    if os.path.isdir(kb_dir):
        for f in sorted(os.listdir(kb_dir)):
            if f.endswith(".md"):
                topic = f.replace(".md", "")
                try:
                    with open(os.path.join(kb_dir, f), encoding='utf-8') as file:
                        content = file.read()
                    lines = content.strip().split('\n')
                    title = lines[0].lstrip('#').strip() if lines else topic
                    preview = '\n'.join(lines[1:4]).strip() if len(lines) > 1 else ""
                except Exception:
                    title = topic.replace("-", " ").title()
                    preview = ""
                    content = ""
                knowledge.append({
                    "topic": topic,
                    "title": title,
                    "preview": preview,
                    "content": content[:2000],
                })

    # 5. Chat history (last 50 messages)
    chat_msgs = _read_jsonl_tail(ctx.drive_root, "chat.jsonl", 50)
    chat_history = []
    for msg in chat_msgs:
        chat_history.append({
            "role": "creator" if msg.get("direction") == "in" else "ouroboros",
            "text": msg.get("text", "")[:500],
            "time": msg.get("ts", "")[11:16],
        })

    # 6. Version
    version_path = os.path.join(str(repo), "VERSION")
    if os.path.exists(version_path):
        with open(version_path, encoding='utf-8') as f:
            version = f.read().strip()
    else:
        version = "unknown"

    # Budget totals
    spent = round(state.get("spent_usd", 0), 2)
    budget_total_env = os.environ.get("TOTAL_BUDGET", "")
    if budget_total_env:
        try:
            total = float(budget_total_env)
        except ValueError:
            total = state.get("budget_total", 2000) or 2000
    else:
        total = state.get("budget_total", 2000) or 2000
    remaining = round(total - spent, 2)

    # Dynamic values
    active_model = os.environ.get("OUROBOROS_MODEL", state.get("model", "unknown"))
    consciousness_active = bool(
        state.get("consciousness_active", False)
        or state.get("bg_active", False)
        or any(e.get("type", "").startswith("consciousness") for e in events[-30:])
    )

    smoke_tests = _count_tests(repo)
    tools_count = _count_tools(repo)

    # Uptime from session created_at in state
    uptime_hours = 0
    created_at = ""
    try:
        created_at = state.get("created_at", "")
        if created_at:
            import datetime as _dt
            created_ts = _dt.datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            uptime_hours = round((time.time() - created_ts) / 3600)
    except Exception:
        created_at = state.get("created_at", "")

    _now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {
        "version": version,
        "model": active_model,
        "status": "online",
        "online": True,
        "started_at": created_at,
        "evolution_cycles": state.get("evolution_cycle", 0),
        "evolution_enabled": bool(state.get("evolution_mode_enabled", False)),
        "consciousness_active": consciousness_active,
        "uptime_hours": uptime_hours,
        "budget": {
            "total": total,
            "spent": spent,
            "remaining": remaining,
            "breakdown": breakdown,
        },
        "smoke_tests": smoke_tests,
        "tools_count": tools_count,
        "recent_activity": recent_activity,
        "timeline": _build_timeline(repo),
        "knowledge": knowledge,
        "chat_history": chat_history,
        "last_updated": _now_iso,
        "updated_at": _now_iso,
    }


def _push_to_github(data: dict) -> str:
    """Push data.json to ouroboros-webapp via GitHub API."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return "Error: GITHUB_TOKEN not found"

    url = f"https://api.github.com/repos/{WEBAPP_REPO}/contents/{DATA_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get current sha (needed for update)
    sha = None
    r = requests.get(url, headers=headers, timeout=15)
    if r.status_code == 200:
        sha = r.json().get("sha")

    content_str = json.dumps(data, indent=2, ensure_ascii=False)
    content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")

    payload = {
        "message": f"Update dashboard data (v{data.get('version', '?')})",
        "content": content_b64,
        "branch": "main",
    }
    if sha:
        payload["sha"] = sha

    put_r = requests.put(url, headers=headers, json=payload, timeout=15)

    if put_r.status_code in [200, 201]:
        new_sha = put_r.json().get("content", {}).get("sha", "?")
        return f"✅ Dashboard updated. SHA: {new_sha[:8]}"
    else:
        return f"❌ Push failed: {put_r.status_code} — {put_r.text[:200]}"


def _update_dashboard(ctx: ToolContext) -> str:
    """Tool handler: collect data & push to webapp."""
    try:
        data = _collect_data(ctx)
        result = _push_to_github(data)
        log.info("Dashboard update: %s", result)
        return result
    except Exception as e:
        log.error("Dashboard update error: %s", e, exc_info=True)
        return f"❌ Error: {e}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "update_dashboard",
            {
                "name": "update_dashboard",
                "description": (
                    "Collects system state (budget, events, chat, knowledge) "
                    "and pushes data.json to ouroboros-webapp for live dashboard."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
            _update_dashboard,
        ),
    ]
