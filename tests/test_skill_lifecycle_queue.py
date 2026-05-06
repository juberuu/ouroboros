from __future__ import annotations

import asyncio
import contextlib
import threading

import pytest
import ouroboros.skill_lifecycle_queue as q


def _reset_queue():
    q._events.clear()
    q._active = None
    q._lock = None
    q._dedupe_jobs.clear()


def test_lifecycle_job_success_notifies(monkeypatch):
    _reset_queue()
    sent = []

    def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", fake_send)

    async def runner():
        return {"ok": True}

    result = asyncio.run(
        q.run_lifecycle_job(
            kind="review",
            target="weather",
            runner=runner,
            options=q.LifecycleJobOptions(result_message=lambda _r: "done"),
        )
    )
    snap = q.queue_snapshot()
    assert result == {"ok": True}
    assert snap["events"][-1]["status"] == "succeeded"
    assert sent
    progress = [kwargs for _args, kwargs in sent if kwargs.get("is_progress")]
    assert progress
    assert any(str(item.get("task_id") or "").startswith("skill_lifecycle_review_weather_") for item in progress)
    assert not any(kwargs.get("task_id") == "skill_lifecycle_review" for _args, kwargs in sent)


def test_lifecycle_job_failure_records_error(monkeypatch):
    _reset_queue()
    sent = []
    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **k: sent.append((a, k)))

    async def runner():
        raise RuntimeError("boom")

    try:
        asyncio.run(q.run_lifecycle_job(kind="install", target="bad", runner=runner))
    except RuntimeError:
        pass
    event = q.queue_snapshot()["events"][-1]
    assert event["status"] == "failed"
    assert event["error"] == "boom"
    assert sent
    assert any(kwargs.get("is_progress") for _args, kwargs in sent)


def test_repeated_lifecycle_jobs_get_distinct_chat_task_ids(monkeypatch):
    _reset_queue()
    sent = []
    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **k: sent.append((a, k)))

    async def runner():
        return {"ok": True}

    async def main():
        await q.run_lifecycle_job(kind="review", target="weather", runner=runner)
        await q.run_lifecycle_job(kind="review", target="weather", runner=runner)

    asyncio.run(main())

    completed_ids = [
        kwargs.get("task_id")
        for args, kwargs in sent
        if kwargs.get("is_progress") and "completed" in str(args[1])
    ]
    assert len(completed_ids) == 2
    assert len(set(completed_ids)) == 2
    assert all(str(task_id).startswith("skill_lifecycle_review_weather_") for task_id in completed_ids)


def test_lifecycle_chat_task_ids_remain_distinct_after_queue_reset(monkeypatch):
    _reset_queue()
    sent = []
    monkeypatch.setattr("supervisor.message_bus.send_with_budget", lambda *a, **k: sent.append((a, k)))

    async def runner():
        return {"ok": True}

    asyncio.run(q.run_lifecycle_job(kind="review", target="weather", runner=runner))
    _reset_queue()
    asyncio.run(q.run_lifecycle_job(kind="review", target="weather", runner=runner))

    completed_ids = [
        kwargs.get("task_id")
        for args, kwargs in sent
        if kwargs.get("is_progress") and "completed" in str(args[1])
    ]
    assert len(completed_ids) == 2
    assert len(set(completed_ids)) == 2


def test_lifecycle_jobs_serialize():
    _reset_queue()
    order = []

    async def make_runner(name):
        async def runner():
            order.append(f"start-{name}")
            await asyncio.sleep(0.01)
            order.append(f"end-{name}")
            return name
        return runner

    async def main():
        await asyncio.gather(
            q.run_lifecycle_job(kind="a", target="one", runner=await make_runner("one")),
            q.run_lifecycle_job(kind="b", target="two", runner=await make_runner("two")),
        )

    asyncio.run(main())
    assert order in (["start-one", "end-one", "start-two", "end-two"], ["start-two", "end-two", "start-one", "end-one"])


def test_lifecycle_queue_keeps_recent_80_events():
    _reset_queue()

    async def runner():
        return True

    async def main():
        for idx in range(85):
            await q.run_lifecycle_job(kind="k", target=str(idx), runner=runner)

    asyncio.run(main())
    events = q.queue_snapshot()["events"]
    assert len(events) == 80
    assert events[0]["target"] == "5"
    assert events[-1]["target"] == "84"


def test_lifecycle_job_blocking_wrapper_records_event():
    _reset_queue()

    result = q.run_lifecycle_job_blocking(
        kind="review",
        target="weather",
        dedupe_key="review:weather:abc",
        runner=lambda: {"ok": True},
        options=q.LifecycleJobOptions(result_message=lambda _r: "done"),
    )

    assert result == {"ok": True}
    event = q.queue_snapshot()["events"][-1]
    assert event["kind"] == "review"
    assert event["dedupe_key"] == "review:weather:abc"
    assert event["status"] == "succeeded"


