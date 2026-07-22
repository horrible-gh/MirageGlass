"""Capture the top screenful of index.html with Playwright.

- Fixed viewport (1280x800 by default), not full page, so tiles in the rail do
  not end up with wildly different heights.
- Waits for networkidle plus document.fonts.ready, then scrolls once to trigger
  lazy-loaded content before coming back up.
- A failure here is not an upload failure. It returns False and the UI falls
  back to a numbered card.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def capture(index_html: Path, out_png: Path, settings) -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright is not installed - continuing without a thumbnail.")
        return False

    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(
                    viewport={
                        "width": settings.capture_width,
                        "height": settings.capture_height,
                    }
                )
                page.goto(index_html.as_uri(), wait_until="networkidle",
                          timeout=settings.capture_timeout_ms)
                page.evaluate("() => document.fonts && document.fonts.ready")
                page.evaluate(
                    "() => window.scrollTo(0, document.body.scrollHeight)"
                )
                page.wait_for_timeout(300)
                page.evaluate("() => window.scrollTo(0, 0)")
                page.wait_for_timeout(200)
                page.screenshot(path=str(out_png), full_page=False)
            finally:
                browser.close()
        return out_png.is_file()
    except Exception as e:  # a capture failure must not turn into a registration failure
        logger.warning("Thumbnail capture failed (%s): %s", index_html, e)
        return False
