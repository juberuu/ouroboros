from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ouroboros import usage_accounting as ua


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    (root / "state").mkdir(parents=True)
    return root


def _request(data_root, **overrides):
    values = {
        "model": "openai/gpt-5.2",
        "provider": "openai",
        "reservation_usd": 1.0,
        "drive_root": data_root,
        "task_id": "child",
        "root_task_id": "root",
        "source": "test",
    }
    values.update(overrides)
    return ua.AttemptRequest(**values)


def _ledger(data_root):
    path = data_root / ua.LEDGER_REL
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_attempt_lifecycle_and_root_projection(data_root):
    reservation = ua.reserve_attempt(_request(data_root, root_limit_usd=2.0))
    ua.mark_dispatched(reservation)
    ua.settle_attempt(
        reservation,
        {"prompt_tokens": 10, "completion_tokens": 5},
        cost_usd=0.25,
        cost_final=True,
    )

    projection = ua.usage_projection(data_root)
    assert projection["settled_usd"] == 0.25
    assert projection["confirmed_usd"] == 0.25
    assert projection["cost_final"] is True
    assert projection["by_root"]["root"]["settled_usd"] == 0.25
    assert projection["by_root"]["root"]["limit_usd"] == 2.0
    rows = _ledger(data_root)
    assert [row["state"] for row in rows] == ["reserved", "dispatched", "settled"]
    assert [row["seq"] for row in rows] == [1, 2, 3]


def test_projection_uses_explicit_runtime_limit_over_environment(data_root):
    assert ua.usage_projection(data_root, global_limit_usd=7.5)["limit_usd"] == 7.5


def test_breakdown_uses_final_rows_and_keeps_unattributed_explicit(data_root):
    reservation = ua.reserve_attempt(_request(
        data_root, category="review", prompt_tokens_estimate=10,
    ))
    ua.mark_dispatched(reservation)
    ua.settle_attempt(
        reservation,
        {"prompt_tokens": 10, "completion_tokens": 3, "cached_tokens": 2},
        cost_usd=0.2,
        cost_final=True,
    )
    external_id = ua.record_unmetered_external_dispatch(
        "skill-call-1",
        drive_root=data_root,
        provider="external-skill",
        category="skill",
        prompt_tokens=4,
        completion_tokens=1,
    )

    breakdown = ua.usage_breakdown(data_root)
    assert breakdown["physical_calls"] == 2
    assert breakdown["prompt_tokens"] == 14
    assert breakdown["completion_tokens"] == 4
    assert breakdown["confirmed_usd"] == 0.2
    assert breakdown["unknown_unmetered"] == 1
    assert breakdown["by_model"]["openai/gpt-5.2"]["physical_calls"] == 1
    assert breakdown["by_provider"]["external-skill"]["unknown_unmetered"] == 1
    assert breakdown["by_category"]["skill"]["physical_calls"] == 1
    assert breakdown["unattributed"]["model"]["physical_calls"] == 1
    assert external_id.startswith("external-")


def test_external_unmetered_dispatch_is_idempotent_and_conflict_checked(data_root):
    first = ua.record_unmetered_external_dispatch(
        "stable-id", drive_root=data_root, provider="skill", task_id="t",
    )
    second = ua.record_unmetered_external_dispatch(
        "stable-id", drive_root=data_root, provider="skill", task_id="t",
    )
    assert first == second
    assert len(_ledger(data_root)) == 1
    with pytest.raises(ua.UsageAccountingError, match="conflicting"):
        ua.record_unmetered_external_dispatch(
            "stable-id", drive_root=data_root, provider="different", task_id="t",
        )


def test_provider_failure_remains_unresolved(data_root):
    sends = 0

    def fail():
        nonlocal sends
        sends += 1
        raise TimeoutError("transport timeout")

    with pytest.raises(TimeoutError):
        ua.execute_physical_attempt(_request(data_root), fail)
    assert sends == 1
    projection = ua.usage_projection(data_root)
    assert projection["unresolved_upper_bound_usd"] == 1.0
    assert _ledger(data_root)[-1]["state"] == "unresolved"


