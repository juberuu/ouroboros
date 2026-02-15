"""
Browser automation tools via Playwright.

Provides browse_page (open URL, get content/screenshot)
and browser_action (click, fill, evaluate JS on current page).
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

# Module-level state: persistent browser/page within a task
_browser = None
_page = None


def _get_or_create_loop():
    """Get existing or create new event loop."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


async def _ensure_browser():
    """Ensure browser and page are ready."""
    global _browser, _page
    if _browser and _browser.is_connected():
        return _page

    from playwright.async_api import async_playwright
    pw = await async_playwright().start()
    _browser = await pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    _page = await _browser.new_page(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    return _page


async def _browse_page_async(url: str, output: str = "text",
                              wait_for: str = "", timeout: int = 30000) -> str:
    page = await _ensure_browser()
    await page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    if wait_for:
        await page.wait_for_selector(wait_for, timeout=timeout)

    if output == "screenshot":
        data = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(data).decode()
    elif output == "html":
        html = await page.content()
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    elif output == "markdown":
        text = await page.evaluate("""() => {
            const walk = (el) => {
                let out = '';
                for (const child of el.childNodes) {
                    if (child.nodeType === 3) {
                        const t = child.textContent.trim();
                        if (t) out += t + ' ';
                    } else if (child.nodeType === 1) {
                        const tag = child.tagName;
                        if (['SCRIPT','STYLE','NOSCRIPT'].includes(tag)) continue;
                        if (['H1','H2','H3','H4','H5','H6'].includes(tag))
                            out += '\\n' + '#'.repeat(parseInt(tag[1])) + ' ';
                        if (tag === 'P' || tag === 'DIV' || tag === 'BR') out += '\\n';
                        if (tag === 'LI') out += '\\n- ';
                        if (tag === 'A') out += '[';
                        out += walk(child);
                        if (tag === 'A') out += '](' + (child.href||'') + ')';
                    }
                }
                return out;
            };
            return walk(document.body);
        }""")
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")
    else:  # text
        text = await page.inner_text("body")
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")


async def _browser_action_async(action: str, selector: str = "",
                                 value: str = "", timeout: int = 5000) -> str:
    page = await _ensure_browser()

    if action == "click":
        if not selector:
            return "Error: selector required for click"
        await page.click(selector, timeout=timeout)
        await page.wait_for_timeout(500)
        return f"Clicked: {selector}"
    elif action == "fill":
        if not selector:
            return "Error: selector required for fill"
        await page.fill(selector, value, timeout=timeout)
        return f"Filled {selector} with: {value}"
    elif action == "select":
        if not selector:
            return "Error: selector required for select"
        await page.select_option(selector, value, timeout=timeout)
        return f"Selected {value} in {selector}"
    elif action == "screenshot":
        data = await page.screenshot(type="png", full_page=False)
        return base64.b64encode(data).decode()
    elif action == "evaluate":
        if not value:
            return "Error: value (JS code) required for evaluate"
        result = await page.evaluate(value)
        out = str(result)
        return out[:20000] + ("... [truncated]" if len(out) > 20000 else "")
    elif action == "scroll":
        direction = value or "down"
        if direction == "down":
            await page.evaluate("window.scrollBy(0, 600)")
        elif direction == "up":
            await page.evaluate("window.scrollBy(0, -600)")
        elif direction == "top":
            await page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return f"Scrolled {direction}"
    else:
        return f"Unknown action: {action}. Use: click, fill, select, screenshot, evaluate, scroll"


def _run_async(coro):
    """Run async coroutine from sync context."""
    loop = _get_or_create_loop()
    return loop.run_until_complete(coro)


def _browse_page(ctx: ToolContext, url: str, output: str = "text",
                 wait_for: str = "", timeout: int = 30000) -> str:
    return _run_async(_browse_page_async(url, output, wait_for, timeout))


def _browser_action(ctx: ToolContext, action: str, selector: str = "",
                    value: str = "", timeout: int = 5000) -> str:
    return _run_async(_browser_action_async(action, selector, value, timeout))


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": (
                    "Open a URL in headless browser. Returns page content as text, "
                    "html, markdown, or screenshot (base64 PNG). "
                    "Browser persists across calls within a task."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL to open"},
                        "output": {
                            "type": "string",
                            "enum": ["text", "html", "markdown", "screenshot"],
                            "description": "Output format (default: text)",
                        },
                        "wait_for": {
                            "type": "string",
                            "description": "CSS selector to wait for before extraction",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Page load timeout in ms (default: 30000)",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=_browse_page,
        ),
        ToolEntry(
            name="browser_action",
            schema={
                "name": "browser_action",
                "description": (
                    "Perform action on current browser page. Actions: "
                    "click (selector), fill (selector + value), select (selector + value), "
                    "screenshot (base64 PNG), evaluate (JS code in value), "
                    "scroll (value: up/down/top/bottom)."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["click", "fill", "select", "screenshot", "evaluate", "scroll"],
                            "description": "Action to perform",
                        },
                        "selector": {
                            "type": "string",
                            "description": "CSS selector for click/fill/select",
                        },
                        "value": {
                            "type": "string",
                            "description": "Value for fill/select, JS for evaluate, direction for scroll",
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "Action timeout in ms (default: 5000)",
                        },
                    },
                    "required": ["action"],
                },
            },
            handler=_browser_action,
        ),
    ]
