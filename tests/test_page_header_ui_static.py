from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_shared_page_header_helper_has_no_inline_styles():
    source = _read("web/modules/page_header.js")

    assert "export function renderPageHeader" in source
    assert "export function renderTabStrip" in source
    assert "style=" not in source
    assert "app-page-header" in source
    assert "app-tab-strip" in source


def test_primary_pages_use_shared_header_helper():
    for rel in [
        "web/modules/settings_ui.js",
        "web/modules/dashboard.js",
        "web/modules/skills.js",
        "web/modules/widgets.js",
        "web/modules/files.js",
        "web/modules/chat.js",
    ]:
        source = _read(rel)
        assert "page_header.js" in source
        assert "renderPageHeader" in source