def test_lock_failure_is_fail_closed_before_send(data_root, monkeypatch):
    import ouroboros.platform_layer as platform

    monkeypatch.setattr(platform, "acquire_exclusive_file_lock", lambda *args, **kwargs: None)
    sends = 0

    def send():
        nonlocal sends
        sends += 1

    with pytest.raises(ua.UsageAccountingError):
        ua.execute_physical_attempt(_request(data_root), send)
    assert sends == 0


def test_relative_or_mock_like_drive_root_never_writes_cwd(data_root):
    with pytest.raises(ua.UsageAccountingError, match="must be absolute"):
        ua.reserve_attempt(_request(data_root, drive_root="."))


def test_paid_response_survives_settlement_storage_failure(data_root, monkeypatch):
    def broken_settle(*args, **kwargs):
        raise OSError("disk full after response")

    monkeypatch.setattr(ua, "settle_attempt", broken_settle)
    response = {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}
    assert ua.execute_physical_attempt(_request(data_root), lambda: response) is response
    assert _ledger(data_root)[-1]["state"] == "unresolved"
    assert ua.usage_projection(data_root)["unresolved_upper_bound_usd"] == 1.0


def test_paid_response_survives_usage_extractor_failure(data_root):
    response = object()

    def broken_extractor(_response):
        raise ValueError("malformed provider usage")

    assert ua.execute_physical_attempt(
        _request(data_root), lambda: response, extractor=broken_extractor,
    ) is response
    assert _ledger(data_root)[-1]["state"] == "unresolved"


def test_async_paid_response_survives_usage_extractor_failure(data_root):
    response = object()

    async def send():
        return response

    def broken_extractor(_response):
        raise ValueError("malformed provider usage")

    result = asyncio.run(ua.execute_physical_attempt_async(
        _request(data_root), send, extractor=broken_extractor,
    ))
    assert result is response
    assert _ledger(data_root)[-1]["state"] == "unresolved"


def test_provider_reported_zero_cost_is_final_not_missing(data_root):
    response = {
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "cost": 0},
    }
    ua.execute_physical_attempt(_request(data_root), lambda: response)
    projection = ua.usage_projection(data_root)
    assert projection["confirmed_usd"] == 0
    assert projection["unknown_unmetered"] == 0
    assert projection["cost_final"] is True
    assert _ledger(data_root)[-1]["cost_usd"] == 0


def test_torn_final_row_is_quarantined_but_midstream_corruption_fails(data_root):
    reservation = ua.reserve_attempt(_request(data_root))
    ua.release_attempt(reservation)
    ledger = data_root / ua.LEDGER_REL
    with ledger.open("ab") as handle:
        handle.write(b'{"seq":')

    projection = ua.usage_projection(data_root)
    assert projection["attempt_counts"]["released"] == 1
    assert projection["integrity_degraded"] is True
    assert projection["cost_final"] is False
    breakdown = ua.usage_breakdown(data_root)
    assert breakdown["integrity_degraded"] is True
    assert breakdown["cost_final"] is False
    assert (data_root / ua.QUARANTINE_REL).is_file()
    repaired = ledger.read_bytes()
    assert b'{"seq":' not in repaired


@pytest.mark.parametrize(
    "field,value",
    (("seq", "not-a-number"), ("prompt_tokens", "not-a-number")),
)
def test_structurally_invalid_numeric_tail_is_quarantined(data_root, field, value):
    reservation = ua.reserve_attempt(_request(data_root))
    ua.release_attempt(reservation)
    ledger = data_root / ua.LEDGER_REL
    row = {
        "seq": 3,
        "ts": "2026-01-01T00:00:00Z",
        "attempt_id": "tail-attempt",
        "kind": "attempt",
        "state": "reserved",
        "reservation_upper_bound_usd": 1.0,
        field: value,
    }
    with ledger.open("a") as handle:
        handle.write(json.dumps(row) + "\n")

    projection = ua.usage_projection(data_root)
    assert projection["integrity_degraded"] is True
    assert projection["cost_final"] is False
    assert projection["attempt_counts"]["released"] == 1


