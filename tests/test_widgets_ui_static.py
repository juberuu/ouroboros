"""Static contract checks for the Widgets page renderer."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _widgets_js() -> str:
    return (REPO_ROOT / "web" / "modules" / "widgets.js").read_text(
        encoding="utf-8"
    )


def test_widgets_support_declarative_schema_components():
    source = _widgets_js()
    assert "render.kind === 'declarative'" in source
    for marker in [
        "type === 'form'",
        "type === 'action'",
        "type === 'poll'",
        "type === 'subscription'",
        "type === 'kv'",
        "type === 'table'",
        "type === 'markdown'",
        "type === 'json'",
        "['image', 'audio', 'video', 'file'].includes(type)",
        "type === 'gallery'",
        "type === 'progress'",
    ]:
        assert marker in source
    assert "rememberFormValues();" in source
    assert "formValues[idx][field.name] = fieldValue(form, field);" in source
    assert "String(optValue) === String(saved ?? '')" in source
    assert "component.auto_start === true" in source
    assert "queueMicrotask(() => startPoll(idx));" in source
    assert "boundedNumber(spec.interval_ms, 2000, 1000, 30000)" in source
    assert "disposeMountedWidgets();" in source
    assert "timers.forEach((timer) => clearTimeout(timer));" in source
    assert "const controller = new AbortController();" in source
    assert "controllers.forEach((controller) => controller.abort());" in source
    assert "widgetMessageHandlers.add(handler);" in source
    assert "ctx.ws.on('message'" in source
    assert "msg?.type !== expectedType" in source
    assert "event.detail?.page === 'widgets'" in source
    assert "disposeMountedWidgets();" in source.split("window.addEventListener('ouro:page-shown'")[1]
    assert "let renderGeneration = 0;" in source
    assert "generation !== renderGeneration" in source
    assert "widgetsVisible = false;" in source
    assert "if (!widgetsVisible || generation !== renderGeneration) return;" in source


def test_widgets_escape_and_sanitize_untrusted_content():
    source = _widgets_js()
    assert "function renderMarkdownSafe" in source
    assert "DOMPurify.sanitize" in source
    assert "FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'input', 'img', 'video', 'audio', 'source']" in source
    assert "FORBID_ATTR: ['style', 'src', 'srcset', 'srcdoc']" in source
    assert "escapeHtml(JSON.stringify(value, null, 2))" in source
    assert "escapeHtml(getPath(row, c.path, ''))" in source


def test_widgets_media_sources_are_constrained_to_extension_routes_or_data_urls():
    source = _widgets_js()
    assert "function safeMediaSrc" in source
    assert "const route = spec.route || spec.api_route || '';" in source
    assert "extensionRouteUrl(tab, route, params)" in source
    assert "data:(image\\/" in source
    assert "parsed.pathname.startsWith(expectedPrefix)" in source
    assert "parsed.origin === window.location.origin" in source
    assert "javascript:" not in source


def test_widgets_treat_head_as_no_body_request():
    source = _widgets_js()
    assert "const noBody = method === 'GET' || method === 'HEAD';" in source
    assert "const init = noBody" in source


def test_widgets_keep_iframe_sandbox_locked_down():
    source = _widgets_js()
    assert 'sandbox=""' in source
    assert "allow-scripts" not in source


def test_widgets_use_design_radius_tokens():
    style = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    block_start = style.index(".widget-field input,")
    block_end = style.index("}", block_start)
    block = style[block_start:block_end]
    assert "border-radius: var(--radius-sm);" in block
    assert "border-radius: 9px;" not in block
