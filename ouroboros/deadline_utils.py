"""Small shared helpers for deadline-aware task behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional


def parse_deadline_ts(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def deadline_remaining_sec(ctx: Any) -> float:
    meta = getattr(ctx, "task_metadata", {})
    if not isinstance(meta, dict):
        return 0.0
    deadline = parse_deadline_ts(meta.get("deadline_at"))
    return (deadline - utc_now()).total_seconds() if deadline is not None else 0.0