def test_quarantined_dispatch_tail_makes_replay_evidence_degraded(data_root):
    reservation = ua.reserve_attempt(_request(data_root, task_id="replay-risk"))
    ledger = data_root / ua.LEDGER_REL
    corrupt_dispatch = {
        **_ledger(data_root)[-1],
        "seq": 2,
        "state": "dispatched",
        "prompt_tokens": "torn",
    }
    with ledger.open("a") as handle:
        handle.write(json.dumps(corrupt_dispatch) + "\n")

    evidence = ua.usage_breakdown(data_root, task_id="replay-risk")
    assert evidence["physical_calls"] == 0
    assert evidence["integrity_degraded"] is True
    ua.release_attempt(reservation)

    lines = ledger.read_text().splitlines()
    ledger.write_text(lines[0] + "\nnot-json\n" + lines[1] + "\n")
    with pytest.raises(ua.UsageLedgerCorrupt):
        ua.usage_projection(data_root)


def test_structurally_invalid_final_row_is_quarantined_but_midstream_is_fatal(data_root):
    reservation = ua.reserve_attempt(_request(data_root))
    ua.release_attempt(reservation)
    ledger = data_root / ua.LEDGER_REL
    bad = {
        "seq": 999,
        "kind": "attempt",
        "attempt_id": "bad-tail",
        "state": "dispatched",
    }
    with ledger.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(bad) + "\n")

    assert ua.usage_projection(data_root)["attempt_counts"] == {"released": 1}
    assert all(row.get("attempt_id") != "bad-tail" for row in _ledger(data_root))
    assert (data_root / ua.QUARANTINE_REL).is_file()

    lines = ledger.read_text().splitlines()
    bad["seq"] = 2
    ledger.write_text(lines[0] + "\n" + json.dumps(bad) + "\n" + lines[1] + "\n")
    with pytest.raises(ua.UsageLedgerCorrupt):
        ua.usage_projection(data_root)


def test_concurrent_writers_keep_monotonic_sequence(data_root):
    def one(index):
        reservation = ua.reserve_attempt(_request(data_root, task_id=f"t{index}"))
        ua.mark_dispatched(reservation)
        ua.settle_attempt(reservation, cost_usd=0.01, cost_final=True)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(one, range(16)))
    rows = _ledger(data_root)
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))
    assert ua.usage_projection(data_root)["settled_usd"] == 0.16


def test_known_reservation_is_checked_before_dispatch(data_root):
    first = ua.reserve_attempt(_request(data_root, reservation_usd=0.6, global_limit_usd=1.0))
    with pytest.raises(ua.BudgetExceeded):
        ua.reserve_attempt(_request(data_root, reservation_usd=0.5, global_limit_usd=1.0))
    assert [row["state"] for row in _ledger(data_root)] == ["reserved"]
    ua.release_attempt(first)


@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.5", 8.01278),
        ("openai/gpt-5.5-pro", 48.07668),
    ],
)
def test_long_context_reservation_uses_provider_pricing_tier(data_root, model, expected):
    reservation = ua.reserve_attempt(_request(
        data_root,
        model=model,
        provider="openrouter",
        reservation_usd=None,
        prompt_tokens_estimate=460_332,
        max_completion_tokens=65_536,
    ))
    row = _ledger(data_root)[-1]
    assert row["reservation_basis"] == "linear_pricing"
    assert row["reservation_upper_bound_usd"] == expected
    # Exact provider-confirmed receipt from the v6.64 smoke that exposed the
    # missing >272k OpenRouter tier.
    assert row["reservation_upper_bound_usd"] >= 5.120415
    ua.release_attempt(reservation)


@pytest.mark.parametrize(
    "provider,model",
    [("openai", "openai::gpt-5.5"), ("openrouter", "openai/gpt-5.5")],
)
def test_openai_family_reservation_margin_selects_long_tier(provider, model, data_root):
    reservation = ua.reserve_attempt(_request(
        data_root,
        model=model,
        provider=provider,
        reservation_usd=None,
        # ceil(247272 * 1.10) == the provider's 272k tier boundary.
        prompt_tokens_estimate=247_272,
        max_completion_tokens=0,
    ))
    row = _ledger(data_root)[-1]
    assert row["reservation_upper_bound_usd"] == 2.72
    ua.release_attempt(reservation)


