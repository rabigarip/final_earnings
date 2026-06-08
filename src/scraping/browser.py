"""
Headless browser fetch for JS-rendered pages (e.g. SCMP search).

Uses Playwright to load a URL, wait for content, and return the rendered HTML.
Fails gracefully if Playwright is not installed or browser is unavailable.
"""

from __future__ import annotations


def fetch_page_with_browser(
    url: str,
    *,
    user_agent: str | None = None,
    timeout_ms: int = 20000,
    wait_selector: str | None = None,
    wait_timeout_ms: int = 10000,
) -> str:
    """
    Load URL in headless Chromium and return the rendered HTML.
    Returns empty string on any failure (Playwright not installed, launch error, timeout).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ""

    ua = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=ua,
                    viewport={"width": 1280, "height": 720},
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.set_default_timeout(timeout_ms)
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_selector:
                    page.wait_for_selector(wait_selector, timeout=wait_timeout_ms)
                else:
                    # Give JS a moment to render search results
                    page.wait_for_timeout(2000)
                html = page.content()
                return html or ""
            finally:
                browser.close()
    except Exception:
        return ""
