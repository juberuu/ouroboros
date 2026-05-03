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
        "type === 'code'",
        "type === 'chart'",
        "type === 'tabs'",
        "type === 'stream'",
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
    assert "new EventSource(url)" in source
    assert "eventSources.forEach((source) => source.close());" in source
    assert "new Chart(canvas, config)" in source
    assert "chartInstances.forEach((chart) => chart.destroy());" in source
    assert "data-widget-tab-key" in source
    assert "component.job === true || component.mode === 'job'" in source
    assert "startJobPoll" in source
    assert "status_route" in source
    assert "event.detail?.page === 'widgets'" in source
    page_shown_branch = source.split("window.addEventListener('ouro:page-shown'")[1]
    assert "disposeMountedWidgets();" in page_shown_branch
    assert "let renderGeneration = 0;" in source
    assert "generation !== renderGeneration" in source
    assert "widgetsVisible = false;" in source
    assert "if (!widgetsVisible || generation !== renderGeneration) return;" in source
    assert "let widgetsMounted = false;" in source
    assert "if (widgetsMounted && !force) return;" in source
    assert "widgetsMounted = false;" in page_shown_branch


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


def test_widgets_downloads_use_host_handler_not_navigation():
    source = _widgets_js()
    assert "data-widget-download-url" in source
    assert "event.preventDefault();" in source
    assert "download_file_to_downloads" in source
    assert "URL.createObjectURL(blob)" in source
    assert "window.location.href" not in source
    assert "window.location.assign" not in source
    assert '<a class="btn btn-default" href' not in source


def test_widgets_treat_head_as_no_body_request():
    source = _widgets_js()
    assert "const noBody = method === 'GET' || method === 'HEAD';" in source
    assert "const init = noBody" in source


def test_widgets_keep_iframe_sandbox_locked_down():
    """The legacy ``kind: "iframe"`` widget surface mounts an extension
    route inside a <iframe> with the *empty* sandbox attribute (no
    permissions at all). v5.7.0 added ``kind: "module"``, which mounts
    extension-supplied JS inside a separate <iframe srcdoc> with
    ``sandbox="allow-scripts"`` BUT no ``allow-same-origin`` token —
    so the iframe is still an opaque origin (no SPA cookie / storage
    access) and is further constrained by a strict CSP. We check both
    invariants here:

    1. The legacy iframe path still uses the empty sandbox.
    2. The module iframe path adds ``allow-scripts`` but never adds
       ``allow-same-origin`` (the only token that would re-expose
       parent storage).
    """
    source = _widgets_js()
    assert 'sandbox=""' in source
    # ``allow-scripts`` is now legitimately present, but only inside the
    # ``kind === 'module'`` branch. The dangerous combined sandbox token
    # must never appear in an actual iframe attribute.
    assert 'sandbox="allow-scripts"' in source
    assert 'sandbox="allow-scripts allow-same-origin"' not in source
    assert 'sandbox="allow-scripts allow-forms allow-same-origin"' not in source
    assert "render.kind === 'module'" in source
    # Verify the module iframe carries a CSP that does NOT grant network
    # access directly. The parent injects a postMessage fetch bridge instead,
    # restricted to /api/extensions/<skill>/... from the parent side.
    assert "default-src 'none'" in source
    assert "script-src 'unsafe-inline'" in source
    assert "OuroborosWidget = { fetch: window.fetch }" in source
    assert "module widget fetch outside extension route prefix" in source


def test_widgets_use_design_radius_tokens():
    style = (REPO_ROOT / "web" / "style.css").read_text(encoding="utf-8")
    block_start = style.index(".widget-field input,")
    block_end = style.index("}", block_start)
    block = style[block_start:block_end]
    assert "border-radius: var(--radius-sm);" in block
    assert "border-radius: 9px;" not in block


def test_widgets_inline_card_preserves_session_state():
    source = _widgets_js()
    assert "const saved = widgetSessionState.get(persistenceKey) || {};" in source
    assert "const savedCity = escapeHtml(saved.city || 'Moscow');" in source
    assert "const savedResult = saved.resultHtml" in source
    assert "widgetSessionState.set(persistenceKey, { city: query, resultHtml: result.innerHTML });" in source
    assert "return () => {" in source


def test_widgets_v5_7_0_new_components_render():
    """v5.7.0 host-owned declarative components: ``map`` (Leaflet-ready
    fallback list), ``calendar`` (host SVG-style row list), ``kanban``
    (HTML5 drag with on_move POST). All three must be present in the
    declarative renderer so authors can reference them in widgets, and
    none of them may bring skill-supplied JS into the SPA origin."""
    source = _widgets_js()
    assert "type === 'map'" in source
    assert "type === 'calendar'" in source
    assert "type === 'kanban'" in source
    # Module / arbitrary <script> from the skill must NEVER be inserted
    # into the host origin. ``data-widget-map-config`` carries the spec
    # as JSON in a data attribute (host renders); no runtime eval of
    # extension JS is acceptable in any of the new component renderers.
    assert "data-widget-map-config" in source
    assert "widget-kanban-card" in source