def test_explicit_reservation_is_not_inflated_by_tokenizer_margin(data_root):
    reservation = ua.reserve_attempt(_request(
        data_root,
        model="openai/gpt-5.5-pro",
        provider="openrouter",
        reservation_usd=2.5,
        prompt_tokens_estimate=460_332,
        max_completion_tokens=65_536,
    ))
    assert _ledger(data_root)[-1]["reservation_upper_bound_usd"] == 2.5
    ua.release_attempt(reservation)

    opaque = ua.reserve_attempt(_request(
        data_root,
        model="openai/gpt-5.5-pro",
        provider="openrouter",
        reservation_usd=None,
        max_budget_usd=3.25,
        prompt_tokens_estimate=460_332,
        max_completion_tokens=65_536,
    ))
    assert _ledger(data_root)[-1]["reservation_upper_bound_usd"] == 3.25
    ua.release_attempt(opaque)


def test_openai_margin_changes_hold_not_actual_settlement(data_root):
    reservation = ua.reserve_attempt(_request(
        data_root,
        model="openai/gpt-5.5",
        provider="openrouter",
        reservation_usd=None,
        prompt_tokens_estimate=460_332,
        max_completion_tokens=65_536,
    ))
    ua.mark_dispatched(reservation)
    ua.settle_attempt(
        reservation,
        {"prompt_tokens": 477_909, "completion_tokens": 7_585},
        cost_usd=5.120415,
        cost_final=True,
    )
    row = _ledger(data_root)[-1]
    assert row["cost_usd"] == 5.120415
    assert row["reservation_upper_bound_usd"] == 8.01278


def test_scope_runtime_limit_is_enforced_without_provider_retry(data_root):
    sends = 0

    def send():
        nonlocal sends
        sends += 1

    scope = ua.UsageScope(drive_root=data_root, global_limit_usd=0.5)
    with ua.usage_scope(scope), pytest.raises(ua.BudgetExceeded):
        ua.execute_physical_attempt(_request(data_root, reservation_usd=0.6), send)
    assert sends == 0


def test_llm_retry_machine_does_not_classify_budget_rail_as_provider_failure(data_root, monkeypatch):
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="unused")
    sends = 0

    def create(**kwargs):
        nonlocal sends
        sends += 1

    def forbidden(*args, **kwargs):
        raise AssertionError("local accounting rail reached provider retry logic")

    monkeypatch.setattr(client, "_retry_without_optional_sampling", forbidden)
    monkeypatch.setattr(client, "_openrouter_signature_retry_kwargs", forbidden)
    monkeypatch.setattr(client, "_reroute_kwargs_for_body_error", forbidden)
    target = {
        "provider": "openai",
        "usage_model": "openai/gpt-5.2",
        "resolved_model": "gpt-5.2",
    }
    with ua.usage_scope(ua.UsageScope(drive_root=data_root, global_limit_usd=0)):
        with pytest.raises(ua.BudgetExceeded):
            client._create_chat_completion_with_retries(
                create,
                {"model": "gpt-5.2", "messages": [{"role": "user", "content": "x"}], "max_tokens": 10},
                target,
            )
    assert sends == 0


def test_web_search_does_not_cascade_on_accounting_rail(data_root, monkeypatch):
    from ouroboros.tools import search

    class Ctx:
        task_id = "t"
        task_metadata = {"budget_drive_root": str(data_root)}

    monkeypatch.setenv("OUROBOROS_WEBSEARCH_BACKEND", "openrouter")
    monkeypatch.setattr(
        search,
        "_web_search_openrouter",
        lambda *args, **kwargs: (_ for _ in ()).throw(ua.BudgetExceeded("rail")),
    )
    with pytest.raises(ua.BudgetExceeded):
        search._web_search(Ctx(), "query")


