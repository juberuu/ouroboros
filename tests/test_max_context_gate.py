"""WS4: max context mode is hard-blocked unless the active route has confirmed/
acked >=1M Capability Evidence (fail-closed), with a route-scoped owner-ack escape."""

from __future__ import annotations


import pytest

import ouroboros.capability_evidence as ce
from ouroboros import config as cfg
from ouroboros.gateway import settings as gw


@pytest.fixture(autouse=True)
def _isolate_evidence_store(tmp_path, monkeypatch):
    """_max_context_block / record_owner_ack persist Capability Evidence under
    config.DATA_DIR; isolate it to a tmp dir so a bare local pytest run never
    writes data/state/capability_evidence.json under the live Ouroboros data root
    (triad finding). _max_context_block does `from ouroboros.config import DATA_DIR`
    at call time, so patching the module attribute is read by it."""
    store = tmp_path / "evidence-store"
    store.mkdir()
    monkeypatch.setattr(cfg, "DATA_DIR", store)


def test_max_gate_blocks_when_route_unprobeable(monkeypatch):
    # Force offline: no provider metadata -> unprobeable -> fail-closed.
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    block = gw._max_context_block({"OUROBOROS_MODEL": "anthropic/claude-opus-4-8"})
    assert block is not None
    assert "needs_ack" in block
    assert block["needs_ack"].get("model") == "anthropic/claude-opus-4-8"


def test_max_gate_allows_after_route_scoped_ack(monkeypatch):
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 0)
    settings = {"OUROBOROS_MODEL": "anthropic/claude-opus-4-8"}
    route = gw._active_main_route(settings)
    ce.record_owner_ack(
        cfg.DATA_DIR, provider=route["provider"], model=route["model"],
        base_url=route["base_url"], window_tokens=1_000_000,
    )
    assert gw._max_context_block(settings) is None
    # A DIFFERENT model is still blocked (ack is route-scoped, not repo-wide).
    other = gw._max_context_block({"OUROBOROS_MODEL": "anthropic/claude-opus-4-7"})
    assert other is not None


def test_max_gate_allows_when_metadata_confirms_1m(monkeypatch):
    monkeypatch.setattr(ce, "_provider_metadata_window", lambda *a, **k: 1_000_000)
    assert gw._max_context_block({"OUROBOROS_MODEL": "openai/gpt-5.5"}) is None


def test_max_gate_threads_compatible_key_only_for_compatible_route(monkeypatch):
    """The in-flight OPENAI_COMPATIBLE_API_KEY is threaded into the probe ONLY when the
    active route is openai-compatible. For any other provider the key must NOT reach the
    probe (else probe_oversized_context would overwrite that provider's resolved key with
    the compatible one on the generative probe path — cross-provider key bleed)."""
    seen = {}

    def _cap(provider, model, base_url, allow_fetch=True, api_key=None):
        seen["key"] = api_key
        return 1_000_000  # confirm >=1M so the gate returns cleanly, no generative/network

    monkeypatch.setattr(ce, "_provider_metadata_window", _cap)

    # openai-compatible route: the in-flight key IS threaded.
    assert gw._max_context_block(
        {"OUROBOROS_MODEL": "openai-compatible::llama-3", "OPENAI_COMPATIBLE_API_KEY": "THEKEY"}
    ) is None
    assert seen["key"] == "THEKEY"

    # A different-provider route carrying the SAME leftover key: NOT threaded.
    seen.clear()
    assert gw._max_context_block(
        {"OUROBOROS_MODEL": "openai/gpt-5.5", "OPENAI_COMPATIBLE_API_KEY": "THEKEY"}
    ) is None
    assert seen["key"] is None
