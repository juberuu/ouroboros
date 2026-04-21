"""Tests for supervisor/events.py _handle_llm_usage event persistence."""

import json


def test_llm_usage_writes_cached_tokens_and_cache_write_tokens(tmp_path):
    from supervisor import events as ev_module

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        def update_budget_from_usage(self, usage):
            self.last_usage = usage

    evt = {
        "type": "llm_usage",
        "model": "anthropic/claude-sonnet-4.6",
        "usage": {
            "prompt_tokens": 2000,
            "completion_tokens": 300,
            "cost": 0.01,
            "cached_tokens": 1200,
            "cache_write_tokens": 400,
        },
        "category": "compaction",
        "provider": "openrouter",
        "source": "loop",
        "model_category": "light",
        "api_key_type": "openrouter",
        "cost_estimated": False,
    }
    ctx = FakeCtx()
    ev_module._handle_llm_usage(evt, ctx)

    events_file = tmp_path / "logs" / "events.jsonl"
    written = json.loads(events_file.read_text(encoding="utf-8").strip())
    assert written.get("cached_tokens") == 1200
    assert written.get("cache_write_tokens") == 400
    assert written.get("category") == "compaction"
    assert written.get("provider") == "openrouter"
    assert written.get("source") == "loop"
    assert written.get("model_category") == "light"
    assert written.get("api_key_type") == "openrouter"
    assert written.get("cost_estimated") is False
    assert ctx.last_usage["cached_tokens"] == 1200


def test_task_metrics_are_persisted_and_forwarded_to_live_logs(tmp_path):
    from supervisor import events as ev_module

    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    pushed = []

    class FakeBridge:
        def push_log(self, payload):
            pushed.append(payload)

    class FakeCtx:
        DRIVE_ROOT = tmp_path
        bridge = FakeBridge()

        @staticmethod
        def append_jsonl(path, payload):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload) + "\n")

    evt = {
        "ts": "2026-03-31T10:11:12Z",
        "task_id": "task-99",
        "task_type": "task",
        "duration_sec": 3.14159,
        "tool_calls": 4,
        "tool_errors": 1,
    }
    ev_module._handle_task_metrics(evt, FakeCtx())

    written = json.loads((tmp_path / "logs" / "supervisor.jsonl").read_text(encoding="utf-8").strip())
    assert written["type"] == "task_metrics_event"
    assert written["task_id"] == "task-99"
    assert written["tool_calls"] == 4
    assert written["duration_sec"] == 3.142
    assert pushed[0]["task_id"] == "task-99"
    assert pushed[0]["tool_errors"] == 1
