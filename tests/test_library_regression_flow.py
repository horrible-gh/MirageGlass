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
