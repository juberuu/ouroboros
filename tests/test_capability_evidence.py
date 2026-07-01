"""Capability Evidence (v6.33.0 WS4): sourced, route-fingerprinted window proof."""

from __future__ import annotations



import ouroboros.capability_evidence as ce


def test_route_fingerprint_stable_and_route_sensitive():
    a = ce.route_fingerprint(provider="anthropic", model="claude-opus-4-8", options={"beta": "1m"})
    b = ce.route_fingerprint(provider="anthropic", model="claude-opus-4-8", options={"beta": "1m"})
    assert a == b
    # Any route change yields a new fingerprint.
    assert a != ce.route_fingerprint(provider="anthropic", model="claude-opus-4-8")  # no beta
    assert a != ce.route_fingerprint(provider="anthropic", model="claude-opus-4-7", options={"beta": "1m"})
    assert a != ce.route_fingerprint(provider="openai", model="claude-opus-4-8", options={"beta": "1m"})


def test_unknown_is_fail_closed(tmp_path):
    ev = ce.probe(tmp_path, provider="anthropic", model="claude-opus-4-8", allow_fetch=False)
    assert ev.status == ce.STATUS_UNPROBEABLE
    assert ce.confirms_at_least(ev, ce.ONE_MILLION) is False
    assert ce.confirms_at_least(None) is False


def test_owner_ack_is_asserted_and_route_scoped(tmp_path):
    ce.record_owner_ack(tmp_path, provider="anthropic", model="claude-opus-4-8",
                        window_tokens=1_000_000, options={"beta": "1m"}, note="beta header on")
    ev = ce.probe(tmp_path, provider="anthropic", model="claude-opus-4-8", options={"beta": "1m"}, allow_fetch=False)
    assert ev.status == ce.STATUS_ASSERTED
    assert ev.window_tokens == 1_000_000
    assert ce.confirms_at_least(ev) is True
    # The SAME model on a DIFFERENT route (no beta) is NOT covered by the ack.
    other = ce.probe(tmp_path, provider="anthropic", model="claude-opus-4-8", allow_fetch=False)
    assert other.status == ce.STATUS_UNPROBEABLE
    assert ce.confirms_at_least(other) is False


def test_confirmed_probe_below_threshold_fails_closed(tmp_path):
    # Seed a confirmed-but-small window via owner-ack (asserted counts as known).
    ce.record_owner_ack(tmp_path, provider="gigachat", model="GigaChat-3-Ultra", window_tokens=131_072)
    ev = ce.probe(tmp_path, provider="gigachat", model="GigaChat-3-Ultra", allow_fetch=False)
    assert ev.window_tokens == 131_072
    assert ce.confirms_at_least(ev, ce.ONE_MILLION) is False  # known but < 1M
    assert ce.confirms_at_least(ev, 131_072) is True


def test_revoke_owner_ack(tmp_path):
    ce.record_owner_ack(tmp_path, provider="openai", model="gpt-5.5", window_tokens=1_000_000)
    fp = ce.route_fingerprint(provider="openai", model="gpt-5.5")
    assert any(a["route_fp"] == fp for a in ce.list_owner_acks(tmp_path))
    assert ce.revoke_owner_ack(tmp_path, fp) is True
    ev = ce.probe(tmp_path, provider="openai", model="gpt-5.5", allow_fetch=False)
    assert ev.status == ce.STATUS_UNPROBEABLE


def test_local_health_confirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "_local_health_window", lambda model: 256_000)
    ev = ce.probe(tmp_path, provider="local", model="qwen", use_local=True, allow_fetch=True)
    assert ev.status == ce.STATUS_CONFIRMED
    assert ev.source == ce.SOURCE_LOCAL_HEALTH
    assert ev.window_tokens == 256_000


def test_provider_outage_keeps_prior_confirmed(tmp_path, monkeypatch):
    """A transient provider outage must NEVER erase a prior CONFIRMED record
    (module invariant) — it is kept, surfaced as stale (v6.33.0 P4)."""
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 1_000_000)
    ev1 = ce.probe(tmp_path, provider="openrouter", model="x/y", allow_fetch=True)
    assert ev1.status == ce.STATUS_CONFIRMED and ev1.window_tokens == 1_000_000
    # Provider now unreachable: metadata 0 + a transport failure.
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    monkeypatch.setattr(ce, "_metadata_fetch_transport_failed", lambda *a, **k: True)
    ev2 = ce.probe(tmp_path, provider="openrouter", model="x/y", allow_fetch=True, force=True)
    assert ev2.window_tokens == 1_000_000          # not erased
    assert ev2.status == ce.STATUS_CONFIRMED
    assert ev2.stale is True
    assert ce.confirms_at_least(ev2, ce.ONE_MILLION) is True


