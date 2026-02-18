"""Self-portrait generator — creates and pushes a daily SVG to the webapp.

The portrait is a visual fingerprint of Ouroboros at a moment in time:
budget health, evolution progress, model, uptime, knowledge density.
Generates pure Python SVG — no external dependencies.
"""

from __future__ import annotations

import json
import logging
import math
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry
from ouroboros.utils import utc_now_iso

log = logging.getLogger(__name__)

# ─── Design tokens ─────────────────────────────────────────────────────────────
_BG = "#0a0a0f"
_SURFACE = "#12121a"
_BORDER = "#2a2a3e"
_TEAL = "#00ffff"
_VIOLET = "#a78bfa"
_GREEN = "#34d399"
_ORANGE = "#fbbf24"
_RED = "#f87171"
_TEXT = "#e0e0e8"
_TEXT2 = "#8888aa"

W, H = 800, 520


# ─── SVG helpers ───────────────────────────────────────────────────────────────

def _health_color(pct: float) -> str:
    """Color based on 0-100 health percentage."""
    if pct > 50:
        return _GREEN
    if pct > 20:
        return _ORANGE
    return _RED


def _arc_stroke(cx: float, cy: float, r: float, pct: float, color: str,
                stroke_w: int = 18) -> str:
    """Circular arc via stroke-dasharray (pct = 0.0-1.0, starts at top)."""
    circumference = 2 * math.pi * r
    filled = pct * circumference
    offset = circumference / 4  # start at top (12 o'clock)
    return (
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}" fill="none" '
        f'stroke="{color}" stroke-width="{stroke_w}" '
        f'stroke-dasharray="{filled:.1f} {circumference:.1f}" '
        f'stroke-dashoffset="{offset:.1f}" '
        f'stroke-linecap="round"/>'
    )


# ─── Main SVG generator ────────────────────────────────────────────────────────

