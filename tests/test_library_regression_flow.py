"""Connected browser regression coverage for T0014: console session
auto-reestablish, DnD self-drop no-op, Viewer header zoom, and Viewer rail
group scoping.

This reuses the same real-uvicorn-server + Playwright pattern as
test_library_connected_flow.py (see that file for the fixture/helper
rationale) instead of duplicating it - only the console-write-disabled
server needs its own fixture, since none of the other scenarios here need
that extra environment variable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

from conftest import PROJECT_ROOT
from test_library_connected_flow import (
    _card,
    _create_deck,
    _create_group,
    _dom_section_ids,
    _free_port,
    _move_deck,
    _section_ids,
    _wait_for_section,
    _wait_until,
    _zip,
    connected_server,
)


@pytest.fixture
def console_write_disabled_server(tmp_path: Path):
    """Same shape as connected_server, but MIRAGEGLASS_CONSOLE_WRITE=false, so
    issue_console_session() always deletes the cookie and require_management_write()
    always rejects the console-session path (server/auth.py)."""
    storage = tmp_path / "console-disabled-storage"
    port = _free_port()
    token = "console-disabled-token"
    environment = os.environ.copy()
    environment.update(
        {
            "MIRAGEGLASS_STORAGE": str(storage),
            "MIRAGEGLASS_TOKEN": token,
            "MIRAGEGLASS_CONSOLE_WRITE": "false",
            "MIRAGEGLASS_CAPTURE_TIMEOUT_MS": "3000",
            "PYTHONUNBUFFERED": "1",
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "server.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        def healthy() -> bool:
            if process.poll() is not None:
                return False
            return httpx.get(base_url + "/healthz", timeout=0.5).status_code == 200

        _wait_until(healthy, "console-write-disabled server did not start", timeout=20)
        yield base_url, token
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.returncode not in {0, 1, -15}:
            output = process.stdout.read() if process.stdout else ""
            pytest.fail(f"console-write-disabled server exited with {process.returncode}: {output}")


def _no_native_dialog_guard(page: Page, seen: list[str]) -> None:
    """A4: browser alert/confirm/prompt must never fire. Registering this handler
    also lets a stray native dialog be dismissed instead of hanging the test."""
    page.on("dialog", lambda dialog: (seen.append(dialog.type), dialog.dismiss()))


def _rail_deck_ids(page: Page) -> list[str]:
    return page.locator("#deck-list > .deck-item").evaluate_all(
        "items => items.map(item => item.dataset.deckId)"
    )


# ---------------------------------------------------------------------------
# D1 / D2 - DnD self-drop is a no-op (T0014 section 2 / 6)
# ---------------------------------------------------------------------------

def test_deck_self_drop_is_noop(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group = _create_group(client, "Self Drop Group")
        deck_a = _create_deck(client, "Deck A", "self-a")
        deck_b = _create_deck(client, "Deck B", "self-b")
        deck_c = _create_deck(client, "Deck C", "self-c")
        for index, deck in enumerate([deck_a, deck_b, deck_c]):
            _move_deck(client, deck["id"], group["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            move_requests: list[str] = []

            def record(request) -> None:
                if request.method == "POST" and request.url.endswith("/move"):
                    move_requests.append(request.url)

            page.on("request", record)
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            expected_order = [deck_a["id"], deck_b["id"], deck_c["id"]]
            assert _section_ids(client.get("/api/v1/library").json(), group["id"]) == expected_order

            # Real pointer-driven drag of B onto itself.
            before = list(move_requests)
            _card(page, deck_b["id"]).drag_to(_card(page, deck_b["id"]))
            page.wait_for_timeout(250)
            assert move_requests == before, "Self-drop must send zero organization mutation requests"
            assert _section_ids(client.get("/api/v1/library").json(), group["id"]) == expected_order
            assert _dom_section_ids(page, group["id"]) == expected_order

            # Exercise dropDeck() directly with a forged self-target - the exact call
            # shape a native drop on the source card would produce (targetId ===
            # draggedId). This pins the fix to the function T0014 2 names as the
            # single source of truth, independent of whatever the browser's own
            # dragover default-prevention quirks do when the pointer never really
            # leaves the source card during a real mouse drag.
            before = list(move_requests)
            page.evaluate(
                """({ groupId, deckId }) => {
                    const card = document.querySelector(
                        `.lib-card[data-deck-id="${deckId}"][data-organizable="true"]`
                    );
                    window.startLibraryDrag(
                        { preventDefault(){}, dataTransfer: { setData(){} } },
                        { kind: 'deck', id: deckId },
                        card
                    );
                    window.dropDeck(
                        { preventDefault(){}, stopPropagation(){} }, groupId, deckId, 'after'
                    );
                }""",
                {"groupId": group["id"], "deckId": deck_b["id"]},
            )
            page.wait_for_timeout(250)
            assert move_requests == before, "dropDeck() self-target must not dispatch a move"
            assert _section_ids(client.get("/api/v1/library").json(), group["id"]) == expected_order
            assert _dom_section_ids(page, group["id"]) == expected_order
            assert not page.locator("#library").evaluate("el => el.classList.contains('dragging')")

            context.close()
            browser.close()


def test_group_self_drop_is_noop(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        _create_group(client, "Group One")
        group_2 = _create_group(client, "Group Two")
        _create_group(client, "Group Three")
        # create_group() always inserts at the front, so read the resulting order
        # back instead of assuming it - T0014 does not change group creation order.
        expected_order = [item["id"] for item in client.get("/api/v1/library").json()["groups"]]
        assert group_2["id"] in expected_order

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            move_requests: list[str] = []

            def record(request) -> None:
                if request.method == "POST" and request.url.endswith("/move"):
                    move_requests.append(request.url)

            page.on("request", record)
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            handle = page.locator(f'.lib-group[data-group-id="{group_2["id"]}"] .lib-group-drag-handle')
            before = list(move_requests)
            handle.drag_to(handle)
            page.wait_for_timeout(250)
            assert move_requests == before, "Self-drop must send zero organization mutation requests"
            current_order = [item["id"] for item in client.get("/api/v1/library").json()["groups"]]
            assert current_order == expected_order
            assert page.locator("#lib-groups > .lib-group").evaluate_all(
                "groups => groups.map(group => group.dataset.groupId)"
            ) == expected_order + [""]

            # Exercise dropGroup() directly with a forged self-target, mirroring the
            # deck-level check above.
            before = list(move_requests)
            page.evaluate(
                """(groupId) => {
                    const handle = document.querySelector(
                        `.lib-group[data-group-id="${groupId}"] .lib-group-drag-handle`
                    );
                    window.startLibraryDrag(
                        { preventDefault(){}, dataTransfer: { setData(){} } },
                        { kind: 'group', id: groupId },
                        handle.closest('.lib-group')
                    );
                    window.dropGroup(
                        { preventDefault(){}, stopPropagation(){} }, groupId, 'after'
                    );
                }""",
                group_2["id"],
            )
            page.wait_for_timeout(250)
            assert move_requests == before, "dropGroup() self-target must not dispatch a move"
            current_order = [item["id"] for item in client.get("/api/v1/library").json()["groups"]]
            assert current_order == expected_order

            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# A1-A4 - console session auto-reestablish on 401, exactly one retry (T0014 1)
# ---------------------------------------------------------------------------

def test_console_session_silent_reestablish_retries_once(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context()
            page = context.new_page()
            page_errors: list[str] = []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            native_dialogs: list[str] = []
            _no_native_dialog_guard(page, native_dialogs)
            responses: list[tuple[str, str, int]] = []

            def record_response(response) -> None:
                responses.append((response.request.method, response.url, response.status))

            page.on("response", record_response)

            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()
            cookies = context.cookies(base_url)
            assert any(c["name"] == "mirageglass_console" for c in cookies), (
                "Console session cookie was not issued on first load"
            )

            # Simulate the console session having expired: drop the cookie
            # client-side. Server-side it is indistinguishable from a real TTL
            # expiry - both make require_management_write() see no usable cookie
            # and answer 401 (server/auth.py).
            context.clear_cookies()
            page.evaluate("window.__noReloadMarker = true;")
            before_index = len(responses)

            page.locator("#lib-new-group").click()
            page.locator("#dialog-input").fill("Retry Group")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.locator(".lib-group-name", has_text="Retry Group")).to_be_visible()

            window = responses[before_index:]
            group_posts = [r for r in window if r[0] == "POST" and r[1].endswith("/api/v1/groups")]
            root_gets = [r for r in window if r[0] == "GET" and r[1] == base_url + "/"]
            assert [status for (_, _, status) in group_posts] == [401, 201], (
                f"Expected exactly one 401 then one successful retry, got {group_posts}"
            )
            assert len(root_gets) == 1, f"Expected exactly one reestablish GET /, got {root_gets}"

            assert page.evaluate("window.__noReloadMarker") is True, "Reestablish must not reload the page"
            groups = client.get("/api/v1/library").json()["groups"]
            assert len([g for g in groups if g["name"] == "Retry Group"]) == 1, "Mutation must not be duplicated"
            assert native_dialogs == [], "Must never use a native alert/confirm/prompt"
            assert page_errors == []

            context.close()
            browser.close()


def test_console_session_reestablish_failure_ends_in_custom_dialog(console_write_disabled_server):
    base_url, token = console_write_disabled_server
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context()
        page = context.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        native_dialogs: list[str] = []
        _no_native_dialog_guard(page, native_dialogs)
        responses: list[tuple[str, str, int]] = []

        def record_response(response) -> None:
            responses.append((response.request.method, response.url, response.status))

        page.on("response", record_response)

        page.goto(base_url + "/", wait_until="networkidle")
        expect(page.locator("#library")).to_be_visible()
        # MIRAGEGLASS_CONSOLE_WRITE=false: issue_console_session() deletes the
        # cookie instead of issuing one (server/auth.py).
        cookies = context.cookies(base_url)
        assert not any(c["name"] == "mirageglass_console" for c in cookies)

        before_index = len(responses)
        page.locator("#lib-new-group").click()
        page.locator("#dialog-input").fill("Should Not Persist")
        page.get_by_role("button", name="Save", exact=True).click()

        expect(page.locator("#dialog-title")).to_have_text("Could not create this group.")

        window = responses[before_index:]
        group_posts = [r for r in window if r[0] == "POST" and r[1].endswith("/api/v1/groups")]
        root_gets = [r for r in window if r[0] == "GET" and r[1] == base_url + "/"]
        assert [status for (_, _, status) in group_posts] == [401, 401], (
            f"Expected exactly one retry attempt (401 then 401 again), got {group_posts}"
        )
        assert len(root_gets) == 1, f"Expected exactly one reestablish attempt, got {root_gets}"

        page.get_by_role("button", name="OK", exact=True).click()
        with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
            groups = client.get("/api/v1/library").json()["groups"]
        assert not any(g["name"] == "Should Not Persist" for g in groups)
        assert native_dialogs == [], "Must never use a native alert/confirm/prompt"
        assert page_errors == []

        context.close()
        browser.close()


def test_operation_token_mutations_unaffected_by_console_retry(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    # No browser, no cookies at all - only the operation token. The 401-retry
    # logic lives entirely in web/index.html's apiRequest()/reestablishConsoleSession(),
    # which this plain HTTP client never executes; server/auth.py's
    # require_management_write() authorizes the Bearer token on the first attempt
    # (has_operation_token()), so this must succeed in one request with no cookie
    # dance at all.
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        response = client.post("/api/v1/groups", json={"name": "Operation Token Group"})
        assert response.status_code == 201, response.text
        groups = client.get("/api/v1/library").json()["groups"]
        assert any(g["name"] == "Operation Token Group" for g in groups)


# ---------------------------------------------------------------------------
# V1/V2/V3/V4 - Viewer rail scoped to the current deck's Library group (T0014 4)
# ---------------------------------------------------------------------------

def test_viewer_rail_scoped_to_current_group(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        group_b = _create_group(client, "Group B")
        a1 = _create_deck(client, "A1", "rail-a1")
        a2 = _create_deck(client, "A2", "rail-a2")
        a3 = _create_deck(client, "A3", "rail-a3")
        b1 = _create_deck(client, "B1", "rail-b1")
        b2 = _create_deck(client, "B2", "rail-b2")
        u1 = _create_deck(client, "U1", "rail-u1")
        u2 = _create_deck(client, "U2", "rail-u2")
        for index, deck in enumerate([a1, a2, a3]):
            _move_deck(client, deck["id"], group_a["id"], index)
        for index, deck in enumerate([b1, b2]):
            _move_deck(client, deck["id"], group_b["id"], index)
        _move_deck(client, u1["id"], None, 0)
        _move_deck(client, u2["id"], None, 1)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # V1: opening A2 from its Library card scopes the rail to Group A only.
            _card(page, a2["id"]).click()
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("A2")
            assert set(_rail_deck_ids(page)) == {a1["id"], a2["id"], a3["id"]}

            # V4: switching decks from inside the rail keeps the same group scope.
            page.locator(f'#deck-list .deck-item[data-deck-id="{a3["id"]}"]').click()
            expect(page.locator("#bar .title")).to_have_text("A3")
            assert set(_rail_deck_ids(page)) == {a1["id"], a2["id"], a3["id"]}

            page.locator("#to-library").click()
            expect(page.locator("#library")).to_be_visible()

            # V2: an Ungrouped deck only ever sees other Ungrouped decks.
            _card(page, u1["id"]).click()
            expect(page.locator("#bar .title")).to_have_text("U1")
            assert set(_rail_deck_ids(page)) == {u1["id"], u2["id"]}

            context.close()
            browser.close()


def test_viewer_rail_scoped_on_direct_open(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        group_b = _create_group(client, "Group B")
        a1 = _create_deck(client, "A1", "direct-a1")
        a2 = _create_deck(client, "A2", "direct-a2")
        b1 = _create_deck(client, "B1", "direct-b1")
        b2 = _create_deck(client, "B2", "direct-b2")
        for index, deck in enumerate([a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)
        for index, deck in enumerate([b1, b2]):
            _move_deck(client, deck["id"], group_b["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()

            # V3: a direct #deck_id open has no Library click context at all; group
            # scope must still come from server-authoritative /api/v1/library data.
            page.goto(base_url + "/#" + a2["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("A2")
            rail_ids = set(_rail_deck_ids(page))
            assert rail_ids == {a1["id"], a2["id"]}
            assert b1["id"] not in rail_ids
            assert b2["id"] not in rail_ids

            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# V5/V6/V7 - Viewer header zoom in/out (T0014 3)
# ---------------------------------------------------------------------------

def test_viewer_zoom_single_mode_scopes_to_deck_content(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        deck = _create_deck(client, "Zoom Deck", "zoom-1")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#zoom-controls")).to_be_visible()
            expect(page.locator("#zoom-level")).to_have_text("100%")
            expect(page.locator("#zoom-out")).to_have_attribute("aria-label", "Zoom out")
            expect(page.locator("#zoom-in")).to_have_attribute("aria-label", "Zoom in")

            bar_box_before = page.locator("#bar").bounding_box()
            rail_box_before = page.locator("#rail").bounding_box()

            # V5: Zoom In enlarges only the deck content pane.
            page.locator("#zoom-in").click()
            expect(page.locator("#zoom-level")).to_have_text("110%")
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "1.1"

            bar_box_after = page.locator("#bar").bounding_box()
            rail_box_after = page.locator("#rail").bounding_box()
            assert bar_box_after["height"] == bar_box_before["height"]
            assert rail_box_after["width"] == rail_box_before["width"]
            assert rail_box_after["height"] == rail_box_before["height"]

            # Zoom Out returns to a smaller step than the starting point.
            page.locator("#zoom-out").click()
            page.locator("#zoom-out").click()
            expect(page.locator("#zoom-level")).to_have_text("90%")
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "0.9"

            # iframe URL is untouched by zoom.
            assert f"/v/{deck['id']}/" in page.locator("#frame-a").get_attribute("src")

            context.close()
            browser.close()


def test_viewer_zoom_compare_mode_applies_to_both_panes(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    payload_v1 = _zip({"index.html": "<!doctype html><html><body>v1</body></html>"})
    payload_v2 = _zip({"index.html": "<!doctype html><html><body>v2</body></html>"})
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        deck = _create_deck(client, "Compare Zoom Deck", "zoom-compare-1", payload_v1)
        added = client.post(
            f"/api/v1/decks/{deck['id']}/versions",
            files={"file": ("deck.zip", payload_v2, "application/zip")},
            data={"idempotency_key": "zoom-compare-2"},
        )
        assert added.status_code == 201, added.text

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()

            page.locator("#zoom-in").click()
            expect(page.locator("#zoom-level")).to_have_text("110%")

            page.locator('#mode-switch button[data-mode="compare"]').click()
            expect(page.locator("#viewer")).to_have_class("compare")
            expect(page.locator("#compare-controls")).to_be_visible()

            # V6: compare mode applies the same zoom factor to both panes.
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "1.1"
            assert page.locator("#frame-b").evaluate("el => el.style.zoom") == "1.1"

            # Compare controls stay fully usable after zoom changed.
            version_a_before = page.locator("#version-a").input_value()
            version_b_before = page.locator("#version-b").input_value()
            page.locator("#swap").click()
            assert page.locator("#version-a").input_value() == version_b_before
            assert page.locator("#version-b").input_value() == version_a_before
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "1.1"
            assert page.locator("#frame-b").evaluate("el => el.style.zoom") == "1.1"

            context.close()
            browser.close()


def test_viewer_zoom_persists_across_version_screen_and_mode_switches(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    manifest = {
        "screens": [
            {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
            {"key": "details", "tag": "Details", "entry": "details/index.html"},
        ]
    }
    payload_v1 = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>one</body></html>",
            "overview/index.html": "<!doctype html><html><body>overview one</body></html>",
            "details/index.html": "<!doctype html><html><body>details one</body></html>",
        }
    )
    payload_v2 = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>two</body></html>",
            "overview/index.html": "<!doctype html><html><body>overview two</body></html>",
            "details/index.html": "<!doctype html><html><body>details two</body></html>",
        }
    )
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        deck = _create_deck(client, "Zoom Persist Deck", "zoom-persist-1", payload_v1)
        added = client.post(
            f"/api/v1/decks/{deck['id']}/versions",
            files={"file": ("deck.zip", payload_v2, "application/zip")},
            data={"idempotency_key": "zoom-persist-2"},
        )
        assert added.status_code == 201, added.text

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()

            page.locator("#zoom-in").click()
            page.locator("#zoom-in").click()
            expect(page.locator("#zoom-level")).to_have_text("120%")

            # Version switch (single-pane history) keeps the zoom level.
            page.locator("#version-list .version-card.selectable").first.click()
            expect(page.locator("#zoom-level")).to_have_text("120%")
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "1.2"

            # Screen switch keeps the zoom level.
            expect(page.locator("#scope-switch")).to_be_visible()
            page.locator(".screen-item").nth(1).click()
            expect(page.locator("#zoom-level")).to_have_text("120%")
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "1.2"

            # Active/Compare toggle keeps the zoom level on both panes.
            page.locator('#mode-switch button[data-mode="compare"]').click()
            expect(page.locator("#viewer")).to_have_class("compare")
            assert page.locator("#frame-a").evaluate("el => el.style.zoom") == "1.2"
            assert page.locator("#frame-b").evaluate("el => el.style.zoom") == "1.2"
            page.locator('#mode-switch button[data-mode="single"]').click()
            expect(page.locator("#viewer")).not_to_have_class("compare")
            expect(page.locator("#zoom-level")).to_have_text("120%")

            # None of the above disturbed the stable Viewer URL contract.
            assert f"/v/{deck['id']}/" in page.locator("#frame-a").get_attribute("src")

            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# Z1/Z2/Z3 - Library/Home header zoom in/out (T0016 1)
# ---------------------------------------------------------------------------

def test_library_zoom_basic_scales_dashboard_content(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "z1-a1")
        _move_deck(client, a1["id"], group_a["id"], 0)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # Z1: the header shows [-] 100% [+] before anything is touched, and
            # only #lib-scroll (the dashboard content column) carries the zoom -
            # never the header/search bar it lives in.
            expect(page.locator("#lib-zoom-controls")).to_be_visible()
            expect(page.locator("#lib-zoom-level")).to_have_text("100%")
            expect(page.locator("#lib-zoom-out")).to_have_attribute("aria-label", "Zoom out")
            expect(page.locator("#lib-zoom-in")).to_have_attribute("aria-label", "Zoom in")
            assert page.locator("#lib-scroll").evaluate("el => el.style.zoom") == "1"

            page.locator("#lib-zoom-in").click()
            expect(page.locator("#lib-zoom-level")).to_have_text("110%")
            assert page.locator("#lib-scroll").evaluate("el => el.style.zoom") == "1.1"
            assert page.locator("#lib-bar").evaluate("el => el.style.zoom") in ("", "normal")

            # Zoom survives search and group collapse/expand.
            page.locator("#lib-search").fill("A1")
            expect(page.locator("#lib-zoom-level")).to_have_text("110%")
            page.locator("#lib-search").fill("")

            toggle = page.locator(f'.lib-group[data-group-id="{group_a["id"]}"] .lib-group-toggle')
            toggle.click()
            expect(page.locator("#lib-zoom-level")).to_have_text("110%")
            toggle.click()

            page.locator("#lib-zoom-out").click()
            expect(page.locator("#lib-zoom-level")).to_have_text("100%")
            assert page.locator("#lib-scroll").evaluate("el => el.style.zoom") == "1"

            context.close()
            browser.close()


def test_library_zoom_interactions_stay_correct_at_120_percent(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "z2-a1")
        a2 = _create_deck(client, "A2", "z2-a2")
        for index, deck in enumerate([a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            page.locator("#lib-zoom-in").click()
            page.locator("#lib-zoom-in").click()
            expect(page.locator("#lib-zoom-level")).to_have_text("120%")

            # Z2: search still filters correctly while zoomed.
            page.locator("#lib-search").fill("A2")
            expect(_card(page, a2["id"])).to_be_visible()
            expect(_card(page, a1["id"])).to_have_count(0)
            page.locator("#lib-search").fill("")

            # Group collapse toggling still works.
            toggle = page.locator(f'.lib-group[data-group-id="{group_a["id"]}"] .lib-group-toggle')
            toggle.click()
            assert page.locator(f'.lib-group[data-group-id="{group_a["id"]}"]').evaluate(
                "el => el.classList.contains('collapsed')"
            )
            toggle.click()

            # Context action (kebab menu) still opens and dismisses cleanly.
            _card(page, a1["id"]).locator(".lib-kebab").click()
            page.get_by_role("menuitem", name="Rename", exact=True).click()
            expect(page.locator("#dialog-overlay")).to_be_visible()
            page.locator("#dialog-cancel").click()

            # DnD reorder lands at the real, pointer-accurate drop target under zoom.
            _card(page, a2["id"]).drag_to(_card(page, a1["id"]), target_position={"x": 20, "y": 3})
            _wait_for_section(client, page, group_a["id"], [a2["id"], a1["id"]])

            # Deck click still navigates to the Viewer. finishLibraryDrag() (T0014
            # 2) suppresses the click that would otherwise immediately follow a
            # drag release for 300ms - wait it out first so this exercises a real
            # click, not the drag's own release suppression window.
            page.wait_for_timeout(350)
            _card(page, a1["id"]).click()
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("A1")

            context.close()
            browser.close()


def test_library_and_viewer_zoom_are_independent(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        deck = _create_deck(client, "Indep Deck", "z3-1")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # Library 120%.
            page.locator("#lib-zoom-in").click()
            page.locator("#lib-zoom-in").click()
            expect(page.locator("#lib-zoom-level")).to_have_text("120%")

            # Z3: opening a deck starts the Viewer at its own independent 100%.
            _card(page, deck["id"]).click()
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#zoom-level")).to_have_text("100%")

            # Viewer 130% - three steps up from 100%.
            page.locator("#zoom-in").click()
            page.locator("#zoom-in").click()
            page.locator("#zoom-in").click()
            expect(page.locator("#zoom-level")).to_have_text("130%")

            # Back to Library: its own zoom (120%) was never touched by the Viewer's.
            page.locator("#to-library").click()
            expect(page.locator("#library")).to_be_visible()
            expect(page.locator("#lib-zoom-level")).to_have_text("120%")
            assert page.locator("#lib-scroll").evaluate("el => el.style.zoom") == "1.2"

            context.close()
            browser.close()


def test_library_zoom_controls_sit_at_far_right_of_header(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        _create_deck(client, "Right Edge Deck", "z4-1")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1280, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # Z4 (human rejection rej_01M33NJ2M906P7ZM): the zoom controls sit
            # flush against the header's own right padding edge - to the right
            # of "New group", not merely somewhere right of the search box -
            # and that holds regardless of the zoom level or a search filter
            # narrowing the dashboard content below.
            def right_edge_gap() -> float:
                bar = page.locator("#lib-bar").bounding_box()
                zoom = page.locator("#lib-zoom-controls").bounding_box()
                return (bar["x"] + bar["width"]) - (zoom["x"] + zoom["width"])

            new_group_box = page.locator("#lib-new-group").bounding_box()
            zoom_box = page.locator("#lib-zoom-controls").bounding_box()
            assert zoom_box["x"] > new_group_box["x"] + new_group_box["width"]
            assert right_edge_gap() == pytest.approx(16, abs=1)

            page.locator("#lib-zoom-in").click()
            page.locator("#lib-zoom-in").click()
            expect(page.locator("#lib-zoom-level")).to_have_text("120%")
            assert right_edge_gap() == pytest.approx(16, abs=1)

            page.locator("#lib-search").fill("Right Edge")
            assert right_edge_gap() == pytest.approx(16, abs=1)
            page.locator("#lib-search").fill("")

            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# N1/N2/N3 - Viewer rail deck click is a single navigation owner (T0016 2)
# ---------------------------------------------------------------------------

def test_viewer_rail_click_moves_actual_deck_content(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "n1-a1")
        a2 = _create_deck(client, "A2", "n1-a2")
        a3 = _create_deck(client, "A3", "n1-a3")
        for index, deck in enumerate([a1, a2, a3]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/#" + a1["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("A1")

            # N1: a rail click on a different deck moves currentId, the hash, the
            # title, and - the part earlier coverage only checked indirectly - the
            # actual iframe URL and rendered content.
            page.locator(f'#deck-list .deck-item[data-deck-id="{a2["id"]}"]').click()
            expect(page.locator("#bar .title")).to_have_text("A2")
            assert page.evaluate("location.hash") == "#" + a2["id"]
            assert f"/v/{a2['id']}/" in page.locator("#frame-a").get_attribute("src")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("A2")
            expect(
                page.locator(f'#deck-list .deck-item[data-deck-id="{a2["id"]}"]')
            ).to_have_class("deck-item active")

            # N3: the rail still only shows Group A after switching decks from
            # inside it.
            assert set(_rail_deck_ids(page)) == {a1["id"], a2["id"], a3["id"]}

            context.close()
            browser.close()


def test_viewer_rail_rapid_clicks_settle_on_last_deck(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "n2-a1")
        a2 = _create_deck(client, "A2", "n2-a2")
        a3 = _create_deck(client, "A3", "n2-a3")
        for index, deck in enumerate([a1, a2, a3]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/#" + a1["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()

            # N2: click through A2 then A3 back-to-back without waiting for A2's
            # own async screens/version fetches to settle first. currentId's guard
            # (`if (currentId === id) ...`) must stop A2's late responses from
            # clobbering A3's state once everything resolves.
            page.locator(f'#deck-list .deck-item[data-deck-id="{a2["id"]}"]').click()
            page.locator(f'#deck-list .deck-item[data-deck-id="{a3["id"]}"]').click()

            expect(page.locator("#bar .title")).to_have_text("A3")
            assert page.evaluate("location.hash") == "#" + a3["id"]
            assert f"/v/{a3['id']}/" in page.locator("#frame-a").get_attribute("src")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("A3")
            expect(
                page.locator(f'#deck-list .deck-item[data-deck-id="{a3["id"]}"]')
            ).to_have_class("deck-item active")
            # Only one deck is ever marked active - no leftover A2 highlight.
            assert page.locator("#deck-list .deck-item.active").count() == 1

            context.close()
            browser.close()


# N4 - T0016 review rej_01M33H6TVQ61HBCX: openDeckFromHash() itself needs a
# latest-wins guard, not just selectDeck()'s currentId check. N2 above never
# exercised that gap because both rail clicks race over an unthrottled local
# server, so in practice the first request usually still wins the network too.
# This test forces the exact ordering the review described - A2's very first
# network request (ensureDecksLoaded()'s GET /api/v1/decks) is held open until
# *after* A3's entire navigation (network + rendering) has already settled,
# then released - and checks every piece of Viewer chrome the review named:
# hash, title, frame URL/content, version list, screen chrome, and rail
# highlight.
#
# The hold is done by monkey-patching window.fetch in-page (a manually
# resolved gate Promise) rather than delaying via Playwright's page.route() +
# time.sleep(): a Python route handler runs on the same thread that also
# drives every other synchronous Playwright call in this test, so blocking it
# with time.sleep() blocks the second .click() too (confirmed by instrumenting
# both approaches side by side while building this test) - the two navigations
# end up serialized instead of actually racing, and the test would pass
# whether or not the underlying bug was fixed. Gating fetch() in-page keeps
# both clicks - and both requests - firing back-to-back for real, and lets the
# test release A2's request on demand only once A3 has fully settled.
def test_viewer_rail_rapid_clicks_settle_on_last_deck_with_reversed_network(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    manifest = {
        "screens": [
            {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
            {"key": "details", "tag": "Details", "entry": "details/index.html"},
        ]
    }
    a2_payload_v1 = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>A2</body></html>",
            "overview/index.html": "<!doctype html><html><body>A2 overview</body></html>",
            "details/index.html": "<!doctype html><html><body>A2 details</body></html>",
        }
    )
    a2_payload_v2 = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>A2 v2</body></html>",
            "overview/index.html": "<!doctype html><html><body>A2 overview v2</body></html>",
            "details/index.html": "<!doctype html><html><body>A2 details v2</body></html>",
        }
    )
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "n4-a1")
        # A2 is deliberately given a second version and a 2-screen manifest -
        # distinct enough from A3's single-version, single-screen shape that any
        # leftover A2 state (version list, scope-switch) would be obvious.
        a2 = _create_deck(client, "A2", "n4-a2", a2_payload_v1)
        added = client.post(
            f"/api/v1/decks/{a2['id']}/versions",
            files={"file": ("deck.zip", a2_payload_v2, "application/zip")},
            data={"idempotency_key": "n4-a2-v2"},
        )
        assert added.status_code == 201, added.text
        a3 = _create_deck(client, "A3", "n4-a3")
        for index, deck in enumerate([a1, a2, a3]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()

            page.goto(base_url + "/#" + a1["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()

            # Installed only after the initial load has already settled, so the
            # first GET /api/v1/decks it sees is the one A2's click triggers -
            # not the initial-page-load request. That first call is parked on a
            # gate Promise (never rejected, never timed out - just held) until
            # the test explicitly releases it below.
            page.evaluate(
                """() => {
                    const originalFetch = window.fetch.bind(window);
                    let decksCallCount = 0;
                    let releaseFirst;
                    const gate = new Promise(resolve => { releaseFirst = resolve; });
                    window.__releaseFirstDecksFetch = () => releaseFirst();
                    window.fetch = (url, opts) => {
                        if (typeof url === 'string' && url.endsWith('/api/v1/decks')) {
                            decksCallCount += 1;
                            if (decksCallCount === 1) {
                                return gate.then(() => originalFetch(url, opts));
                            }
                        }
                        return originalFetch(url, opts);
                    };
                }"""
            )

            page.locator(f'#deck-list .deck-item[data-deck-id="{a2["id"]}"]').click()
            page.locator(f'#deck-list .deck-item[data-deck-id="{a3["id"]}"]').click()

            # A3's own decks fetch is the second call, so it's never gated -
            # its whole navigation (network + rendering) settles for real
            # while A2's first fetch is still parked on the open gate.
            expect(page.locator("#bar .title")).to_have_text("A3")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("A3")

            # Only now - after A3 has genuinely finished - let A2's held-back
            # decks fetch actually run against the real server and resolve.
            page.evaluate("window.__releaseFirstDecksFetch()")
            page.wait_for_timeout(500)

            expect(page.locator("#bar .title")).to_have_text("A3")
            assert page.evaluate("location.hash") == "#" + a3["id"]
            assert f"/v/{a3['id']}/" in page.locator("#frame-a").get_attribute("src")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("A3")

            # A3 has one version and one (default) screen - if A2's delayed
            # navigation had been allowed to land after the fact, it would have
            # left its own 2-version history and visible screen switcher behind.
            expect(page.locator("#scope-switch")).to_be_hidden()
            expect(page.locator("#version-list .version-card")).to_have_count(1)

            expect(
                page.locator(f'#deck-list .deck-item[data-deck-id="{a3["id"]}"]')
            ).to_have_class("deck-item active")
            assert page.locator("#deck-list .deck-item.active").count() == 1

            # Give any further stragglers one more beat, then re-check that A3
            # is still the settled state - not just transiently true.
            page.wait_for_timeout(300)
            expect(page.locator("#bar .title")).to_have_text("A3")
            assert page.evaluate("location.hash") == "#" + a3["id"]

            context.close()
            browser.close()


# N5 - T0016 review rej_01M33JG3BQCB2DAF: openDeckFromHash()'s navGeneration
# guard (N4 above) only stops a stale call from reaching showViewerScreen()
# onward - it never stopped loadRailScope() itself from writing the *shared*
# railScopeIds the moment its own /api/v1/library response landed, regardless
# of whether that call was still the latest one. N4 never caught this because
# it only reverses /api/v1/decks (which loadRailScope() never touches) and
# only ever moves within a single group, so a stale railScopeIds write there
# is indistinguishable from a correct one.
#
# This test reverses /api/v1/library instead, across two *different* groups:
# A2's own library lookup (fired by a click that's immediately superseded) is
# held open until after B1's entire navigation - a different group - has
# already settled for real. The corruption this guards against is silent: the
# DOM isn't wrong the instant the stale response lands, since nothing calls
# renderDecks() again on its own. It only surfaces on the next renderDecks()
# call for an unrelated reason (here, picking a different screen in B1's own
# tree) - exactly the path the review named - so the test deliberately
# triggers one after releasing the gate and checks the rail is still Group B,
# not reverted to Group A.
def test_viewer_rail_cross_group_navigation_settles_on_current_group_with_reversed_network(
    connected_server,
):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    manifest = {
        "screens": [
            {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
            {"key": "details", "tag": "Details", "entry": "details/index.html"},
        ]
    }
    b1_payload = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>B1</body></html>",
            "overview/index.html": "<!doctype html><html><body>B1 overview</body></html>",
            "details/index.html": "<!doctype html><html><body>B1 details</body></html>",
        }
    )
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        group_b = _create_group(client, "Group B")
        a1 = _create_deck(client, "A1", "n5-a1")
        a2 = _create_deck(client, "A2", "n5-a2")
        for index, deck in enumerate([a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)
        # B1 is deliberately multi-screen - distinct enough that selecting one
        # of its own screens after the gate release is a real, independently
        # motivated action, not something only there to poke at the bug.
        b1 = _create_deck(client, "B1", "n5-b1", b1_payload)
        b2 = _create_deck(client, "B2", "n5-b2")
        for index, deck in enumerate([b1, b2]):
            _move_deck(client, deck["id"], group_b["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()

            page.goto(base_url + "/#" + a1["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()

            # Installed only after the initial load has already settled, so the
            # first GET /api/v1/library it sees is the one A2's own loadRailScope()
            # fires - not the initial-page-load request. That call is parked on a
            # gate Promise (never rejected, never timed out - just held) until the
            # test explicitly releases it below. window.__libraryCallCount is
            # exposed so the test can wait until A2's fetch has actually started
            # (and is parked) before firing B1's navigation - openDeckFromHash()
            # re-checks navGeneration right before loadRailScope() too, so if B1
            # superseded A2 before A2 even got this far, A2 would bail out having
            # never called loadRailScope() at all, and this test would be
            # exercising nothing.
            page.evaluate(
                """() => {
                    const originalFetch = window.fetch.bind(window);
                    window.__libraryCallCount = 0;
                    let releaseFirst;
                    const gate = new Promise(resolve => { releaseFirst = resolve; });
                    window.__releaseFirstLibraryFetch = () => releaseFirst();
                    window.fetch = (url, opts) => {
                        if (typeof url === 'string' && url.endsWith('/api/v1/library')) {
                            window.__libraryCallCount += 1;
                            if (window.__libraryCallCount === 1) {
                                return gate.then(() => originalFetch(url, opts));
                            }
                        }
                        return originalFetch(url, opts);
                    };
                }"""
            )

            # A2's own rail click starts a navigation whose loadRailScope() fetch
            # is the gated first call. B1 itself is reached the same way a typed
            # hash, bookmark, or browser back/forward would reach it (the code's
            # own openDeckFromHash() comment calls all of these equivalent entry
            # points) - not a rail click, since Group B's decks are never part of
            # A1/A2's own rail scope to begin with.
            page.locator(f'#deck-list .deck-item[data-deck-id="{a2["id"]}"]').click()
            # Wait until A2's loadRailScope() has actually reached its
            # /api/v1/library fetch (and is now parked on the gate) before
            # superseding it - otherwise A2's own navGeneration re-check would
            # make it bail out before ever calling loadRailScope() at all.
            page.wait_for_function("window.__libraryCallCount === 1")
            # B1 is a different group entirely; its own loadRailScope() fetch is
            # the second call, never gated, so its whole navigation (network +
            # rendering) settles for real while A2's first fetch is still
            # parked on the open gate.
            page.evaluate("id => { location.hash = id; }", b1["id"])

            expect(page.locator("#bar .title")).to_have_text("B1")
            assert page.evaluate("location.hash") == "#" + b1["id"]
            assert set(_rail_deck_ids(page)) == {b1["id"], b2["id"]}

            # Only now - after B1 has genuinely finished - let A2's held-back
            # library fetch actually run against the real server and resolve.
            page.evaluate("window.__releaseFirstLibraryFetch()")
            page.wait_for_timeout(500)

            # The DOM doesn't necessarily change the instant the stale response
            # lands - loadRailScope() resolving late no longer triggers a
            # render on its own. The corruption this guards against only shows
            # up on the *next* renderDecks() call, so force one via an
            # unrelated, legitimate action: picking B1's other screen.
            page.locator(".screen-tree .screen-item", has_text="Details").click()

            expect(page.locator("#bar .title")).to_have_text("B1")
            assert page.evaluate("location.hash") == "#" + b1["id"]
            assert f"/v/{b1['id']}/" in page.locator("#frame-a").get_attribute("src")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("B1 details")

            # The rail must still be Group B - not reverted to Group A by A2's
            # stale (but now-resolved) loadRailScope() response.
            assert set(_rail_deck_ids(page)) == {b1["id"], b2["id"]}
            expect(
                page.locator(f'#deck-list .deck-item[data-deck-id="{b1["id"]}"]')
            ).to_have_class("deck-item active")
            assert page.locator("#deck-list .deck-item.active").count() == 1

            # Give any further stragglers one more beat, then re-check that B1
            # is still the settled state - not just transiently true.
            page.wait_for_timeout(300)
            expect(page.locator("#bar .title")).to_have_text("B1")
            assert page.evaluate("location.hash") == "#" + b1["id"]
            assert set(_rail_deck_ids(page)) == {b1["id"], b2["id"]}

            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# H1/H2 - Group-wide drop highlight has a symmetric top/bottom gap (T0016 3)
# ---------------------------------------------------------------------------

def _group_cards_gap(page: Page, group_id: str) -> tuple[float, float]:
    return page.evaluate(
        """(groupId) => {
            const cards = document.querySelector(`.lib-group[data-group-id="${groupId}"] .lib-group-cards`);
            const box = cards.getBoundingClientRect();
            const children = Array.from(cards.children);
            const tops = children.map(child => child.getBoundingClientRect().top);
            const bottoms = children.map(child => child.getBoundingClientRect().bottom);
            const top = Math.min(...tops);
            const bottom = Math.max(...bottoms);
            return [top - box.top, box.bottom - bottom];
        }""",
        group_id,
    )


def test_group_drop_highlight_has_symmetric_vertical_gap(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "h1-a1")
        a2 = _create_deck(client, "A2", "h1-a2")
        for index, deck in enumerate([a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # H1: simulate the "drop onto the whole group" highlight state (the
            # same class dropDeck()'s dragover handler applies to .lib-group-cards)
            # and confirm the highlighted box has an equal gap above and below the
            # deck cards it wraps - before the fix the top padding (8px) left a
            # visible gap the bottom padding (0px) did not.
            page.evaluate(
                """(groupId) => {
                    document.querySelector(`.lib-group[data-group-id="${groupId}"] .lib-group-cards`)
                        .classList.add('deck-drop-target');
                }""",
                group_a["id"],
            )
            top_gap, bottom_gap = _group_cards_gap(page, group_a["id"])
            assert top_gap > 0 and bottom_gap > 0
            assert abs(top_gap - bottom_gap) < 1.0, f"top={top_gap} bottom={bottom_gap}"

            context.close()
            browser.close()


def test_group_drop_highlight_symmetric_when_empty_filtered_collapsed_and_zoomed(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        empty_group = _create_group(client, "Empty Group")
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "Alpha One", "h2-a1")
        a2 = _create_deck(client, "Alpha Two", "h2-a2")
        for index, deck in enumerate([a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # H2a: an empty group's highlighted box still wraps its "No decks"
            # placeholder with an equal gap on both sides.
            page.evaluate(
                """(groupId) => {
                    document.querySelector(`.lib-group[data-group-id="${groupId}"] .lib-group-cards`)
                        .classList.add('deck-drop-target');
                }""",
                empty_group["id"],
            )
            top_gap, bottom_gap = _group_cards_gap(page, empty_group["id"])
            assert abs(top_gap - bottom_gap) < 1.0, f"empty group: top={top_gap} bottom={bottom_gap}"

            # H2b: search filter narrows the visible groups, highlight stays symmetric.
            page.locator("#lib-search").fill("Alpha")
            expect(page.locator(f'.lib-group[data-group-id="{group_a["id"]}"]')).to_be_visible()
            page.evaluate(
                """(groupId) => {
                    document.querySelector(`.lib-group[data-group-id="${groupId}"] .lib-group-cards`)
                        .classList.add('deck-drop-target');
                }""",
                group_a["id"],
            )
            top_gap, bottom_gap = _group_cards_gap(page, group_a["id"])
            assert abs(top_gap - bottom_gap) < 1.0, f"filtered: top={top_gap} bottom={bottom_gap}"
            page.locator("#lib-search").fill("")

            # H2c: collapsed group - the cards row is hidden, but the header still
            # accepts the drop-target highlight class (unaffected by the padding fix).
            page.locator(f'.lib-group[data-group-id="{group_a["id"]}"] .lib-group-toggle').click()
            assert page.locator(f'.lib-group[data-group-id="{group_a["id"]}"]').evaluate(
                "el => el.classList.contains('collapsed')"
            )
            page.evaluate(
                """(groupId) => {
                    document.querySelector(`.lib-group[data-group-id="${groupId}"] .lib-group-header`)
                        .classList.add('deck-drop-target');
                }""",
                group_a["id"],
            )
            assert page.locator(
                f'.lib-group[data-group-id="{group_a["id"]}"] .lib-group-header'
            ).evaluate("el => el.classList.contains('deck-drop-target')")
            page.locator(f'.lib-group[data-group-id="{group_a["id"]}"] .lib-group-toggle').click()

            # H2d: Library zoom scales the whole dashboard uniformly, so the gap
            # stays symmetric (though its pixel size changes with the zoom factor).
            page.locator("#lib-zoom-in").click()
            page.locator("#lib-zoom-in").click()
            expect(page.locator("#lib-zoom-level")).to_have_text("120%")
            page.evaluate(
                """(groupId) => {
                    document.querySelector(`.lib-group[data-group-id="${groupId}"] .lib-group-cards`)
                        .classList.add('deck-drop-target');
                }""",
                group_a["id"],
            )
            top_gap, bottom_gap = _group_cards_gap(page, group_a["id"])
            assert abs(top_gap - bottom_gap) < 1.5, f"zoomed: top={top_gap} bottom={bottom_gap}"

            context.close()
            browser.close()


# ---------------------------------------------------------------------------
# R1/R2/R3 - Viewer rail scope fails closed, never falls back to every deck
# (T0016 4)
# ---------------------------------------------------------------------------

def test_viewer_rail_scope_fails_closed_on_library_error(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "r1-a1")
        a2 = _create_deck(client, "A2", "r1-a2")
        a3 = _create_deck(client, "A3", "r1-a3")
        for index, deck in enumerate([a1, a2, a3]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.route(
                "**/api/v1/library",
                lambda route: route.fulfill(status=500, content_type="application/json", body="{}"),
            )

            # R1: /api/v1/library returning 500 must fail closed to just the
            # current deck, never the full deck list.
            page.goto(base_url + "/#" + a2["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("A2")
            assert _rail_deck_ids(page) == [a2["id"]]

            context.close()
            browser.close()


def test_viewer_rail_scope_fails_closed_on_missing_membership(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "r2-a1")
        a2 = _create_deck(client, "A2", "r2-a2")
        for index, deck in enumerate([a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            empty_library = {"groups": [], "ungrouped": {"decks": []}}
            page.route(
                "**/api/v1/library",
                lambda route: route.fulfill(
                    status=200, content_type="application/json", body=json.dumps(empty_library)
                ),
            )

            # R2: a 200 /api/v1/library that simply doesn't list the current deck
            # anywhere (grouped or Ungrouped) must also fail closed, not fall back
            # to unscoped.
            page.goto(base_url + "/#" + a2["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("A2")
            assert _rail_deck_ids(page) == [a2["id"]]

            context.close()
            browser.close()


def test_viewer_rail_order_matches_library_group_order(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        group_a = _create_group(client, "Group A")
        a1 = _create_deck(client, "A1", "r3-a1")
        a2 = _create_deck(client, "A2", "r3-a2")
        a3 = _create_deck(client, "A3", "r3-a3")
        # Registration order is a1, a2, a3, but Library's own (server-authoritative)
        # order is customized to a3, a1, a2 - the rail must reflect that order, not
        # /api/v1/decks' registration order (T0016 4).
        for index, deck in enumerate([a3, a1, a2]):
            _move_deck(client, deck["id"], group_a["id"], index)
        assert _section_ids(client.get("/api/v1/library").json(), group_a["id"]) == [
            a3["id"], a1["id"], a2["id"],
        ]

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(viewport={"width": 1100, "height": 800})
            page = context.new_page()
            page.goto(base_url + "/#" + a1["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()

            # R3: the rail's DOM order (not just membership) matches Library's.
            assert _rail_deck_ids(page) == [a3["id"], a1["id"], a2["id"]]

            context.close()
            browser.close()
