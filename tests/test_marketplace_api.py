"""Tests for the ClawHub marketplace HTTP API adapter layer."""

from __future__ import annotations

import asyncio
import json

from ouroboros import marketplace_api
from ouroboros.marketplace.clawhub import ClawHubSkillSummary


class _Request:
    def __init__(self, query_params):
        self.query_params = query_params


def _json_response_payload(response):
    return json.loads(response.body.decode("utf-8"))


def test_marketplace_api_search_drops_params_with_query(monkeypatch):
    captured = {}

    def _fake_search(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return {
            "results": [],
            "next_cursor": "",
            "path": "search",
            "attempts": [],
        }

    monkeypatch.setattr(marketplace_api, "_registry_search", _fake_search)
    response = asyncio.run(
        marketplace_api.api_marketplace_search(
            _Request(
                {
                    "q": "deep research",
                    "limit": "7",
                    "offset": "50",
                    "cursor": "abc",
                    "official": "1",
                }
            )
        )
    )

    assert response.status_code == 200
    assert captured["query"] == "deep research"
    assert captured["kwargs"]["limit"] == 7
    assert "offset" not in captured["kwargs"]
    assert captured["kwargs"]["cursor"] is None
    assert captured["kwargs"]["official_only"] is False
    assert captured["kwargs"]["timeout_sec"] == 15
    assert "enrich_search_results" not in captured["kwargs"]
    payload = _json_response_payload(response)
    assert payload["official"] is True
    assert payload["offset"] == 0
    assert payload["cursor"] is None
    assert payload["registry_path"] == "search"


def test_marketplace_api_search_filters_official_after_enrichment(monkeypatch):
    def _fake_search(query, **_kwargs):
        return {
            "results": [
                ClawHubSkillSummary(slug="official", badges={"official": True}),
                ClawHubSkillSummary(slug="community", badges={}),
            ],
            "next_cursor": "",
            "path": "search",
            "attempts": [],
        }

    monkeypatch.setattr(marketplace_api, "_registry_search", _fake_search)
    response = asyncio.run(
        marketplace_api.api_marketplace_search(
            _Request({"q": "deep research", "official": "1"})
        )
    )

    assert response.status_code == 200
    payload = _json_response_payload(response)
    assert payload["official"] is True
    assert [r["slug"] for r in payload["results"]] == ["official"]


def test_marketplace_api_browse_keeps_official_and_cursor(monkeypatch):
    captured = {}

    def _fake_search(query, **kwargs):
        captured["query"] = query
        captured["kwargs"] = kwargs
        return {
            "results": [],
            "next_cursor": "next",
            "path": "packages",
            "attempts": [],
        }

    monkeypatch.setattr(marketplace_api, "_registry_search", _fake_search)
    response = asyncio.run(
        marketplace_api.api_marketplace_search(
            _Request({"limit": "5", "cursor": "abc", "official": "1"})
        )
    )

    assert response.status_code == 200
    assert captured["query"] == ""
    assert captured["kwargs"]["limit"] == 5
    assert captured["kwargs"]["cursor"] == "abc"
    assert captured["kwargs"]["official_only"] is True
    assert captured["kwargs"]["timeout_sec"] == 5
    payload = _json_response_payload(response)
    assert payload["official"] is True
    assert payload["cursor"] == "abc"
    assert payload["next_cursor"] == "next"