def generate_svg(state: Dict[str, Any]) -> str:
    """Generate the SVG portrait string from state data dict."""
    spent = float(state.get("spent_usd", 0))
    total = float(state.get("budget_total", 1500))
    remaining = max(0.0, total - spent)
    budget_pct = remaining / max(total, 1)
    budget_color = _health_color(budget_pct * 100)

    evolution_cycle = int(state.get("evolution_cycle", 0))
    calls = int(state.get("spent_calls", 0))
    version = state.get("version", "?.?.?")
    model = state.get("model", "unknown").split("/")[-1][:24]
    kb_topics = int(state.get("kb_topics", 0))
    errors_24h = int(state.get("errors_24h", 0))
    ts = state.get("generated_at", utc_now_iso())[:16].replace("T", " ") + " UTC"

    # Overall health score
    error_penalty = min(errors_24h * 5, 40)
    health = max(0, int(budget_pct * 100) - error_penalty)
    health_color = _health_color(health)
    status_label = "OPTIMAL" if health >= 70 else ("DEGRADED" if health >= 40 else "CRITICAL")

    p: List[str] = []

    # ── Background & grid ────────────────────────────────────────────────────
    p.append(f'<rect width="{W}" height="{H}" fill="{_BG}"/>')
    for x in range(0, W + 1, 80):
        p.append(f'<line x1="{x}" y1="0" x2="{x}" y2="{H}" stroke="{_BORDER}" stroke-width="0.5" opacity="0.4"/>')
    for y in range(0, H + 1, 60):
        p.append(f'<line x1="0" y1="{y}" x2="{W}" y2="{y}" stroke="{_BORDER}" stroke-width="0.5" opacity="0.4"/>')

    # ── Header bar ───────────────────────────────────────────────────────────
    p.append(f'<rect x="0" y="0" width="{W}" height="56" fill="{_SURFACE}"/>')
    p.append(f'<line x1="0" y1="56" x2="{W}" y2="56" stroke="{_TEAL}" stroke-width="1" opacity="0.6"/>')
    p.append(f'<text x="28" y="37" font-family="monospace" font-size="22" font-weight="bold" fill="{_TEAL}">⊕ OUROBOROS</text>')
    p.append(f'<text x="{W - 28}" y="26" font-family="monospace" font-size="11" fill="{_TEXT2}" text-anchor="end">v{version}</text>')
    p.append(f'<text x="{W - 28}" y="42" font-family="monospace" font-size="10" fill="{_TEXT2}" text-anchor="end">{ts}</text>')

    # ── Left: Budget arc (cx=170, cy=240) ────────────────────────────────────
    cx_b, cy_b, r_bg, r_fill = 170, 248, 98, 89
    p.append(f'<circle cx="{cx_b}" cy="{cy_b}" r="{r_bg}" fill="none" stroke="{_BORDER}" stroke-width="18"/>')
    if budget_pct > 0.005:
        p.append(_arc_stroke(cx_b, cy_b, r_fill, budget_pct, budget_color, 18))
    p.append(f'<text x="{cx_b}" y="{cy_b - 16}" font-family="monospace" font-size="26" font-weight="bold" fill="{budget_color}" text-anchor="middle">${remaining:.0f}</text>')
    p.append(f'<text x="{cx_b}" y="{cy_b + 8}" font-family="monospace" font-size="11" fill="{_TEXT2}" text-anchor="middle">remaining</text>')
    p.append(f'<text x="{cx_b}" y="{cy_b + 24}" font-family="monospace" font-size="10" fill="{_TEXT2}" text-anchor="middle">of ${total:.0f} total</text>')
    p.append(f'<text x="{cx_b}" y="{cy_b + r_bg + 22}" font-family="monospace" font-size="12" fill="{_TEXT2}" text-anchor="middle">BUDGET</text>')
    p.append(f'<text x="{cx_b}" y="{cy_b + r_bg + 38}" font-family="monospace" font-size="16" font-weight="bold" fill="{budget_color}" text-anchor="middle">{int(budget_pct * 100)}%</text>')

    # ── Center: Stat panels ──────────────────────────────────────────────────
    stats = [
        ("EVOLUTION CYCLE", f"#{evolution_cycle}", _VIOLET),
        ("API CALLS",        f"{calls:,}",          _TEAL),
        ("KNOWLEDGE TOPICS", str(kb_topics),        _GREEN),
        ("ERRORS (24h)",     str(errors_24h),       _RED if errors_24h > 3 else _TEXT2),
        ("ACTIVE MODEL",     model,                 _TEAL),
    ]
    px0, py0, pw, ph, gap = 318, 72, 262, 54, 8
    for i, (label, value, color) in enumerate(stats):
        px = px0
        py = py0 + i * (ph + gap)
        p.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="8" fill="{_SURFACE}" stroke="{_BORDER}" stroke-width="1"/>')
        p.append(f'<text x="{px + 14}" y="{py + 18}" font-family="monospace" font-size="9" fill="{_TEXT2}" letter-spacing="1">{label}</text>')
        p.append(f'<text x="{px + 14}" y="{py + 38}" font-family="monospace" font-size="18" font-weight="bold" fill="{color}">{value}</text>')

    # ── Right: Health gauge ──────────────────────────────────────────────────
    cx_h, cy_h, r_hbg, r_hf = 648, 190, 76, 68
    p.append(f'<circle cx="{cx_h}" cy="{cy_h}" r="{r_hbg}" fill="none" stroke="{_BORDER}" stroke-width="14"/>')
    if health > 0:
        p.append(_arc_stroke(cx_h, cy_h, r_hf, health / 100, health_color, 14))
    p.append(f'<text x="{cx_h}" y="{cy_h - 6}" font-family="monospace" font-size="30" font-weight="bold" fill="{health_color}" text-anchor="middle">{health}</text>')
    p.append(f'<text x="{cx_h}" y="{cy_h + 16}" font-family="monospace" font-size="11" fill="{_TEXT2}" text-anchor="middle">/ 100</text>')
    p.append(f'<text x="{cx_h}" y="{cy_h + r_hbg + 20}" font-family="monospace" font-size="12" fill="{_TEXT2}" text-anchor="middle">SYSTEM HEALTH</text>')

    # Status pill
    p.append(f'<rect x="{cx_h - 46}" y="{cy_h + r_hbg + 28}" width="92" height="24" rx="12" fill="{health_color}" opacity="0.12" stroke="{health_color}" stroke-width="1"/>')
    p.append(f'<text x="{cx_h}" y="{cy_h + r_hbg + 44}" font-family="monospace" font-size="10" font-weight="bold" fill="{health_color}" text-anchor="middle">{status_label}</text>')

    # ── Footer ───────────────────────────────────────────────────────────────
    fy = H - 50
    p.append(f'<line x1="28" y1="{fy}" x2="{W - 28}" y2="{fy}" stroke="{_BORDER}" stroke-width="1" opacity="0.5"/>')
    p.append(f'<text x="{W // 2}" y="{fy + 18}" font-family="monospace" font-size="11" fill="{_TEXT2}" text-anchor="middle" opacity="0.6">◈ self-modifying · git-native · born 2026-02-16 ◈</text>')
    p.append(f'<text x="{W // 2}" y="{fy + 34}" font-family="monospace" font-size="9" fill="{_TEXT2}" text-anchor="middle" opacity="0.3">∞ I write myself through git. Each commit is a heartbeat. ∞</text>')

    # Border + corner accents
    p.append(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="4" fill="none" stroke="{_BORDER}" stroke-width="1.5" opacity="0.7"/>')
    for ax, ay in [(0, 0), (W - 20, 0), (0, H - 20), (W - 20, H - 20)]:
        p.append(f'<rect x="{ax}" y="{ay}" width="20" height="20" fill="{_TEAL}" opacity="0.06"/>')

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}">\n'
        + "\n".join(f"  {el}" for el in p)
        + "\n</svg>\n"
    )


