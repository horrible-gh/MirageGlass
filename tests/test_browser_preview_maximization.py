"""Verify tab-contained preview maximization and reversible layout behavior."""

import re

from playwright.sync_api import expect, sync_playwright

from test_panel_visibility import _ready_deck
from test_library_connected_flow import _capture, _launch_browser, connected_server


def _stage_bounds(page):
    bounds = page.locator("#stage").bounding_box()
    assert bounds is not None
    return bounds


def test_browser_fill_preserves_browser_chrome_preview_and_panels(connected_server):
    base_url, token = connected_server
    deck = _ready_deck(base_url, token, "tab-preview-maximize")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
        frame = page.locator("#frame-a")
        expect(frame).to_have_attribute("src", re.compile(r"/v1/"))
        frame_url = frame.get_attribute("src")
        frame.evaluate("element => element.contentWindow.previewProbe = 'preserved'")
        original = _stage_bounds(page)
        panels = {
            name: page.locator("#" + name).is_visible()
            for name in ("sidebar", "rail", "version-section")
        }
        # A call to the native API is an error even if the environment permits it.
        page.evaluate("""() => {
            document.querySelector('#stage').requestFullscreen = () => {
                throw Error('Browser fullscreen must not be requested');
            };
            document.exitFullscreen = () => {
                throw Error('Browser fullscreen must not be exited');
            };
        }""")
        button = page.locator("#fullscreen")
        button.click()
        expect(page.locator("#stage")).to_have_class(re.compile(r"browser-maximized"))
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(button).to_have_attribute("aria-label", page.evaluate("() => window.MG_STRINGS.restorePreview"))
        assert not page.evaluate("Boolean(document.fullscreenElement)")
        bounds = _stage_bounds(page)
        assert abs(bounds["x"]) <= 1 and abs(bounds["y"]) <= 1
        assert abs(bounds["width"] - page.viewport_size["width"]) <= 1
        assert abs(bounds["height"] - page.viewport_size["height"]) <= 1
        assert frame.get_attribute("src") == frame_url
        assert frame.evaluate("element => element.contentWindow.previewProbe") == "preserved"
        _capture(page, "tab-preview-maximized")

        # The button remains inside the tab and is the direct way back.
        button.click()
        expect(button).to_have_attribute("aria-pressed", "false")
        expect(button).to_have_attribute("aria-label", page.evaluate("() => window.MG_STRINGS.maximizePreview"))
        assert not page.locator("#stage").evaluate(
            "element => element.classList.contains('browser-maximized')"
        )
        restored = _stage_bounds(page)
        assert abs(restored["x"] - original["x"]) <= 1
        assert abs(restored["width"] - original["width"]) <= 1
        assert abs(restored["height"] - original["height"]) <= 1
        for name, was_visible in panels.items():
            assert page.locator("#" + name).is_visible() == was_visible
        assert frame.get_attribute("src") == frame_url
        assert frame.evaluate("element => element.contentWindow.previewProbe") == "preserved"

        button.click()
        page.keyboard.press("Escape")
        expect(button).to_have_attribute("aria-pressed", "false")
        assert _stage_bounds(page)["width"] == restored["width"]

        # A route change must never strand the library beneath a fixed preview.
        button.click()
        page.evaluate("() => { location.hash = ''; }")
        expect(page.locator("#library")).to_be_visible()
        assert not page.locator("#stage").evaluate(
            "element => element.classList.contains('browser-maximized')"
        )
        page.locator(f'.lib-card[data-deck-id="{deck["id"]}"] .lib-card-name').click()
        expect(page.locator("#app")).to_be_visible()
        expect(page.locator("#fullscreen")).to_have_attribute("aria-pressed", "false")
        assert not errors, errors
        browser.close()


def test_browser_fill_restores_responsive_viewer_at_small_width(connected_server):
    base_url, token = connected_server
    deck = _ready_deck(base_url, token, "tab-preview-narrow")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
        button = page.locator("#fullscreen")
        button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        bounds = _stage_bounds(page)
        assert abs(bounds["x"]) <= 1 and abs(bounds["y"]) <= 1
        assert abs(bounds["width"] - 390) <= 1
        assert abs(bounds["height"] - 844) <= 1
        assert not page.evaluate("Boolean(document.fullscreenElement)")
        button.click()
        expect(button).to_have_attribute("aria-pressed", "false")
        expect(page.locator("#mobile-menu")).to_be_visible()
        page.reload(wait_until="networkidle")
        expect(page.locator("#fullscreen")).to_have_attribute("aria-pressed", "false")
        browser.close()
