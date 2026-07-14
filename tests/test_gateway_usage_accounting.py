from __future__ import annotations

import asyncio
import json
import types

from starlette.requests import Request

from ouroboros import usage_accounting as ua


def _data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    (root / "logs").mkdir(parents=True)
    (root / "state" / "state.json").write_text(
        json.dumps({"spent_usd": 0.0, "spent_calls": 0}),
        encoding="utf-8",
    )
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "7.5")
    ua.ensure_legacy_imported(root)
    return root


def _attempt(root, *, reservation_usd, task_id, category="task"):
    return ua.reserve_attempt(ua.AttemptRequest(
        model="openai/gpt-5.2",
        provider="openai",
        reservation_usd=reservation_usd,
        global_limit_usd=7.5,
        drive_root=root,
        task_id=task_id,
        root_task_id="root-1",
        category=category,
        source="test.gateway",
    ))


def _seed_accounting(root):
    settled = _attempt(root, reservation_usd=0.5, task_id="settled", category="review")
    ua.mark_dispatched(settled)
    ua.settle_attempt(
        settled,
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cached_tokens": 40,
            "cache_write_tokens": 5,
            "prompt_cache_ttl": "default",
        },
        cost_usd=0.25,
        cost_final=True,
    )
    _attempt(root, reservation_usd=1.0, task_id="reserved")
    unresolved = _attempt(root, reservation_usd=0.5, task_id="unresolved")
    ua.mark_dispatched(unresolved)
    ua.mark_unresolved(unresolved, "provider outcome unknown")
    ua.record_unmetered_external_dispatch(
        "external-skill-call",
        drive_root=root,
        provider="external-skill",
        task_id="external",
        root_task_id="root-1",
        category="skill",
        source="test.external",
    )


def test_cost_breakdown_uses_ledger_not_later_compatibility_events(tmp_path, monkeypatch):
    from ouroboros.gateway.history import make_cost_breakdown_endpoint
    from supervisor import state as supervisor_state

    root = _data_root(tmp_path, monkeypatch)
    _seed_accounting(root)
    monkeypatch.setattr(supervisor_state, "TOTAL_BUDGET_LIMIT", 7.5)
    with (root / "logs" / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "type": "llm_usage",
            "model": "fabricated/compatibility-only",
            "provider": "openrouter",
            "cost": 99.0,
            "prompt_tokens": 999,
        }) + "\n")

    response = asyncio.run(make_cost_breakdown_endpoint(root)(None))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["total_cost"] == 0.25
    assert payload["total_calls"] == 3
    assert payload["total_prompt_tokens"] == 100
    assert "fabricated/compatibility-only" not in payload["by_model"]
    assert payload["by_model"]["openai/gpt-5.2"]["cost"] == 0.25
    assert payload["by_task_category"]["review"]["calls"] == 1
    assert payload["accounting"] == {
        "available": True,
        "settled_usd": 0.25,
        "confirmed_usd": 0.25,
        "estimated_usd": 0.0,
        "reserved_usd": 1.0,
        "unresolved_upper_bound_usd": 0.5,
        "accounted_usd": 1.75,
        "unknown_unmetered": 1,
        "cost_final": False,
        "attempt_counts": {"reserved": 1, "settled": 2, "unresolved": 1},
        "authority": "physical_attempt_ledger",
        "limit_usd": 7.5,
        "remaining_known_usd": 5.75,
    }


def test_api_state_money_and_call_count_are_ledger_projections(tmp_path, monkeypatch):
    from ouroboros.gateway.state import api_state
    from supervisor import queue, state, workers

    root = _data_root(tmp_path, monkeypatch)
    _seed_accounting(root)
    # Compatibility state may lag or be corrupt without becoming monetary authority.
    (root / "state" / "state.json").write_text(
        json.dumps({"spent_usd": 99.0, "spent_calls": 999, "current_branch": "ouroboros"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(state, "TOTAL_BUDGET_LIMIT", 7.5)
    monkeypatch.setattr(state, "load_state", lambda: {
        "spent_usd": 99.0,
        "spent_calls": 999,
        "current_branch": "ouroboros",
    })
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "PENDING", [])
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(queue, "get_evolution_status_snapshot", lambda: {})
    app = types.SimpleNamespace(state=types.SimpleNamespace(
        drive_root=root,
        app_start=0.0,
    ))
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/state",
        "headers": [],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1),
        "app": app,
    })

    response = asyncio.run(api_state(request))
    payload = json.loads(response.body)

    assert response.status_code == 200
    assert payload["spent_usd"] == 1.75
    assert payload["spent_calls"] == 3
    assert payload["budget_limit"] == 7.5
    assert payload["accounting"]["authority"] == "physical_attempt_ledger"
    assert payload["accounting"]["accounted_usd"] == 1.75
    assert payload["accounting"]["unknown_unmetered"] == 1
    assert payload["accounting"]["remaining_known_usd"] == 5.75


def test_cost_breakdown_fails_loudly_when_authoritative_history_is_corrupt(tmp_path, monkeypatch):
    from ouroboros.gateway.history import make_cost_breakdown_endpoint

    root = _data_root(tmp_path, monkeypatch)
    (root / ua.LEDGER_REL).write_bytes(b"not-json\n{}\n")
    (root / "logs" / "events.jsonl").write_text(
        json.dumps({"type": "llm_usage", "model": "fake", "cost": 99.0}) + "\n",
        encoding="utf-8",
    )

    response = asyncio.run(make_cost_breakdown_endpoint(root)(None))
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert "error" in payload
    assert payload["accounting"] == {
        "available": False,
        "authority": "physical_attempt_ledger",
        "cost_final": False,
        "error_code": "ledger_unavailable",
    }
    assert "total_cost" not in payload


def test_api_state_marks_accounting_unavailable_without_legacy_zero(tmp_path, monkeypatch):
    from ouroboros.gateway.state import api_state
    from supervisor import queue, state, workers

    root = _data_root(tmp_path, monkeypatch)
    (root / ua.LEDGER_REL).write_text("not-json\n{}\n", encoding="utf-8")
    monkeypatch.setattr(state, "TOTAL_BUDGET_LIMIT", 7.5)
    monkeypatch.setattr(state, "load_state", lambda: {
        "spent_usd": 99.0, "spent_calls": 999, "current_branch": "ouroboros",
    })
    monkeypatch.setattr(workers, "WORKERS", {})
    monkeypatch.setattr(workers, "PENDING", [])
    monkeypatch.setattr(workers, "RUNNING", {})
    monkeypatch.setattr(queue, "get_evolution_status_snapshot", lambda: {})
    request = Request({
        "type": "http", "method": "GET", "path": "/api/state", "headers": [],
        "query_string": b"", "scheme": "http", "server": ("test", 80),
        "client": ("test", 1),
        "app": types.SimpleNamespace(state=types.SimpleNamespace(drive_root=root, app_start=0.0)),
    })

    response = asyncio.run(api_state(request))
    payload = json.loads(response.body)
    assert response.status_code == 200
    assert payload["spent_usd"] is None
    assert payload["spent_calls"] is None
    assert payload["budget_pct"] is None
    assert payload["accounting"]["available"] is False
    assert payload["accounting"]["accounted_usd"] is None
    assert payload["accounting"]["remaining_known_usd"] is None
    assert payload["accounting"]["error_code"] == "ledger_unavailable"
