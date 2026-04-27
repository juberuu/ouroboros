"""Unit tests for ouroboros.marketplace.clawhub HTTP client.

Mocks ``urllib.request.urlopen`` to test URL construction, registry-host
allowlist enforcement, response cap enforcement, and JSON decoding edge
cases without ever touching the real network.
"""

from __future__ import annotations

import io
import json
from unittest import mock

import pytest

from ouroboros.marketplace import clawhub as clawhub_mod
from ouroboros.marketplace.clawhub import (
    ClawHubArchive,
    ClawHubClientError,
    ClawHubClientHostBlocked,
    download,
    info,
    search,
)


def _mock_response(body: bytes, *, status: int = 200, headers: dict | None = None):
    """Return a context-manager-compatible mock that mimics urlopen()."""
    response = mock.MagicMock()
    response.status = status
    response.getcode.return_value = status
    response.headers = headers or {"Content-Type": "application/json"}
    # Stream in two chunks to exercise the chunk-loop in ``_http_get``.
    chunks = [body[: max(1, len(body) // 2)], body[max(1, len(body) // 2) :], b""]
    iter_chunks = iter(chunks)
    response.read = lambda _n=64 * 1024: next(iter_chunks, b"")
    cm = mock.MagicMock()
    cm.__enter__.return_value = response
    cm.__exit__.return_value = False
    return cm


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def _patch_opener(body, *, status=200, headers=None):
    """Patch the marketplace opener so urlopen-style mocks still work.

    v4.50 swapped the bare ``urllib.request.urlopen`` call for a custom
    opener that re-validates the redirect host on every hop. The
    unit tests don't care about the redirect handler — they only need
    a deterministic response — so we patch ``_OPENER.open`` directly.
    """
    return mock.patch.object(
        clawhub_mod._OPENER, "open", return_value=_mock_response(body, status=status, headers=headers)
    )


def test_search_returns_summaries(monkeypatch):
    body = json.dumps(
        {
            "items": [
                {
                    "name": "skill1",
                    "displayName": "Skill 1",
                    "description": "First skill",
                    "latestVersion": "1.0.0",
                    "license": "MIT",
                    "stats": {"downloads": 1234, "stars": 42},
                    "metadata": {"openclaw": {"os": ["darwin"]}},
                },
                {
                    "name": "skill2",
                    "displayName": "Skill 2",
                    "description": "Second",
                    "latestVersion": "2.0.0",
                    "stats": {"downloads": 7},
                    "family": "code-plugin",
                },
            ]
        }
    ).encode("utf-8")
    with _patch_opener(body) as opener_mock:
        results = search("foo", limit=5)
    opener_mock.assert_called_once()
    request = opener_mock.call_args.args[0]
    assert "/packages/search?" in request.full_url
    assert "family=skill" in request.full_url
    assert "q=foo" in request.full_url
    assert [r.slug for r in results] == ["skill1", "skill2"]
    assert results[0].license == "MIT"
    assert results[0].os_list == ["darwin"]
    assert results[1].is_plugin is True


def test_search_handles_bare_array(monkeypatch):
    body = json.dumps([{"slug": "a", "latestVersion": "1.0.0"}]).encode("utf-8")
    with _patch_opener(body):
        results = search("")
    assert len(results) == 1
    assert results[0].slug == "a"


def test_search_handles_items_and_cursor_metadata(monkeypatch):
    body = json.dumps(
        {"items": [{"slug": "owner/cursor", "latestVersion": "1.0.0"}], "nextCursor": "abc"}
    ).encode("utf-8")
    with _patch_opener(body):
        page = search("", cursor="start", include_metadata=True)
    assert [r.slug for r in page["results"]] == ["owner/cursor"]
    assert page["next_cursor"] == "abc"
    assert page["path"] == "packages"


def test_browse_uses_canonical_packages_endpoint(monkeypatch):
    body = json.dumps({"items": [{"slug": "owner/pkg"}], "nextCursor": ""}).encode("utf-8")
    with _patch_opener(body) as opener_mock:
        page = search("", include_metadata=True)
    request = opener_mock.call_args.args[0]
    assert "/packages?" in request.full_url
    assert "/packages/search?" not in request.full_url
    assert "family=skill" in request.full_url
    assert [r.slug for r in page["results"]] == ["owner/pkg"]
    assert page["path"] == "packages"


def test_search_empty_returns_empty_metadata(monkeypatch):
    empty = json.dumps({"items": [], "nextCursor": None}).encode("utf-8")
    with _patch_opener(empty):
        page = search("", include_metadata=True)
    assert page["results"] == []
    assert page["path"] == "packages"
    assert page["attempts"][0]["ok"] is True


def test_search_forwards_official_filter(monkeypatch):
    body = json.dumps({"items": []}).encode("utf-8")
    with _patch_opener(body) as opener_mock:
        search("", official_only=True)
    request = opener_mock.call_args.args[0]
    assert "isOfficial=true" in request.full_url


def test_search_skips_malformed_records(monkeypatch):
    body = json.dumps(
        {
            "skills": [
                "not-a-dict",
                {"slug": "owner/good"},
            ]
        }
    ).encode("utf-8")
    with _patch_opener(body):
        results = search("")
    assert [r.slug for r in results] == ["owner/good"]


def test_search_invalid_json_raises_client_error():
    with _patch_opener(b"not json"):
        with pytest.raises(ClawHubClientError):
            search("foo")


# ---------------------------------------------------------------------------
# Host allowlist
# ---------------------------------------------------------------------------


def test_evil_host_is_blocked():
    with pytest.raises(ClawHubClientHostBlocked):
        clawhub_mod._registry_base_url("https://evil.example.com/api")


def test_http_only_blocked_for_non_localhost():
    with pytest.raises(ClawHubClientHostBlocked):
        clawhub_mod._registry_base_url("http://clawhub.ai/api")


def test_localhost_http_allowed_for_dev():
    assert clawhub_mod._registry_base_url("http://localhost:8081/api/v1").startswith("http://localhost")


def test_clawhub_ai_default():
    url = clawhub_mod._registry_base_url(None)
    assert url == "https://clawhub.ai/api/v1"


# ---------------------------------------------------------------------------
# info / download
# ---------------------------------------------------------------------------


def test_info_unwraps_top_level_skill(monkeypatch):
    body = json.dumps(
        {
            "package": {"name": "owner/x", "tags": {"latest": "1.2.2"}},
            "latestVersion": {"version": "1.2.3"},
        }
    ).encode("utf-8")
    with _patch_opener(body) as opener_mock:
        summary = info("owner/x")
    request = opener_mock.call_args.args[0]
    assert "/packages/owner/x" in request.full_url
    assert summary.latest_version == "1.2.3"


def test_info_blank_slug_rejected():
    with pytest.raises(ClawHubClientError):
        info("")


@pytest.mark.parametrize("bad_slug", ["../etc", "../../etc", "foo/../bar", "./x"])
def test_info_rejects_traversal_slugs(bad_slug):
    """v4.50: slugs with `..` / `.` segments are rejected before the URL is built."""
    with pytest.raises(ClawHubClientError, match="must not contain"):
        info(bad_slug)


def test_download_returns_archive_with_sha(monkeypatch):
    body = b"PKzipfilebytes" + b"\0" * 100
    with _patch_opener(body, headers={"content-type": "application/zip"}) as opener_mock:
        archive = download("owner/x", version="1.0.0")
    request = opener_mock.call_args.args[0]
    assert "/download?" in request.full_url
    assert "slug=owner%2Fx" in request.full_url
    assert "version=1.0.0" in request.full_url
    assert isinstance(archive, ClawHubArchive)
    assert archive.slug == "owner/x"
    assert archive.version == "1.0.0"
    assert archive.content == body
    assert len(archive.sha256) == 64
    assert archive.content_type.startswith("application/zip")


def test_response_size_cap_enforced(monkeypatch):
    """Massive payload should raise rather than allocate."""
    huge = b"x" * (clawhub_mod._MAX_JSON_RESPONSE_BYTES + 100)
    with _patch_opener(huge):
        with pytest.raises(ClawHubClientError):
            search("foo")


def test_redirect_to_evil_host_is_blocked():
    """v4.50 fix: a 30x Location pointing outside the allowlist must raise."""
    handler = clawhub_mod._AllowlistRedirectHandler()
    fake_req = mock.MagicMock()
    fake_fp = mock.MagicMock()
    fake_headers = {"Location": "https://evil.example.com/x"}
    with pytest.raises(clawhub_mod.ClawHubClientHostBlocked):
        handler.redirect_request(
            fake_req, fake_fp, 302, "Found", fake_headers, "https://evil.example.com/x"
        )


def test_redirect_to_allowed_host_is_followed():
    """A redirect within the allowlist should not be blocked at the host check."""
    handler = clawhub_mod._AllowlistRedirectHandler()
    fake_req = mock.MagicMock()
    fake_fp = mock.MagicMock()
    fake_headers = {}
    # The base implementation may still return None for unsupported
    # status codes; we only need to confirm we don't raise on
    # allowed hosts.
    try:
        handler.redirect_request(
            fake_req, fake_fp, 302, "Found", fake_headers, "https://www.clawhub.ai/x"
        )
    except clawhub_mod.ClawHubClientHostBlocked:
        pytest.fail("Allowed host was incorrectly blocked")
    except Exception:
        # Other exceptions from the parent class for missing args are fine
        # — what matters is that it's NOT ClawHubClientHostBlocked.
        pass


def test_clawhub_com_host_no_longer_allowed():
    """v4.50 removed clawhub.com from the host allowlist (ownership unverified)."""
    with pytest.raises(clawhub_mod.ClawHubClientHostBlocked):
        clawhub_mod._registry_base_url("https://clawhub.com/api/v1")


def test_registry_url_strips_query_strings():
    """v4.50 fix: query strings on the registry URL must be discarded."""
    from ouroboros.config import get_clawhub_registry_url
    import os

    os.environ["OUROBOROS_CLAWHUB_REGISTRY_URL"] = "https://clawhub.ai/api/v1?key=foo"
    try:
        url = get_clawhub_registry_url()
        assert url == "https://clawhub.ai/api/v1"
    finally:
        os.environ.pop("OUROBOROS_CLAWHUB_REGISTRY_URL", None)