def test_transport_failure_records_status_failed(tmp_path, monkeypatch):
    """Provider unreachable (no prior record) -> STATUS_FAILED, distinct from a
    route with no metadata source. STATUS_FAILED is fail-closed for the >=1M gate."""
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    monkeypatch.setattr(ce, "_metadata_fetch_transport_failed", lambda *a, **k: True)
    ev = ce.probe(tmp_path, provider="openrouter", model="x/y", allow_fetch=True)
    assert ev.status == ce.STATUS_FAILED
    assert ce.confirms_at_least(ev, ce.ONE_MILLION) is False


def test_no_metadata_source_is_unprobeable_not_failed(tmp_path, monkeypatch):
    """A route with no metadata source and no outage stays UNPROBEABLE (owner-ack
    path), NOT FAILED — the two must not be conflated."""
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    monkeypatch.setattr(ce, "_metadata_fetch_transport_failed", lambda *a, **k: False)
    ev = ce.probe(tmp_path, provider="anthropic", model="claude-opus-4-8", allow_fetch=True)
    assert ev.status == ce.STATUS_UNPROBEABLE


def test_openrouter_metadata_retries_after_transport_failure(monkeypatch):
    """A failed /models fetch is NOT one-shot-poisoned: the next allow_fetch=True
    probe retries, and a model unresolved while the provider is unreachable reads
    as a transport failure (not silently unprobeable) (v6.33.0 triad fix)."""
    from ouroboros.llm import LLMClient

    monkeypatch.setattr(LLMClient, "_SUPPORTED_PARAMS_FETCHED", False)
    monkeypatch.setattr(LLMClient, "_CAPABILITIES_FETCH_OK", False)
    monkeypatch.setattr(LLMClient, "_CONTEXT_LENGTH_CACHE", {})
    state = {"n": 0}

    def fake_fetch():
        state["n"] += 1
        LLMClient._SUPPORTED_PARAMS_FETCHED = True
        if state["n"] == 1:
            LLMClient._CAPABILITIES_FETCH_OK = False  # provider unreachable
        else:
            LLMClient._CAPABILITIES_FETCH_OK = True
            LLMClient._CONTEXT_LENGTH_CACHE["x/y"] = 1_000_000

    monkeypatch.setattr(LLMClient, "_fetch_openrouter_capabilities", fake_fetch)
    # 1st probe: fetch attempted but failed -> 0 + transport-failed signalled.
    assert LLMClient.openrouter_context_length("x/y") == 0
    assert state["n"] == 1
    assert LLMClient.metadata_fetch_attempted_and_failed() is True
    # 2nd probe: retries (the prior fetch failed), provider recovered, model present.
    assert LLMClient.openrouter_context_length("x/y") == 1_000_000
    assert state["n"] == 2
    assert LLMClient.metadata_fetch_attempted_and_failed() is False


# --- CW6: OpenAI-compatible /models metadata probe (vLLM/Ollama/...) ---

def test_openai_compatible_metadata_window_parses_max_model_len(monkeypatch):

    import ouroboros.config as cfg

    monkeypatch.setattr(cfg, "load_settings", lambda: {"OPENAI_COMPATIBLE_API_KEY": "k"})

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [
                {"id": "other-model", "max_model_len": 8192},
                {"id": "my-model", "max_model_len": 1048576},
            ]}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())

    win = ce._openai_compatible_metadata_window("my-model", "http://localhost:8000/v1", allow_fetch=True)
    assert win == 1048576
    # The saved model is normally provider-prefixed; /models lists the BARE id (CW6 fix).
    win_prefixed = ce._openai_compatible_metadata_window(
        "openai-compatible::my-model", "http://localhost:8000/v1", allow_fetch=True)
    assert win_prefixed == 1048576


