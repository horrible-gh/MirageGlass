"""Exercise panel controls, reclaimed preview space, and saved layout choices."""

from itertools import product
import re

import httpx
from playwright.sync_api import expect, sync_playwright

from test_library_connected_flow import (
    _capture,
    _create_deck,
    _launch_browser,
    _wait_until,
    connected_server,
)


PANELS = (
    ("sidebar", "sidebar-toggle", "sidebar-close"),
    ("rail", "rail-toggle", "rail-close"),
    ("version-section", "history-toggle", "history-close"),
)


def _ready_deck(base_url, token, key):
    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    ) as client:
        deck = _create_deck(client, "Panel controls", key)
        _wait_until(
            lambda: client.get(f"/api/v1/decks/{deck['id']}/versions").json()
            ["items"][0]["status"] == "ready",
            "Panel test deck did not become ready",
            timeout=20,
        )
        return deck


def _assert_panel_states(page, hidden):
    for (panel, toggle, _), is_hidden in zip(PANELS, hidden):
        if is_hidden:
            expect(page.locator(f"#{panel}")).to_be_hidden()
        else:
            expect(page.locator(f"#{panel}")).to_be_visible()
        expect(page.locator(f"#{toggle}")).to_be_visible()
        expect(page.locator(f"#{toggle}")).to_have_attribute(
            "aria-expanded", str(not is_hidden).lower()
        )
    dimensions = page.evaluate("""() => {
        const width = id => document.getElementById(id).getBoundingClientRect().width;
        return {
            preview: width('stage'),
            expected: innerWidth - width('sidebar') - width('rail') - width('version-section'),
            page: document.documentElement.scrollWidth,
            viewport: innerWidth,
        };
    }""")
    assert abs(dimensions["preview"] - dimensions["expected"]) <= 1, dimensions
    assert dimensions["page"] <= dimensions["viewport"], dimensions


def test_panels_hide_independently_restore_and_survive_reload(connected_server):
    base_url, token = connected_server
    deck = _ready_deck(base_url, token, "panel-desktop")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
        expect(page.locator("#frame-a")).to_have_attribute("src", re.compile(r"/v1/"))
        _assert_panel_states(page, (False, False, False))
        _capture(page, "panels-expanded")

        # Toggling panels must not reload or change the active preview.
        frame = page.locator("#frame-a")
        frame.evaluate("frame => frame.contentWindow.panelProbe = 'still-open'")
        frame_url = frame.get_attribute("src")
        previous = (False, False, False)
        for hidden in product((False, True), repeat=3):
            for (_, toggle, _), before, after in zip(PANELS, previous, hidden):
                if before != after:
                    page.locator(f"#{toggle}").click()
            _assert_panel_states(page, hidden)
            assert frame.get_attribute("src") == frame_url
            assert frame.evaluate("frame => frame.contentWindow.panelProbe") == "still-open"
            previous = hidden

        _capture(page, "panels-hidden")
        page.reload(wait_until="networkidle")
        _assert_panel_states(page, (True, True, True))

        # Every panel has its own close action and a reachable way back.
        for index, (_, toggle, close) in enumerate(PANELS):
            page.locator(f"#{toggle}").click()
            hidden = [True, True, True]
            hidden[index] = False
            _assert_panel_states(page, hidden)
            page.locator(f"#{close}").click()
            _assert_panel_states(page, (True, True, True))
            expect(page.locator(f"#{toggle}")).to_be_focused()

        # Navigating through the library must not reopen hidden viewer panels.
        page.locator("#to-library").click()
        expect(page.locator("#library")).to_be_visible()
        expect(page.locator("#sidebar")).to_be_hidden()
        page.locator(f'.lib-card[data-deck-id="{deck["id"]}"] .lib-card-name').click()
        expect(page.locator("#app")).to_be_visible()
        _assert_panel_states(page, (True, True, True))

        # Saving an expanded choice also survives a refresh.
        for _, toggle, _ in PANELS:
            page.locator(f"#{toggle}").click()
        page.reload(wait_until="networkidle")
        _assert_panel_states(page, (False, False, False))
        for width in (1024, 768):
            page.set_viewport_size({"width": width, "height": 900})
            _assert_panel_states(page, (False, False, False))
            for _, toggle, close in PANELS:
                for control in (toggle, close):
                    bounds = page.locator(f"#{control}").bounding_box()
                    assert bounds and bounds["x"] >= 0
                    assert bounds["x"] + bounds["width"] <= width
        assert not errors, errors
        browser.close()


def test_mobile_drawer_does_not_overwrite_desktop_panel_choices(connected_server):
    base_url, token = connected_server
    deck = _ready_deck(base_url, token, "panel-mobile")
    with sync_playwright() as playwright:
        browser = _launch_browser(playwright)
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
        page.locator("#rail-close").click()
        page.locator("#history-close").click()

        for desktop_hidden in (False, True):
            if desktop_hidden:
                page.locator("#sidebar-close").click()
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#sidebar")).to_be_hidden()
            expect(page.locator("#mobile-menu")).to_be_visible()
            expect(page.locator("#mobile-menu")).to_have_attribute("aria-expanded", "false")

            for close_action in ("button", "escape", "shade", "navigate"):
                page.locator("#mobile-menu").click()
                expect(page.locator("#sidebar")).to_be_visible()
                expect(page.locator("#mobile-menu")).to_have_attribute("aria-expanded", "true")
                expect(page.locator("#mobile-shade")).to_be_visible()
                if close_action == "button":
                    page.locator("#sidebar-close").click()
                    expect(page.locator("#mobile-menu")).to_be_focused()
                elif close_action == "escape":
                    page.locator("#sidebar-close").focus()
                    page.keyboard.press("Escape")
                    expect(page.locator("#mobile-menu")).to_be_focused()
                elif close_action == "shade":
                    page.locator("#mobile-shade").click(position={"x": 350, "y": 400})
                else:
                    page.locator("#nav-all").click()
                    expect(page.locator("#library")).to_be_visible()
                expect(page.locator("#sidebar")).to_be_hidden()
                expect(page.locator("#mobile-shade")).to_be_hidden()
                expect(page.locator("#mobile-menu")).to_have_attribute("aria-expanded", "false")

            page.locator(f'.lib-card[data-deck-id="{deck["id"]}"] .lib-card-name').click()
            expect(page.locator("#app")).to_be_visible()
            page.locator("#history-toggle").click()
            expect(page.locator("#version-section")).to_be_visible()
            page.locator("#history-close").click()
            expect(page.locator("#version-section")).to_be_hidden()
            page.locator("#rail-toggle").click()
            expect(page.locator("#rail")).to_be_visible()
            page.locator("#rail-close").click()
            expect(page.locator("#rail")).to_be_hidden()
            page.reload(wait_until="networkidle")
            expect(page.locator("#sidebar")).to_be_hidden()
            expect(page.locator("#rail")).to_be_hidden()
            expect(page.locator("#version-section")).to_be_hidden()
            page.set_viewport_size({"width": 1440, "height": 900})
            _assert_panel_states(page, (desktop_hidden, True, True))
        browser.close()
