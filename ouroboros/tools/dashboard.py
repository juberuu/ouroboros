import json
import os
import base64
import time
import requests
from ouroboros.utils import read_text, run_cmd, short
from ouroboros.memory import Memory

def _get_timeline():
    # Construct timeline from logs or scratchpad. For now, static placeholder + recent events
    # In future: parse events.jsonl for actual evolution milestones
    return [
        {"version": "4.24.0", "time": "2026-02-17", "event": "Web App v2 Deployed", "type": "milestone"},
        {"version": "4.21.0", "time": "2026-02-17", "event": "Budget Breakdown & Categorization", "type": "feature"},
        {"version": "4.18.0", "time": "2026-02-17", "event": "GitHub Issues Integration", "type": "feature"},
        {"version": "4.8.0", "time": "2026-02-16", "event": "Consciousness Loop Online", "type": "milestone"},
        {"version": "4.0.0", "time": "2026-02-16", "event": "Ouroboros Genesis", "type": "birth"}
    ]

def _update_dashboard():
    """Compiles system state and pushes data.json to ouroboros-webapp repo."""
    try:
        # 1. Load State
        state_path = "/content/drive/MyDrive/Ouroboros/state/state.json"
        if os.path.exists(state_path):
            with open(state_path, 'r') as f:
                state = json.load(f)
        else:
            state = {}

        # 2. Get uptime
        # We can estimate uptime from supervisor logs or just use current session start
        # For now, placeholder or calc from state['created_at']
        
        # 3. Get Recent Activity (from events.jsonl tail)
        events_path = "/content/drive/MyDrive/Ouroboros/logs/events.jsonl"
        recent_activity = []
        if os.path.exists(events_path):
            lines = run_cmd(["tail", "-n", "20", events_path]).split('\n')
            for line in reversed(lines):
                if not line.strip(): continue
                try:
                    e = json.load(line)
                    # Map event to activity item
                    icon = "📡"
                    text = e.get("event")
                    e_type = "info"
                    
                    if e.get("event") == "llm_usage": continue # too noisy
                    if e.get("event") == "task_done": icon="Mw"; text=f"Task completed: {short(e.get('result', ''), 30)}"; e_type="success"
                    if e.get("event") == "evolution_cycle": icon="🧬"; text=f"Evolution #{e.get('cycle')} {e.get('status')}"; e_type="evolution"
                    
                    recent_activity.append({
                        "icon": icon,
                        "text": text,
                        "time": e.get("timestamp", "")[11:16], # HH:MM
                        "type": e_type
                    })
                except: pass
        
        # 4. Get Knowledge Base
        kb_path = "/content/drive/MyDrive/Ouroboros/memory/knowledge"
        knowledge = []
        if os.path.exists(kb_path):
            for f in os.listdir(kb_path):
                if f.endswith(".md"):
                    content = read_text(os.path.join(kb_path, f))
                    knowledge.append({
                        "topic": f.replace(".md", ""),
                        "title": f.replace(".md", "").replace("-", " ").title(),
                        "content": content
                    })

        # 5. Get Chat History (last 50)
        chat_path = "/content/drive/MyDrive/Ouroboros/logs/chat.jsonl"
        chat_history = []
        if os.path.exists(chat_path):
            lines = run_cmd(["tail", "-n", "50", chat_path]).split('\n')
            for line in lines:
                if not line.strip(): continue
                try:
                    msg = json.loads(line)
                    chat_history.append({
                        "role": msg.get("role"),
                        "text": msg.get("content"),
                        "time": msg.get("timestamp", "")[11:16]
                    })
                except: pass

        # Compile Data
        data = {
            "version": read_text("VERSION").strip(),
            "model": state.get("model", "anthropic/claude-sonnet-4"),
            "evolution_cycles": state.get("evolution_cycle", 0),
            "evolution_enabled": state.get("evolution_mode_enabled", False),
            "consciousness_active": True, # TODO: check actual status
            "uptime_hours": 24, # TODO: calc
            "budget": {
                "total": state.get("budget_total", 1000),
                "spent": round(state.get("spent_usd", 0), 2),
                "remaining": round(state.get("budget_remaining", 0), 2),
                "breakdown": state.get("budget_breakdown", {})
            },
            "smoke_tests": 88, # TODO: dynamic
            "tools_count": 42, # TODO: dynamic
            "recent_activity": recent_activity[:10],
            "timeline": _get_timeline(),
            "knowledge": knowledge,
            "chat_history": chat_history,
            "last_updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        }

        # Push to GitHub (PUT /repos/.../contents/data.json)
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            return "Error: GITHUB_TOKEN not found"

        repo = "razzant/ouroboros-webapp"
        path = "data.json"
        
        # Get current sha needed for update
        url = f"https://api.github.com/repos/{repo}/contents/{path}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json"
        }
        
        sha = None
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            sha = r.json().get("sha")

        content_str = json.dumps(data, indent=2)
        content_b64 = base64.b64encode(content_str.encode("utf-8")).decode("utf-8")
        
        payload = {
            "message": f"Update dashboard data (v{data['version']})",
            "content": content_b64,
            "branch": "main"
        }
        if sha:
            payload["sha"] = sha

        put_r = requests.put(url, headers=headers, json=payload)
        
        if put_r.status_code in [200, 201]:
            return f"Dashboard updated successfully. SHA: {put_r.json()['content']['sha']}"
        else:
            return f"Failed to push data.json: {put_r.status_code} {put_r.text}"

    except Exception as e:
        return f"Error updating dashboard: {str(e)}"

def get_tools():
    return [
        {
            "name": "update_dashboard",
            "description": "Collects system state and pushes data.json to ouroboros-webapp for live dashboard updates.",
            "parameters": {
                "type": "OBJECT",
                "properties": {},
                "required": []
            }
        }
    ]
