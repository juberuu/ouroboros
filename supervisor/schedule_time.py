"""Schedule time parsing helpers (cron + timezone) for the task queue."""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


def timezone_for_schedule(record: Dict[str, Any]) -> datetime.tzinfo:
    raw = str(record.get("timezone") or "").strip()
    if raw:
        try:
            return ZoneInfo(raw)
        except Exception:
            log.warning("Invalid schedule timezone %r; falling back to local time", raw)
    # Blank timezone -> DST-aware system local zone (platform-layer SSOT).
    from ouroboros.platform_layer import local_zoneinfo

    return local_zoneinfo()


def parse_schedule_time(value: Any, tz: datetime.tzinfo) -> Optional[datetime.datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def next_cron_time(expr: str, base: datetime.datetime) -> datetime.datetime:
    from croniter import croniter

    return croniter(str(expr or ""), base).get_next(datetime.datetime)


def schedule_next_run(record: Dict[str, Any], *, base: Optional[datetime.datetime] = None) -> str:
    trigger = record.get("trigger") if isinstance(record.get("trigger"), dict) else {}
    if str(trigger.get("type") or "cron") != "cron":
        return ""
    expr = str(trigger.get("expr") or record.get("cron") or "").strip()
    if not expr:
        return ""
    tz = timezone_for_schedule(record)
    base_dt = base.astimezone(tz) if base is not None else datetime.datetime.now(tz)
    return next_cron_time(expr, base_dt).isoformat()
