from __future__ import annotations

import asyncio

import ouroboros.skill_lifecycle_queue as q


def _reset_queue():
    q._events.clear()
    q._active = None
    q._lock = None


def test_lifecycle_job_success_notifies(monkeypatch):
    _reset_queue()
    sent = []

    def fake_send(*args, **kwargs):
        sent.append((args, kwargs))

    monkeypatch.setattr("supervisor.message_bus.send_with_budget", fake_send)

    async def runner():
        return {"ok": True}

    result = asyncio.run(q.run_lifecycle_job(kind="review", target="weather", runner=runner, result_message=lambda _r: "done"))
    snap = q.queue_snapshot()
    assert result == {"ok": True}
    assert snap["events"][-1]["status"] == "succeeded"
    assert sent


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