def test_lifecycle_dedupe_rejects_active_duplicate():
    _reset_queue()
    started = threading.Event()
    release = threading.Event()

    async def runner():
        started.set()
        await asyncio.to_thread(release.wait)
        return {"ok": True}

    async def main():
        first = asyncio.create_task(
            q.run_lifecycle_job(
                kind="review",
                target="weather",
                dedupe_key="review:weather:abc",
                runner=runner,
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        with pytest.raises(q.DuplicateLifecycleJobError) as exc:
            await q.run_lifecycle_job(
                kind="review",
                target="weather",
                dedupe_key="review:weather:abc",
                runner=runner,
            )
        assert exc.value.job.target == "weather"
        release.set()
        assert await first == {"ok": True}

    asyncio.run(main())


def test_cancelled_waiting_lifecycle_job_releases_lock_and_dedupe():
    _reset_queue()
    first_started = threading.Event()
    release_first = threading.Event()

    async def blocking_runner():
        first_started.set()
        await asyncio.to_thread(release_first.wait)
        return {"first": True}

    async def quick_runner():
        return {"ok": True}

    async def main():
        first = asyncio.create_task(
            q.run_lifecycle_job(
                kind="review",
                target="alpha",
                dedupe_key="review:alpha:hash",
                runner=blocking_runner,
            )
        )
        assert await asyncio.to_thread(first_started.wait, 2)
        second = asyncio.create_task(
            q.run_lifecycle_job(
                kind="review",
                target="beta",
                dedupe_key="review:beta:hash",
                runner=quick_runner,
            )
        )
        await asyncio.sleep(0.05)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release_first.set()
        assert await asyncio.wait_for(first, timeout=2) == {"first": True}
        assert await asyncio.wait_for(
            q.run_lifecycle_job(
                kind="review",
                target="beta",
                dedupe_key="review:beta:hash",
                runner=quick_runner,
            ),
            timeout=2,
        ) == {"ok": True}

    asyncio.run(main())


def test_cancelled_file_lock_wait_cleans_active_job(monkeypatch):
    _reset_queue()
    entered_file_lock = threading.Event()
    release_file_lock = threading.Event()
    finished = []

    @contextlib.asynccontextmanager
    async def fake_file_lock(_drive_root):
        entered_file_lock.set()
        await asyncio.to_thread(release_file_lock.wait)
        yield

    monkeypatch.setattr(q, "async_skill_lifecycle_file_lock", fake_file_lock)

    async def runner():
        return {"ok": True}

    async def main():
        task = asyncio.create_task(
            q.run_lifecycle_job(
                kind="review",
                target="alpha",
                dedupe_key="review:alpha:file-lock",
                runner=runner,
                options=q.LifecycleJobOptions(
                    on_finished=lambda job, _result, exc: finished.append(
                        (job.status, type(exc).__name__ if exc else "")
                    ),
                ),
            )
        )
        assert await asyncio.to_thread(entered_file_lock.wait, 2)
        assert q.queue_snapshot()["active"]["target"] == "alpha"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert q.queue_snapshot()["active"] is None
        assert finished == [("cancelled", "CancelledError")]
        release_file_lock.set()
        assert await asyncio.wait_for(
            q.run_lifecycle_job(
                kind="review",
                target="alpha",
                dedupe_key="review:alpha:file-lock",
                runner=runner,
            ),
            timeout=2,
        ) == {"ok": True}

    asyncio.run(main())


def test_async_file_lock_cancelled_wait_has_no_late_unlock(tmp_path, monkeypatch):
    import ouroboros.platform_layer as platform_layer

    allow_lock = threading.Event()
    attempts = []
    unlocks = []

    def fake_lock(_fd):
        attempts.append("lock")
        if not allow_lock.is_set():
            raise OSError("busy")

    def fake_unlock(_fd):
        unlocks.append("unlock")

    monkeypatch.setattr(platform_layer, "file_lock_exclusive_nb", fake_lock)
    monkeypatch.setattr(platform_layer, "file_unlock", fake_unlock)

    async def wait_on_lock():
        async with q.async_skill_lifecycle_file_lock(tmp_path):
            return True

    async def main():
        task = asyncio.create_task(wait_on_lock())
        await asyncio.sleep(0.06)
        assert attempts
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert unlocks == []
        allow_lock.set()
        assert await wait_on_lock() is True
        assert unlocks == ["unlock"]

    asyncio.run(main())
