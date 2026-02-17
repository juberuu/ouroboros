"""
Drive-based owner message injection for running tasks.

When a task is running in a separate worker process, owner messages from Telegram
can be written here, and the loop reads them on each round.
"""
import datetime
import json
import logging
import pathlib
from typing import Optional

log = logging.getLogger(__name__)

_FILENAME = "memory/owner_messages_pending.jsonl"


def get_pending_path(drive_root: pathlib.Path) -> pathlib.Path:
    return drive_root / _FILENAME


def write_owner_message(drive_root: pathlib.Path, text: str) -> None:
    """Write an owner message to the pending file (called from launcher/supervisor)."""
    path = get_pending_path(drive_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "text": text,
    }, ensure_ascii=False)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        log.debug("Suppressed exception", exc_info=True)


def drain_owner_messages(drive_root: pathlib.Path) -> list:
    """Read and clear all pending owner messages. Returns list of message texts."""
    path = get_pending_path(drive_root)
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return []
        messages = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                messages.append(entry.get("text", ""))
            except Exception:
                log.debug("Suppressed exception", exc_info=True)
        # Clear the file after reading
        path.write_text("", encoding="utf-8")
        return [m for m in messages if m]
    except Exception:
        log.debug("Suppressed exception", exc_info=True)
        return []
