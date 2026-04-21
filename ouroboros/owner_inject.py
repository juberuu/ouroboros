"""
Per-task owner message injection for running worker tasks.

Each task gets its own mailbox file: owner_messages_{task_id}.jsonl
Messages have unique IDs for dedup. Reading uses offset tracking
(append-only within a session) instead of clearing the file.

The supervisor does NOT write here directly. Only the LLM (via
forward_to_worker tool) writes to a task's mailbox. Workers drain
messages for their own task_id on each LLM round.
"""
import datetime
import json
import logging
import pathlib
import uuid
from typing import List, Optional

log = logging.getLogger(__name__)

_MAILBOX_DIR = "memory/owner_mailbox"


def _mailbox_path(drive_root: pathlib.Path, task_id: str) -> pathlib.Path:
    return drive_root / _MAILBOX_DIR / f"{task_id}.jsonl"


def get_pending_path(drive_root: pathlib.Path) -> pathlib.Path:
    """Legacy compat: path to old global pending file (for cleanup on startup)."""
    return drive_root / "memory/owner_messages_pending.jsonl"


def write_owner_message(
    drive_root: pathlib.Path,
    text: str,
    task_id: str,
    msg_id: Optional[str] = None,
) -> None:
    """Write an owner message to a specific task's mailbox."""
    path = _mailbox_path(drive_root, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = json.dumps({
        "msg_id": msg_id or uuid.uuid4().hex,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "text": text,
    }, ensure_ascii=False)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
    except Exception:
        log.debug("Failed to write owner message for task %s", task_id, exc_info=True)


def drain_owner_messages(
    drive_root: pathlib.Path,
    task_id: str,
    seen_ids: Optional[set] = None,
) -> List[str]:
    """Read new messages for a specific task. Returns list of message texts.

    Uses seen_ids set for dedup: messages already in the set are skipped,
    new message IDs are added to it. Caller should keep the set across rounds.
    """
    path = _mailbox_path(drive_root, task_id)
    if not path.exists():
        return []
    if seen_ids is None:
        seen_ids = set()
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
                mid = entry.get("msg_id", "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                text = entry.get("text", "")
                if text:
                    messages.append(text)
            except Exception:
                log.debug("Malformed mailbox line for task %s", task_id, exc_info=True)
        return messages
    except Exception:
        log.debug("Failed to read mailbox for task %s", task_id, exc_info=True)
        return []


def cleanup_task_mailbox(drive_root: pathlib.Path, task_id: str) -> None:
    """Remove a task's mailbox file after task completes."""
    path = _mailbox_path(drive_root, task_id)
    try:
        if path.exists():
            path.unlink()
    except Exception:
        log.debug("Failed to cleanup mailbox for task %s", task_id, exc_info=True)