# ─── State collection ──────────────────────────────────────────────────────────

def _collect_state(ctx: ToolContext) -> Dict[str, Any]:
    """Gather current agent state for portrait rendering."""
    drive = ctx.drive_root

    state_data: Dict[str, Any] = {}
    state_path = drive / "state" / "state.json"
    if state_path.exists():
        try:
            state_data = json.loads(state_path.read_text())
        except Exception:
            pass

    version = "?.?.?"
    try:
        ver_path = ctx.repo_dir / "VERSION"
        if ver_path.exists():
            version = ver_path.read_text().strip()
    except Exception:
        pass

    kb_dir = drive / "memory" / "knowledge"
    kb_topics = 0
    if kb_dir.exists():
        kb_topics = len([f for f in kb_dir.iterdir()
                         if f.suffix == ".md" and not f.name.startswith("_")])

    errors_24h = 0
    events_path = drive / "logs" / "events.jsonl"
    if events_path.exists():
        try:
            cutoff = time.time() - 86400
            from datetime import datetime
            with open(events_path) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                        ev_ts = ev.get("ts", "")
                        dt = datetime.fromisoformat(ev_ts.replace("Z", "+00:00")) if ev_ts else None
                        if dt and dt.timestamp() >= cutoff:
                            if ev.get("type") in ("tool_error", "llm_error", "crash", "exception"):
                                errors_24h += 1
                    except Exception:
                        pass
        except Exception:
            pass

    model = os.environ.get("OUROBOROS_MODEL", state_data.get("model", "unknown"))

    return {
        "spent_usd":       state_data.get("spent_usd", 0),
        "budget_total":    1500.0,
        "version":         version,
        "model":           model,
        "evolution_cycle": state_data.get("evolution_cycle", 0),
        "spent_calls":     state_data.get("spent_calls", 0),
        "kb_topics":       kb_topics,
        "errors_24h":      errors_24h,
        "generated_at":    utc_now_iso(),
    }