def test_openai_compatible_metadata_window_fail_closed(monkeypatch):
    import httpx

    # hot path (allow_fetch=False) and no base_url => no network, 0.
    assert ce._openai_compatible_metadata_window("m", "http://x/v1", allow_fetch=False) == 0
    assert ce._openai_compatible_metadata_window("m", "", allow_fetch=True) == 0

    # transport error => fail-closed to 0 (never raises).
    def _boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("ouroboros.config.load_settings", lambda: {})
    monkeypatch.setattr(httpx, "get", _boom)
    assert ce._openai_compatible_metadata_window("m", "http://x/v1", allow_fetch=True) == 0


def test_provider_metadata_window_routes_openai_compatible(monkeypatch):
    seen = {}

    def _fake(model, base_url, allow_fetch, api_key=None):
        seen["hit"] = (model, base_url)
        return 4096

    monkeypatch.setattr(ce, "_openai_compatible_metadata_window", _fake)
    win = ce._provider_metadata_window("openai-compatible", "m", "http://x/v1", allow_fetch=True)
    assert win == 4096 and seen["hit"] == ("m", "http://x/v1")
    # gigachat stays unprobeable (no per-model window in its /models)
    assert ce._provider_metadata_window("gigachat", "GigaChat", "", allow_fetch=True) == 0


def test_probe_threads_api_key_through_metadata_and_generative(tmp_path, monkeypatch):
    """First-run onboarding passes the in-flight OPENAI_COMPATIBLE_API_KEY to
    probe(api_key=...) because it is not yet on disk. Confirm the key reaches BOTH
    the metadata probe and (on fall-through) the generative probe for an
    openai-compatible route."""
    seen = {}

    def _fake_meta(model, base_url, allow_fetch, api_key=None):
        seen["meta_key"] = api_key
        return 0  # force fall-through to the generative probe

    def _fake_gen(provider, model, base_url="", api_key=None):
        seen["gen_key"] = api_key
        return 0, ce.STATUS_UNPROBEABLE, "stub"

    monkeypatch.setattr(ce, "_openai_compatible_metadata_window", _fake_meta)
    monkeypatch.setattr(ce, "_generative_probe_window", _fake_gen)
    ce.probe(
        tmp_path, provider="openai-compatible", model="m", base_url="http://x/v1",
        allow_fetch=True, allow_generative=True, force=True, api_key="THEKEY",
    )
    assert seen["meta_key"] == "THEKEY"
    assert seen["gen_key"] == "THEKEY"


# --- v6.46.0: generative context-window probe + cloudru base_url fix ---

def test_classify_generative_probe_response_no_network():
    from ouroboros.capability_evidence import (
        classify_generative_probe_response as C,
        STATUS_CONFIRMED, STATUS_UNPROBEABLE, STATUS_FAILED,
    )
    # 4xx overflow reject WITH a parseable limit -> CONFIRMED at that number (free path).
    win, st, _ = C(400, "This model's maximum context length is 1048576 tokens. However you requested 2000000 tokens")
    assert (win, st) == (1048576, STATUS_CONFIRMED)
    win, st, _ = C(400, "Input length (160062 tokens) is longer than the model's context length (59862 tokens)")
    assert (win, st) == (59862, STATUS_CONFIRMED)
    # 4xx WITHOUT a number (e.g. Zhipu code 1261, or a 413 size reject) -> owner-ack.
    assert C(400, "error code 1261 prompt too long")[1] == STATUS_UNPROBEABLE
    # 200: the oversized input was ACCEPTED (possibly paid) -> never auto-confirm; owner-ack.
    assert C(200, "", canaries=["A"], echoed_text="A", usage_prompt_tokens=100, sent_token_estimate=100)[1] == STATUS_UNPROBEABLE
    # transport / 5xx / unknown -> transient FAILED.
    assert C(503, "bad gateway")[1] == STATUS_FAILED
    assert C(None, "timeout")[1] == STATUS_FAILED


def test_active_main_route_sets_cloudru_base_url():
    from ouroboros.gateway.settings import _active_main_route
    route = _active_main_route({
        "OUROBOROS_MODEL": "cloudru::glm-5.2",
        "CLOUDRU_FOUNDATION_MODELS_BASE_URL": "https://foundation-models.api.cloud.ru/v1",
    })
    assert route["provider"] == "cloudru"
    # The bug was base_url='' for cloudru (only openai/openai-compatible/gigachat set it).
    assert route["base_url"] == "https://foundation-models.api.cloud.ru/v1"
