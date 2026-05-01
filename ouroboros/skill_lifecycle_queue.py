"""Global lifecycle queue for mutating skill operations.

The Skills, ClawHub, and OuroborosHub surfaces can all trigger long-running
operations that touch the same skill state plane. This module provides one
process-local FIFO lane so install/review/dependency/enable operations do not
race each other through unrelated HTTP handlers.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import pathlib
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Deque, Dict, Optional


_MAX_EVENTS = 80


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class LifecycleJob:
    id: str
    kind: str
    target: str
    source: str = ""
    status: str = "queued"
    message: str = ""
    error: str = ""
    queued_at: str = field(default_factory=_now_iso)
    started_at: str = ""
    finished_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "target": self.target,
            "source": self.source,
            "status": self.status,
            "message": self.message,
            "error": self.error,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


_job_counter = itertools.count(1)
_lock: Optional[asyncio.Lock] = None
_events: Deque[LifecycleJob] = deque(maxlen=_MAX_EVENTS)
_active: Optional[LifecycleJob] = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def _store(job: LifecycleJob) -> None:
    if job not in _events:
        _events.append(job)


@contextlib.contextmanager
def skill_lifecycle_file_lock(drive_root: pathlib.Path):
    from ouroboros.platform_layer import file_lock_exclusive, file_unlock

    lock_dir = pathlib.Path(drive_root) / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "skill_lifecycle.lock"
    with lock_path.open("a+") as fh:
        file_lock_exclusive(fh.fileno())
        try:
            yield
        finally:
            file_unlock(fh.fileno())


@contextlib.asynccontextmanager
async def async_skill_lifecycle_file_lock(drive_root: pathlib.Path):
    from ouroboros.platform_layer import file_lock_exclusive, file_unlock

    lock_dir = pathlib.Path(drive_root) / "state"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "skill_lifecycle.lock"
    with lock_path.open("a+") as fh:
        await asyncio.to_thread(file_lock_exclusive, fh.fileno())
        try:
            yield
        finally:
            await asyncio.to_thread(file_unlock, fh.fileno())


def _notify_chat(job: LifecycleJob) -> None:
    if job.status not in {"succeeded", "failed"}:
        return
    try:
        from supervisor.message_bus import send_with_budget

        tone = "completed" if job.status == "succeeded" else "failed"
        detail = job.error or job.message or job.status
        send_with_budget(
            0,
            f"### Skill lifecycle: `{job.target}` {tone}\n\n`{job.kind}`: {detail}",
            fmt="markdown",
            task_id=f"skill_lifecycle_{job.kind}",
        )
    except Exception:
        # Notification must never turn a completed lifecycle operation into an
        # API failure. Logs can still capture import/runtime issues upstream.
        return


async def run_lifecycle_job(
    *,
    kind: str,
    target: str,
    source: str = "",
    message: str = "",
    runner: Callable[[], Awaitable[Any]],
    result_message: Callable[[Any], str] | None = None,
    result_error: Callable[[Any], str] | None = None,
) -> Any:
    """Run ``runner`` through the global skill lifecycle lane."""

    global _active
    job = LifecycleJob(
        id=f"skill-job-{next(_job_counter)}",
        kind=str(kind or "operation"),
        target=str(target or "skill"),
        source=str(source or ""),
        message=str(message or ""),
    )
    _store(job)
    async with _get_lock():
        from ouroboros.config import DATA_DIR

        _active = job
        job.status = "running"
        job.started_at = _now_iso()
        async with async_skill_lifecycle_file_lock(pathlib.Path(DATA_DIR)):
            try:
                result = await runner()
                error = result_error(result) if result_error else ""
                job.error = str(error or "")
                job.status = "failed" if job.error else "succeeded"
                if result_message:
                    job.message = result_message(result)
                elif not job.message:
                    job.message = job.status
                return result
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                raise
            finally:
                job.finished_at = _now_iso()
                _active = None
                _notify_chat(job)


def queue_snapshot() -> Dict[str, Any]:
    """Return a JSON-friendly view of recent lifecycle activity."""

    return {
        "active": _active.to_dict() if _active else None,
        "events": [job.to_dict() for job in list(_events)],
    }