def test_unknown_pricing_is_not_reported_as_zero(data_root, monkeypatch):
    monkeypatch.setenv("TOTAL_BUDGET", "0")
    reservation = ua.reserve_attempt(
        _request(
            data_root,
            model="unknown/vendor-model",
            reservation_usd=None,
            max_completion_tokens=100,
        )
    )
    ua.mark_dispatched(reservation)
    ua.settle_attempt(reservation, {})
    projection = ua.usage_projection(data_root)
    assert projection["settled_usd"] == 0
    assert projection["unresolved_upper_bound_usd"] == 0
    assert projection["unknown_unmetered"] == 1
    assert projection["cost_final"] is False


def test_opaque_operation_without_max_budget_reserves_unknown(data_root, monkeypatch):
    monkeypatch.setenv("TOTAL_BUDGET", "0")
    reservation = ua.reserve_attempt(
        _request(
            data_root,
            model="anthropic/claude-opus-4.8",
            reservation_usd=None,
            prompt_tokens_estimate=1000,
            force_unknown_reservation=True,
        )
    )
    row = _ledger(data_root)[-1]
    assert row["reservation_upper_bound_usd"] is None
    assert row["pricing_known"] is False
    assert row["reservation_basis"] == "opaque_unknown"
    projection = ua.usage_projection(data_root)
    assert projection["reserved_usd"] == 0
    assert projection["unknown_unmetered"] == 1
    ua.release_attempt(reservation)


def test_unknown_pricing_is_fail_closed_under_finite_global_or_root_limit(data_root):
    with pytest.raises(ua.BudgetExceeded) as exc_info:
        ua.reserve_attempt(_request(
            data_root, model="unknown/vendor-model", reservation_usd=None,
        ))
    assert exc_info.value.limit_scope == "global"
    assert _ledger(data_root) == []

    with pytest.raises(ua.BudgetExceeded) as exc_info:
        ua.reserve_attempt(_request(
            data_root,
            model="unknown/vendor-model",
            reservation_usd=None,
            global_limit_usd=float("inf"),
            root_limit_usd=2.0,
        ))
    assert exc_info.value.limit_scope == "root"
    assert _ledger(data_root) == []


def test_zero_bound_cannot_dispatch_after_finite_limit_is_reached(data_root):
    first = ua.reserve_attempt(_request(data_root, reservation_usd=1.0, global_limit_usd=1.0))
    ua.mark_dispatched(first)
    ua.settle_attempt(first, {}, cost_usd=1.0, cost_final=True)

    with pytest.raises(ua.BudgetExceeded):
        ua.reserve_attempt(_request(
            data_root, task_id="next", reservation_usd=None,
            max_budget_usd=0.0, global_limit_usd=1.0,
        ))


