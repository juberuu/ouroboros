"""Static contract checks for the ClawHub Marketplace UI module."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _marketplace_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "marketplace.js").read_text(
        encoding="utf-8"
    )


def test_marketplace_search_mode_hides_pagination_and_keeps_official_clickable():
    source = _marketplace_js()
    assert "const searchMode = Boolean(String(query || '').trim());" in source
    assert "if (searchMode || (!nextCursor && !hasPrevious))" in source
    assert "onlyOfficial.disabled = searchMode;" not in source
    assert "Filters enriched search results to skills marked official." in source


def test_marketplace_search_request_drops_browse_only_params():
    source = _marketplace_js()
    assert "const MARKETPLACE_SEARCH_LIMIT = 16;" in source
    assert "String(query ? MARKETPLACE_SEARCH_LIMIT : state.limit)" in source
    assert "if (!query && state.cursor) params.set('cursor', state.cursor);" in source
    assert "if (state.onlyOfficial) params.set('official', '1');" in source
    assert "params.set('offset'" not in source


def test_marketplace_browse_tracks_cursor_history_for_prev():
    source = _marketplace_js()
    assert "cursorHistory: []" in source
    assert "state.cursorHistory.push(state.cursor || '');" in source
    assert "state.cursor = state.cursorHistory.pop() || '';" in source
    assert "hasPrevious: state.cursorHistory.length > 0" in source


def test_marketplace_copy_names_current_clawhub_endpoints():
    source = _marketplace_js()
    assert "Browse uses <code>packages?family=skill</code>" in source
    assert "<code>search?q=...</code>" in source
    assert "packages/search?family=skill" not in source
