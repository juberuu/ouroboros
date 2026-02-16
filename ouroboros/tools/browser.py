"""
Browser automation tools via Playwright (sync API).

Provides browse_page (open URL, get content/screenshot)
and browser_action (click, fill, evaluate JS on current page).

Browser state lives in ToolContext (per-task lifecycle),
not module-level globals — safe across threads.
"""

from __future__ import annotations

import base64
import logging
import subprocess
import sys
from typing import Any, Dict, List

from ouroboros.tools.registry import ToolContext, ToolEntry

log = logging.getLogger(__name__)

_playwright_ready = False


def _ensure_playwright_installed():
    """Install Playwright and Chromium if not already available."""
    global _playwright_ready
    if _playwright_ready:
        return

    try:
        import playwright  # noqa: F401
    except ImportError:
        log.info("Playwright not found, installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "playwright"])

    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            pw.chromium.executable_path
        log.info("Playwright chromium binary found")
    except Exception:
        log.info("Installing Playwright chromium binary...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install-deps", "chromium"])

    _playwright_ready = True


def _ensure_browser(ctx: ToolContext):
    """Create or reuse browser for this task. State lives in ctx."""
    if ctx._browser is not None:
        try:
            if ctx._browser.is_connected():
                return ctx._page
        except Exception:
            pass
        # Browser died — clean up and recreate
        cleanup_browser(ctx)

    _ensure_playwright_installed()

    from playwright.sync_api import sync_playwright
    ctx._pw_instance = sync_playwright().start()
    ctx._browser = ctx._pw_instance.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    ctx._page = ctx._browser.new_page(
        viewport={"width": 1280, "height": 720},
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    ctx._page.set_default_timeout(30000)
    return ctx._page


def cleanup_browser(ctx: ToolContext) -> None:
    """Close browser and playwright. Called by agent.py in finally block."""
    try:
        if ctx._page is not None:
            ctx._page.close()
    except Exception:
        pass
    try:
        if ctx._browser is not None:
            ctx._browser.close()
    except Exception:
        pass
    try:
        if ctx._pw_instance is not None:
            ctx._pw_instance.stop()
    except Exception:
        pass
    ctx._page = None
    ctx._browser = None
    ctx._pw_instance = None


def _browse_page(ctx: ToolContext, url: str, output: str = "text",
                 wait_for: str = "", timeout: int = 30000) -> str:
    page = _ensure_browser(ctx)
    page.goto(url, timeout=timeout, wait_until="domcontentloaded")

    if wait_for:
        page.wait_for_selector(wait_for, timeout=timeout)

    if output == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        ctx._last_screenshot_b64 = b64
        return (
            f"Screenshot captured ({len(b64)} bytes base64). "
            f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
        )
    elif output == "html":
        html = page.content()
        return html[:50000] + ("... [truncated]" if len(html) > 50000 else "")
    elif output == "markdown":
        text = page.evaluate("""() => {
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
        text = page.inner_text("body")
        return text[:30000] + ("... [truncated]" if len(text) > 30000 else "")


def _browser_action(ctx: ToolContext, action: str, selector: str = "",
                    value: str = "", timeout: int = 5000) -> str:
    page = _ensure_browser(ctx)

    if action == "click":
        if not selector:
            return "Error: selector required for click"
        page.click(selector, timeout=timeout)
        page.wait_for_timeout(500)
        return f"Clicked: {selector}"
    elif action == "fill":
        if not selector:
            return "Error: selector required for fill"
        page.fill(selector, value, timeout=timeout)
        return f"Filled {selector} with: {value}"
    elif action == "select":
        if not selector:
            return "Error: selector required for select"
        page.select_option(selector, value, timeout=timeout)
        return f"Selected {value} in {selector}"
    elif action == "screenshot":
        data = page.screenshot(type="png", full_page=False)
        b64 = base64.b64encode(data).decode()
        ctx._last_screenshot_b64 = b64
        return (
            f"Screenshot captured ({len(b64)} bytes base64). "
            f"Call send_photo(image_base64='__last_screenshot__') to deliver it to the owner."
        )
    elif action == "evaluate":
        if not value:
            return "Error: value (JS code) required for evaluate"
        result = page.evaluate(value)
        out = str(result)
        return out[:20000] + ("... [truncated]" if len(out) > 20000 else "")
    elif action == "scroll":
        direction = value or "down"
        if direction == "down":
            page.evaluate("window.scrollBy(0, 600)")
        elif direction == "up":
            page.evaluate("window.scrollBy(0, -600)")
        elif direction == "top":
            page.evaluate("window.scrollTo(0, 0)")
        elif direction == "bottom":
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        return f"Scrolled {direction}"
    else:
        return f"Unknown action: {action}. Use: click, fill, select, screenshot, evaluate, scroll"


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="browse_page",
            schema={
                "name": "browse_page",
                "description": (
                    "Open a URL in headless browser. Returns page content as text, "
                    "html, markdown, or screenshot (base64 PNG). "
                    "Browser persists across calls within a task. "
                    "For screenshots: use send_photo tool to deliver the image to owner."
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
            timeout_sec=60,
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
            timeout_sec=60,
        ),
    ]