def test_legacy_state_projection_cannot_regress_under_reordered_writers(
    data_root, monkeypatch,
):
    from supervisor import state

    state.init(data_root, total_budget_limit=0.0)
    first_started = threading.Event()
    release_first = threading.Event()
    calls = []

    def breakdown(_root):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(2.0)
            value = 1.0
        else:
            value = 2.0
        return {
            "accounted_usd": value, "physical_calls": int(value),
            "prompt_tokens": int(value), "completion_tokens": 0, "cached_tokens": 0,
            "settled_usd": value, "confirmed_usd": value, "estimated_usd": 0.0,
            "reserved_usd": 0.0, "unresolved_upper_bound_usd": 0.0,
            "unknown_unmetered": 0, "cost_final": True, "attempt_counts": {},
        }

    monkeypatch.setattr(ua, "ensure_legacy_imported", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(ua, "usage_breakdown", breakdown)
    older = threading.Thread(target=state.update_budget_from_usage, args=({},))
    newer = threading.Thread(target=state.update_budget_from_usage, args=({},))
    older.start()
    assert first_started.wait(2.0)
    newer.start()
    time.sleep(0.1)
    assert calls == [1]
    release_first.set()
    older.join(2.0)
    newer.join(2.0)

    assert calls == [1, 2]
    assert state.load_state()["spent_usd"] == 2.0


def test_legacy_import_is_resumable_and_preserves_delta(data_root):
    events = data_root / "logs" / "events.jsonl"
    events.parent.mkdir(parents=True)
    usage = {
        "type": "llm_usage",
        "ts": "2026-01-01T00:00:00Z",
        "task_id": "t",
        "model": "openai/gpt-5.2",
        "provider": "openai",
        "prompt_tokens": 10,
        "completion_tokens": 2,
        "cost": 0.1,
    }
    events.write_text(
        "\n".join(
            (
                json.dumps(usage),
                json.dumps(usage),
                json.dumps({"type": "llm_round", "ts": "2026-01-01T00:00:01Z", "task_id": "ambiguous"}),
            )
        )
        + "\n"
    )
    (data_root / "state" / "state.json").write_text(
        json.dumps(
            {
                "spent_usd": 0.4,
                "spent_calls": 3,
            }
        )
    )
    settings = data_root / "settings.json"
    settings.write_text('{"secret":"unchanged"}\n')
    before = settings.read_bytes()

    first = ua.ensure_legacy_imported(data_root)
    row_count = len(_ledger(data_root))
    second = ua.ensure_legacy_imported(data_root)

    assert first["legacy_usage_count"] == 1
    assert first["legacy_metadata_count"] == 2
    assert first["legacy_delta_usd"] == 0.3
    assert first["legacy_baseline_source"] == "state.json"
    assert second == first
    assert len(_ledger(data_root)) == row_count
    projection = ua.usage_projection(data_root)
    assert projection["settled_usd"] == 0.4
    assert projection["unknown_unmetered"] == 2
    assert projection["attempt_counts"]["metadata_only"] == 2
    assert settings.read_bytes() == before
    manifests = list((data_root / "archive" / "usage_import").glob("*/sha256.json"))
    assert len(manifests) == 1
    archive = manifests[0].parent
    archived_hashes = json.loads(manifests[0].read_text())
    for name, expected in first["source_sha256"].items():
        assert archived_hashes[name] == expected
        if expected and name != "settings.json":
            assert hashlib.sha256((archive / name).read_bytes()).hexdigest() == expected
    assert not (archive / "settings.json").exists()
    assert first["quarantined_test_operator_rows"] == 0
    assert first["test_operator_quarantine_policy"] == "typed_evidence_only_no_inference"


def test_completed_import_is_immutable_without_a_second_repair_api(data_root):
    events = data_root / "logs" / "events.jsonl"
    events.parent.mkdir(parents=True)
    usage_rows = [
        {
            "type": "llm_usage",
            "task_id": f"t{index}",
            "model": "openai/gpt-5.2",
            "provider": "openai",
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "cost": 0.1,
        }
        for index in range(2)
    ]
    events.write_text("\n".join(json.dumps(row) for row in usage_rows) + "\n")
    (data_root / "state" / "state.json").write_text(
        json.dumps({"spent_usd": 0, "spent_calls": 1})
    )
    (data_root / "settings.json").write_text('{"secret":"unchanged"}\n')

    incomplete = ua.ensure_legacy_imported(data_root)
    original_ledger = (data_root / ua.LEDGER_REL).read_bytes()
    original_watermark = (data_root / ua.IMPORT_REL).read_bytes()
    assert incomplete["legacy_baseline_source"] == "state.json"
    assert incomplete["legacy_usage_count"] == 2
    assert incomplete["legacy_metadata_count"] == 0

    assert ua.ensure_legacy_imported(data_root) == incomplete
    assert (data_root / ua.LEDGER_REL).read_bytes() == original_ledger
    assert (data_root / ua.IMPORT_REL).read_bytes() == original_watermark


def test_concurrent_legacy_importers_share_one_exact_snapshot(data_root, monkeypatch):
    events = data_root / "logs" / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(
        json.dumps(
            {
                "type": "llm_usage",
                "task_id": "t",
                "model": "openai/gpt-5.2",
                "provider": "openai",
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "cost": 0.1,
            }
        )
        + "\n"
    )
    (data_root / "state" / "state.json").write_text(json.dumps({"spent_usd": 0.1, "spent_calls": 1}))
    (data_root / "settings.json").write_text('{"secret":"unchanged"}\n')

    original = ua._legacy_snapshot
    calls = 0
    calls_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def snapshot(root):
        nonlocal calls
        assert not (root / "state" / "usage_attempts.lock").exists()
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return original(root)

    def import_once(_index):
        barrier.wait()
        return ua.ensure_legacy_imported(data_root)

    monkeypatch.setattr(ua, "_legacy_snapshot", snapshot)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(import_once, range(4)))

    assert calls == 1
    assert all(result == results[0] for result in results)
    assert results[0]["legacy_usage_count"] == 1
    assert len(_ledger(data_root)) == 1