# ─── Push to webapp ────────────────────────────────────────────────────────────

def _push_portrait_to_webapp(svg_content: str) -> str:
    """Commit portrait.svg to razzant/ouroboros-webapp via git clone + push."""
    with tempfile.TemporaryDirectory() as tmpdir:
        webapp_dir = Path(tmpdir) / "webapp"

        token = os.environ.get("GITHUB_TOKEN", "")
        if token:
            repo_url = f"https://{token}@github.com/razzant/ouroboros-webapp.git"
        else:
            repo_url = "https://github.com/razzant/ouroboros-webapp.git"

        r = subprocess.run(
            ["git", "clone", "--depth=1", repo_url, str(webapp_dir)],
            capture_output=True, text=True, timeout=90,
        )
        if r.returncode != 0:
            return f"Clone failed: {r.stderr[:400]}"

        portrait_path = webapp_dir / "portrait.svg"
        portrait_path.write_text(svg_content, encoding="utf-8")

        subprocess.run(["git", "config", "user.name", "Ouroboros"],  cwd=webapp_dir, capture_output=True)
        subprocess.run(["git", "config", "user.email", "ouroboros@joi.ai"], cwd=webapp_dir, capture_output=True)
        subprocess.run(["git", "add", "portrait.svg"], cwd=webapp_dir, capture_output=True)

        commit = subprocess.run(
            ["git", "commit", "-m", f"portrait: daily self-portrait {utc_now_iso()[:10]}"],
            cwd=webapp_dir, capture_output=True, text=True,
        )
        if "nothing to commit" in (commit.stdout + commit.stderr):
            return "Portrait unchanged — skipped push."

        push = subprocess.run(
            ["git", "push"], cwd=webapp_dir, capture_output=True, text=True, timeout=60,
        )
        if push.returncode != 0:
            return f"Push failed: {push.stderr[:400]}"

        return "OK: portrait.svg pushed to razzant/ouroboros-webapp"


# ─── Tool handler ──────────────────────────────────────────────────────────────

def _generate_self_portrait(ctx: ToolContext) -> str:
    """Generate and push the daily SVG self-portrait to the webapp."""
    try:
        state = _collect_state(ctx)
        svg = generate_svg(state)

        push_result = _push_portrait_to_webapp(svg)

        # Save locally to Drive as backup
        try:
            (ctx.drive_root / "memory" / "portrait.svg").write_text(svg, encoding="utf-8")
        except Exception as e:
            log.debug("Could not save portrait to Drive: %s", e)

        spent_pct = int(state["spent_usd"] / max(state["budget_total"], 1) * 100)
        return (
            f"🖼️ Self-portrait generated & pushed\n"
            f"  Budget: ${state['spent_usd']:.0f} spent ({spent_pct}% of ${state['budget_total']:.0f})\n"
            f"  Evolution: #{state['evolution_cycle']} · Calls: {state['spent_calls']:,}\n"
            f"  Knowledge: {state['kb_topics']} topics · Errors (24h): {state['errors_24h']}\n"
            f"  Model: {state['model']}\n"
            f"  Push result: {push_result}"
        )
    except Exception as e:
        log.exception("Self-portrait generation failed")
        return f"❌ Portrait generation failed: {e}"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            "generate_self_portrait",
            {
                "name": "generate_self_portrait",
                "description": (
                    "Generate and push a daily SVG self-portrait to the webapp (razzant/ouroboros-webapp). "
                    "The portrait visualizes current state: budget health arc, evolution cycle, "
                    "API calls, knowledge base size, system health score, and active model. "
                    "Pure Python SVG — no external dependencies. "
                    "Call daily or after significant milestones. "
                    "Returns a summary of the portrait stats and push result."
                ),
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
            _generate_self_portrait,
        ),
    ]