def test_actor_limit_blocks_third_retry_before_provider_send(data_root, monkeypatch):
    from ouroboros.llm import LLMClient

    client = LLMClient(api_key="unused")
    sends = 0

    def create(**kwargs):
        nonlocal sends
        sends += 1
        raise RuntimeError(f"provider failure {sends}")

    monkeypatch.setattr(
        client,
        "_retry_without_optional_sampling",
        lambda kwargs, model, exc: {**kwargs, "temperature": None},
    )
    monkeypatch.setattr(
        client,
        "_openrouter_signature_retry_kwargs",
        lambda target, kwargs, exc: {**kwargs, "messages": []},
    )
    target = {
        "provider": "openai",
        "usage_model": "openai/gpt-5.2",
        "resolved_model": "gpt-5.2",
    }
    with ua.physical_attempt_limit(2), pytest.raises(ua.PhysicalAttemptLimitExceeded):
        client._create_chat_completion_with_retries(
            create,
            {"model": "gpt-5.2", "messages": [{"role": "user", "content": "x"}]},
            target,
        )

    assert sends == 2
    assert ua.usage_projection(data_root)["attempt_counts"] == {
        "unresolved": 2,
        "released": 1,
    }


def test_env_zero_is_unbounded_but_explicit_zero_is_a_hard_rail(data_root, monkeypatch):
    monkeypatch.setenv("TOTAL_BUDGET", "0")
    request = ua.AttemptRequest(
        model="local/test",
        provider="local",
        drive_root=data_root,
    )
    reservation = ua.reserve_attempt(request)
    ua.release_attempt(reservation)
    assert "limit_usd" not in ua.usage_projection(data_root)

    with pytest.raises(ua.BudgetExceeded) as exc_info:
        ua.reserve_attempt(
            ua.AttemptRequest(
                model="local/test",
                provider="local",
                drive_root=data_root,
                global_limit_usd=0,
            )
        )
    assert exc_info.value.limit_scope == "global"


def test_explicit_zero_root_limit_blocks_only_that_root(data_root, monkeypatch):
    monkeypatch.setenv("TOTAL_BUDGET", "0")
    with pytest.raises(ua.BudgetExceeded) as exc_info:
        ua.reserve_attempt(
            ua.AttemptRequest(
                model="local/test",
                provider="local",
                drive_root=data_root,
                task_id="task-a",
                root_task_id="root-a",
                root_limit_usd=0,
            )
        )
    assert exc_info.value.limit_scope == "root"
    assert exc_info.value.root_task_id == "root-a"


def test_claude_sdk_reserves_max_budget_and_settles_actual(data_root, monkeypatch):
    from ouroboros.gateways import claude_code as cc

    class Result:
        session_id = "session"
        total_cost_usd = 0.12
        usage = {"input_tokens": 20, "output_tokens": 4}
        subtype = "success"

    class Client:
        def __init__(self, options):
            self.options = options

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield Result()

    monkeypatch.setattr(cc, "ClaudeAgentOptions", lambda **kwargs: kwargs)
    monkeypatch.setattr(cc, "ClaudeSDKClient", Client)
    monkeypatch.setattr(cc, "ResultMessage", Result)

    result = asyncio.run(cc._run_edit_async("do work", str(data_root), budget=2.0))
    assert result.success is True
    assert result.usage["ledger_attempt_ids"]
    projection = ua.usage_projection(data_root)
    assert projection["confirmed_usd"] == 0.12
    rows = _ledger(data_root)
    assert rows[0]["reservation_upper_bound_usd"] == 2.0
