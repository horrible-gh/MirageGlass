"""Connected browser regression for Library organization and Viewer continuity."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect, sync_playwright

from conftest import PROJECT_ROOT


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _zip(entries: dict[str, str]) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries.items():
            archive.writestr(name, value)
    return stream.getvalue()


def _wait_until(predicate, message: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except (AssertionError, httpx.HTTPError) as error:
            last_error = error
        time.sleep(0.05)
    detail = f" ({last_error})" if last_error else ""
    raise AssertionError(message + detail)


@pytest.fixture
def connected_server(tmp_path: Path):
    storage = tmp_path / "connected-storage"
    port = _free_port()
    token = "connected-flow-token"
    environment = os.environ.copy()
    environment.update(
        {
            "MIRAGEGLASS_STORAGE": str(storage),
            "MIRAGEGLASS_TOKEN": token,
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

        _wait_until(healthy, "Connected-flow server did not start", timeout=20)
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
            pytest.fail(f"Connected-flow server exited with {process.returncode}: {output}")


def _create_group(client: httpx.Client, name: str) -> dict:
    response = client.post("/api/v1/groups", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _create_deck(client: httpx.Client, name: str, key: str, payload: bytes | None = None) -> dict:
    body = payload or _zip({"index.html": f"<!doctype html><html><body>{name}</body></html>"})
    response = client.post(
        "/api/v1/decks",
        files={"file": ("deck.zip", body, "application/zip")},
        data={"name": name, "idempotency_key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _move_deck(client: httpx.Client, deck_id: str, group_id: str | None, index: int) -> None:
    response = client.post(
        f"/api/v1/decks/{deck_id}/move",
        json={"group_id": group_id, "index": index},
    )
    assert response.status_code == 200, response.text


def _section_ids(library: dict, group_id: str | None) -> list[str]:
    if group_id is None:
        return [deck["id"] for deck in library["ungrouped"]["decks"]]
    group = next(item for item in library["groups"] if item["id"] == group_id)
    return [deck["id"] for deck in group["decks"]]


def _launch_browser(playwright):
    """Use an explicitly configured browser, or Playwright's bundled Chromium."""
    executable = os.environ.get("MIRAGEGLASS_TEST_BROWSER")
    if executable:
        return playwright.chromium.launch(executable_path=executable)
    return playwright.chromium.launch()


def _capture(page: Page, name: str) -> None:
    directory = os.environ.get("MIRAGEGLASS_TEST_SCREENSHOTS")
    if directory:
        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f"{name}.png"), full_page=True)


def _enable_selection(page: Page) -> None:
    toggle = page.locator("#selection-toggle")
    if toggle.get_attribute("aria-pressed") != "true":
        toggle.click()


def _label(page: Page, key: str) -> str:
    return page.evaluate("key => t(key)", key)


def _project(page: Page, group_id: str):
    return page.locator(f'#project-nav .project-item[data-group-id="{group_id}"]')


def _choose_scope(page: Page, group_id: str | None) -> None:
    if group_id is None:
        page.locator("#nav-ungrouped").click()
    else:
        _project(page, group_id).locator(".side-link").click()
    page.locator("#lib-sort").select_option("manual")


def _dom_section_ids(page: Page, group_id: str | None) -> list[str]:
    value = "" if group_id is None else group_id
    return page.locator(
        f'#lib-groups .lib-card[data-group-id="{value}"]'
    ).evaluate_all("cards => cards.map(card => card.dataset.deckId)")


def _wait_for_section(
    client: httpx.Client,
    page: Page,
    group_id: str | None,
    expected: list[str],
) -> None:
    _wait_until(
        lambda: _section_ids(client.get("/api/v1/library").json(), group_id) == expected,
        f"Section {group_id!r} did not reach {expected!r}",
    )
    _choose_scope(page, group_id)
    while page.locator("#load-more").is_visible():
        page.locator("#load-more").click()
    expect(page.locator("#lib-groups .lib-card")).to_have_count(len(expected))
    assert _dom_section_ids(page, group_id) == expected


def _assert_dom_matches_server(client: httpx.Client, page: Page) -> dict:
    library = client.get("/api/v1/library").json()
    assert page.locator("#project-nav .project-item").evaluate_all(
        "groups => groups.map(group => group.dataset.groupId)"
    ) == [group["id"] for group in library["groups"]]
    # Ungrouped is a separate null-membership scope, never a real project row.
    expect(page.locator("#nav-ungrouped")).to_have_count(1)
    for group in [*library["groups"], library["ungrouped"]]:
        group_id = group.get("id")
        assert [deck["sort_order"] for deck in group["decks"]] == list(range(len(group["decks"])))
        _wait_for_section(client, page, group_id, [deck["id"] for deck in group["decks"]])
    page.locator("#nav-all").click()
    page.locator("#lib-sort").select_option("manual")
    return library


def _card(page: Page, deck_id: str):
    return page.locator(f'#lib-groups .lib-card[data-deck-id="{deck_id}"]')


def _menu_action(page: Page, action: str):
    return page.locator(f'.lib-menu-item[data-action="{action}"]')


def _open_move_dialog(page: Page, deck_id: str) -> None:
    _card(page, deck_id).locator(".lib-kebab").click()
    _menu_action(page, "move").click()
    expect(page.locator("#dialog-overlay")).to_be_visible()
    expect(page.locator("#dialog-cancel")).to_be_focused()


def _confirm_dialog_move(page: Page, group_id: str | None, index: int) -> None:
    group_value = "ungrouped" if group_id is None else f"group:{group_id}"
    page.locator("#dialog-move-group").select_option(group_value)
    page.locator("#dialog-move-index").select_option(str(index))
    page.locator("#dialog-confirm").click()


def test_library_connected_flow_persists_rolls_back_and_preserves_viewer(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    manifest = {"screens": [
        {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
        {"key": "details", "tag": "Details", "entry": "details/index.html"},
    ]}
    def multi_zip(version):
        return _zip({
            "screens.json": json.dumps(manifest),
            "index.html": f"<html><body>multi {version}</body></html>",
            "overview/index.html": f"<html><body>overview {version}</body></html>",
            "details/index.html": f"<html><body>details {version}</body></html>",
        })

    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        source = _create_group(client, "Source")
        target = _create_group(client, "Target")
        empty = _create_group(client, "Empty")
        first = _create_deck(client, "Visible A", "connected-a")
        hidden = _create_deck(client, "Hidden B", "connected-b")
        third = _create_deck(client, "Visible C", "connected-c")
        other = _create_deck(client, "Target D", "connected-d")
        multi = _create_deck(client, "Multi Viewer", "connected-multi", multi_zip(1))
        assert client.post(
            f"/api/v1/decks/{multi['id']}/versions",
            files={"file": ("deck.zip", multi_zip(2), "application/zip")},
            data={"idempotency_key": "connected-multi-v2"},
        ).status_code == 201
        for index, deck in enumerate([first, hidden, third]):
            _move_deck(client, deck["id"], source["id"], index)
        _move_deck(client, other["id"], target["id"], 0)

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(
                viewport={"width": 1280, "height": 900}, has_touch=True,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            page_errors, native_dialogs, move_requests = [], [], []
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("dialog", lambda dialog: (native_dialogs.append(dialog.type), dialog.dismiss()))
            page.on("request", lambda request: move_requests.append(request)
                    if request.method == "POST" and request.url.endswith("/move") else None)
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()
            _assert_dom_matches_server(client, page)

            # Global search hides a sibling; reordering must still use the full
            # server order, rather than the filtered card index.
            page.locator("#lib-search").fill("Visible")
            before = len(move_requests)
            _card(page, third["id"]).drag_to(
                _card(page, first["id"]), target_position={"x": 20, "y": 3})
            expected_source = [third["id"], first["id"], hidden["id"]]
            _wait_until(lambda: _section_ids(client.get("/api/v1/library").json(), source["id"])
                        == expected_source, "Filtered drag lost a hidden sibling")
            assert len(move_requests) == before + 1
            _wait_for_section(client, page, source["id"], expected_source)

            # A single drag reaches another project without opening a move dialog.
            before = len(move_requests)
            _card(page, hidden["id"]).drag_to(_project(page, target["id"]))
            _wait_for_section(client, page, target["id"], [other["id"], hidden["id"]])
            assert len(move_requests) == before + 1
            _choose_scope(page, source["id"])
            _card(page, first["id"]).drag_to(_project(page, empty["id"]))
            _wait_for_section(client, page, empty["id"], [first["id"]])
            page.reload(wait_until="networkidle")
            _wait_for_section(client, page, empty["id"], [first["id"]])

            # A project can itself be reordered, but Ungrouped is never in its order.
            groups_before = [g["id"] for g in client.get("/api/v1/library").json()["groups"]]
            before = len(move_requests)
            _project(page, empty["id"]).drag_to(_project(page, source["id"]),
                                               target_position={"x": 30, "y": 2})
            _wait_until(lambda: [g["id"] for g in client.get("/api/v1/library").json()["groups"]]
                        != groups_before, "Project drag did not persist")
            assert len(move_requests) == before + 1
            assert page.locator("#project-nav #nav-ungrouped").count() == 0

            # The fallback move is keyboard accessible, including destination/index.
            _choose_scope(page, source["id"])
            _card(page, third["id"]).locator(".lib-kebab").focus()
            page.keyboard.press("Enter")
            expect(_menu_action(page, "rename")).to_be_focused()
            page.keyboard.press("ArrowDown")
            expect(_menu_action(page, "move")).to_be_focused()
            page.keyboard.press("Enter")
            expect(page.locator("#dialog-cancel")).to_be_focused()
            _confirm_dialog_move(page, target["id"], 2)
            _wait_for_section(client, page, target["id"], [other["id"], hidden["id"], third["id"]])

            # A failed request cannot leave a fake move in the UI or server.
            server_before = client.get("/api/v1/library").json()
            def fail_move(route):
                route.fulfill(status=500, content_type="application/json",
                              body=json.dumps({"detail": "Injected move failure"}))
            page.route("**/api/v1/decks/move", fail_move)
            _open_move_dialog(page, third["id"])
            _confirm_dialog_move(page, source["id"], 0)
            expect(page.locator("#dialog-overlay")).to_be_visible()
            expect(page.locator("#dialog-body")).not_to_be_empty()
            assert client.get("/api/v1/library").json() == server_before
            page.locator("#dialog-confirm").click()
            page.unroute("**/api/v1/decks/move", fail_move)
            _wait_for_section(client, page, target["id"], [other["id"], hidden["id"], third["id"]])

            # A destination removed by another API client is refreshed after rejection.
            _open_move_dialog(page, other["id"])
            page.locator("#dialog-move-group").select_option(f"group:{source['id']}")
            assert client.delete(f"/api/v1/groups/{source['id']}").status_code == 204
            page.locator("#dialog-confirm").click()
            expect(page.locator("#dialog-overlay")).to_be_visible()
            page.locator("#dialog-confirm").click()
            expect(_project(page, source["id"])).to_have_count(0)
            _assert_dom_matches_server(client, page)

            # CRUD and favorites still use the same server record as an AI client.
            page.locator("#lib-new-group").click()
            page.locator("#dialog-input").fill("Temporary")
            page.locator("#dialog-confirm").click()
            temporary = next(g for g in client.get("/api/v1/library").json()["groups"]
                             if g["name"] == "Temporary")
            _project(page, temporary["id"]).locator(".lib-group-kebab").click()
            _menu_action(page, "delete").click()
            expect(page.locator("#dialog-cancel")).to_be_focused()
            page.locator("#dialog-confirm").click()
            expect(_project(page, temporary["id"])).to_have_count(0)
            page.locator("#nav-all").click()
            _card(page, third["id"]).locator(".lib-kebab").click()
            _menu_action(page, "rename").click()
            page.locator("#dialog-input").fill("Visible C renamed")
            page.locator("#dialog-confirm").click()
            expect(_card(page, third["id"]).locator(".lib-card-name")).to_have_text("Visible C renamed")
            _card(page, multi["id"]).locator(".lib-pin").click()
            _wait_until(lambda: client.get(f"/api/v1/decks/{multi['id']}").json()["favorite"],
                        "Favorite did not persist")
            page.locator("#nav-pinned").click()
            expect(_card(page, multi["id"])).to_be_visible()

            # Viewer timeline, screen selection, compare and stable links survive.
            page.goto(base_url + "/#" + multi["id"], wait_until="networkidle")
            expect(page.locator("#bar .title")).to_have_text("Multi Viewer")
            expect(page.locator(".screen-tree .screen-item")).to_have_count(2)
            expect(page.locator("#version-list .version-card")).to_have_count(2)
            page.locator('#mode-switch button[data-mode="compare"]').click()
            expect(page.locator("#viewer")).to_have_class("compare")
            _capture(page, "viewer-comparison")
            page.locator('.screen-item').nth(1).click()
            page.locator('#scope-switch button[data-scope="screen"]').click()
            expect(page.locator("#version-list .version-card")).to_have_count(2)
            page.locator("#copy").click()
            assert page.evaluate("navigator.clipboard.readText()") == f"{base_url}/v/{multi['id']}/details/"
            assert client.get(f"/v/{multi['id']}/").status_code == 200
            assert client.get(f"/v/{multi['id']}/v1/").status_code == 200
            _wait_until(lambda: client.get(f"/api/v1/decks/{multi['id']}").json()["last_viewed_at"] is not None,
                        "Recent visit did not persist")
            page.locator("#to-library").click()
            page.locator("#nav-recent").click()
            expect(_card(page, multi["id"])).to_be_visible()
            expected_final = _assert_dom_matches_server(client, page)
            page.reload(wait_until="networkidle")
            assert _assert_dom_matches_server(client, page) == expected_final
            assert native_dialogs == [] and page_errors == []
            context.close()
            browser.close()


def test_large_group_select_all_moves_hidden_items_in_one_request(connected_server):
    base_url, token = connected_server
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
        source = _create_group(client, "Large source")
        target = _create_group(client, "Destination")
        decks = [_create_deck(client, f"Batch {index:02d}", f"batch-{index}") for index in range(30)]
        ids = [deck["id"] for deck in decks]
        response = client.post("/api/v1/decks/move", json={"deck_ids": ids, "group_id": source["id"], "index": 0})
        assert response.status_code == 200, response.text
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(base_url + "/", wait_until="networkidle")
            _choose_scope(page, source["id"])
            expect(page.locator("#lib-groups .lib-card")).to_have_count(24)
            expect(page.locator("#load-more")).to_be_visible()
            _capture(page, "large-library-desktop")
            requests = []
            page.on("request", lambda request: requests.append(request.post_data_json)
                    if request.method == "POST" and request.url.endswith("/api/v1/decks/move") else None)
            _enable_selection(page)
            page.locator("#select-all").click()
            _card(page, ids[0]).drag_to(_project(page, target["id"]))
            _wait_until(lambda: _section_ids(client.get("/api/v1/library").json(), target["id"]) == ids,
                        "Full filtered selection did not move")
            assert len(requests) == 1
            assert requests[0]["deck_ids"] == ids
            assert _section_ids(client.get("/api/v1/library").json(), source["id"]) == []
            page.reload(wait_until="networkidle")
            _wait_for_section(client, page, target["id"], ids)

            # A small touch viewport can use the bulk fallback without sidebar
            # dragging or horizontal page overflow.
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            _capture(page, "large-library-mobile")
            # Moving all selected decks back to Ungrouped clears their membership.
            _enable_selection(page)
            page.locator("#select-all").click()
            page.locator("#move-target").select_option("ungrouped")
            page.locator("#bulk-move").click()
            _wait_until(lambda: _section_ids(client.get("/api/v1/library").json(), None) == ids,
                        "Mobile bulk move did not persist")
            page.set_viewport_size({"width": 1440, "height": 1000})
            _wait_for_section(client, page, None, ids)
            assert all(deck["group_id"] is None for deck in client.get("/api/v1/library").json()["ungrouped"]["decks"])
            browser.close()


def test_control_and_shift_selection_drag_as_one_ordered_batch(connected_server):
    base_url, token = connected_server
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
        source = _create_group(client, "Range source")
        target = _create_group(client, "Range destination")
        decks = [_create_deck(client, f"Range {index}", f"range-{index}") for index in range(6)]
        ids = [deck["id"] for deck in decks]
        assert client.post("/api/v1/decks/move", json={
            "deck_ids": ids, "group_id": source["id"], "index": 0,
        }).status_code == 200
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(base_url + "/", wait_until="networkidle")
            _choose_scope(page, source["id"])
            requests = []
            page.on("request", lambda request: requests.append(request.post_data_json)
                    if request.method == "POST" and request.url.endswith("/api/v1/decks/move") else None)
            _card(page, ids[1]).click(modifiers=["Control"])
            _card(page, ids[4]).click(modifiers=["Shift"])
            expect(page.locator("#library")).to_be_visible()
            _card(page, ids[2]).drag_to(_project(page, target["id"]))
            _wait_until(lambda: _section_ids(client.get("/api/v1/library").json(), target["id"])
                        == ids[1:5], "Range selection did not move the contiguous ordered set")
            assert len(requests) == 1
            assert requests[0]["deck_ids"] == ids[1:5]
            assert _section_ids(client.get("/api/v1/library").json(), source["id"]) == [ids[0], ids[5]]
            browser.close()


def test_legacy_favorites_merge_once_and_follow_server_changes(connected_server):
    base_url, token = connected_server
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
        old = _create_deck(client, "Browser favorite", "favorite-old")
        shared = _create_deck(client, "API favorite", "favorite-shared")
        assert client.put(f"/api/v1/decks/{shared['id']}/favorite", json={"favorite": True}).status_code == 200
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context()
            page = context.new_page()
            page.add_init_script("localStorage.setItem('mg.library.pinned', " + json.dumps(json.dumps([old["id"]])) + ");")
            page.goto(base_url + "/", wait_until="networkidle")
            _wait_until(lambda: client.get(f"/api/v1/decks/{old['id']}").json()["favorite"], "Old browser favorite not migrated")
            page.locator("#nav-pinned").click()
            expect(_card(page, old["id"])).to_be_visible()
            expect(_card(page, shared["id"])).to_be_visible()
            assert client.put(f"/api/v1/decks/{old['id']}/favorite", json={"favorite": False}).status_code == 200
            page.reload(wait_until="networkidle")
            page.locator("#nav-pinned").click()
            expect(_card(page, old["id"])).to_have_count(0)
            expect(_card(page, shared["id"])).to_be_visible()
            assert not client.get(f"/api/v1/decks/{old['id']}").json()["favorite"]
            second = browser.new_context()
            fresh_page = second.new_page()
            fresh_page.goto(base_url + "/", wait_until="networkidle")
            fresh_page.locator("#nav-pinned").click()
            expect(_card(fresh_page, shared["id"])).to_be_visible()
            expect(_card(fresh_page, old["id"])).to_have_count(0)
            browser.close()


def test_version_note_ui_and_api_share_persistent_metadata(connected_server):
    base_url, token = connected_server
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
        deck = _create_deck(client, "Version notes", "notes-ui-v1")
        second = client.post(
            f"/api/v1/decks/{deck['id']}/versions",
            files={"file": ("deck.zip", _zip({"index.html": "<html><body>Second version</body></html>"}), "application/zip")},
            data={"idempotency_key": "notes-ui-v2", "note": "Initial API note"},
        )
        assert second.status_code == 201
        version_path = f"/api/v1/decks/{deck['id']}/versions"
        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            requests = []
            page.on("request", lambda request: requests.append((request.method, request.url)))
            page.goto(base_url + "/#" + deck["id"], wait_until="networkidle")
            expect(page.locator('#version-list .version-card[data-version="2"] .version-note')).to_have_text("Initial API note")
            assert any(method == "POST" and url.endswith(f"/decks/{deck['id']}/visit") for method, url in requests)
            assert client.get(f"/api/v1/decks/{deck['id']}").json()["last_viewed_at"] is not None

            # Editing an old version changes its note, never the active pointer or
            # the version being previewed. The API sees the exact same text.
            older = page.locator('#version-list .version-card[data-version="1"]')
            older.click()
            expect(older).to_have_class("version-card selectable viewing")
            expect(page.locator("#frame-a")).to_have_attribute("src", f"/v/{deck['id']}/v1/")
            preview_before = page.locator("#frame-a").get_attribute("src")
            older.locator('.edit-note[data-action="edit-note"]').click()
            page.locator("#dialog-note").fill("Saved from the console\nReview <spacing> & contrast")
            page.locator("#dialog-confirm").click()
            expect(older.locator(".version-note")).to_have_text("Saved from the console\nReview <spacing> & contrast")
            expect(older).to_have_class("version-card selectable viewing")
            assert page.locator("#frame-a").get_attribute("src") == preview_before
            metadata = client.get(version_path).json()
            assert metadata["active_version"] == 2
            assert next(item for item in metadata["items"] if item["version"] == 1)["note"] == "Saved from the console\nReview <spacing> & contrast"

            # Another agent can update the same metadata. Refresh retrieves that
            # API change, and a later full reload retains it as well.
            updated = client.patch(version_path + "/1", json={"note": "Updated by an API agent"})
            assert updated.status_code == 200
            page.locator("#library-refresh").click()
            expect(older.locator(".version-note")).to_have_text("Updated by an API agent")
            page.reload(wait_until="networkidle")
            expect(older.locator(".version-note")).to_have_text("Updated by an API agent")
            assert client.get(version_path).json()["active_version"] == 2
            browser.close()


def test_console_upload_download_round_trip_and_failure_ui(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    manifest = {
        "screens": [
            {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
            {"key": "details", "tag": "Details", "entry": "details/index.html"},
        ]
    }

    def deck_payload(overview: str, details: str) -> bytes:
        return _zip(
            {
                "screens.json": json.dumps(manifest),
                "index.html": "<!doctype html><html><body>Deck root</body></html>",
                "overview/index.html": f"<!doctype html><html><body>{overview}</body></html>",
                "details/index.html": f"<!doctype html><html><body>{details}</body></html>",
            }
        )

    version_one = deck_payload("overview one", "details one")
    version_two = deck_payload("overview one", "details two")
    version_three = deck_payload("overview three", "details two")

    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        older = _create_deck(client, "Older deck", "ui-upload-older")

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            upload_requests: list[str] = []

            def record_request(request) -> None:
                if request.method == "POST" and (
                    request.url.endswith("/api/v1/decks") or "/versions" in request.url
                ):
                    upload_requests.append(request.url)

            page.on("request", record_request)
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()

            # Both fields are required, and dispatching confirm twice still resolves
            # one dialog promise and creates exactly one multipart request.
            page.locator("#library-more").click()
            _menu_action(page, "upload").click()
            expect(page.locator("#dialog-title")).to_have_text(_label(page, "uploadDeck"))
            expect(page.locator(".upload-dropzone")).to_be_visible()
            expect(page.locator(".upload-dropzone")).to_contain_text(_label(page, "dropZip"))
            expect(page.locator("#dialog-confirm")).to_be_disabled()
            page.locator('[data-dialog-field="name"]').fill("Uploaded deck")
            expect(page.locator("#dialog-confirm")).to_be_disabled()
            page.locator('[data-dialog-field="file"]').set_input_files(
                {"name": "deck.zip", "mimeType": "application/zip", "buffer": version_one}
            )
            expect(page.locator("#dialog-confirm")).to_be_enabled()
            before_uploads = len(upload_requests)
            page.locator("#dialog-confirm").evaluate("button => { button.click(); button.click(); }")
            expect(page.locator("#dialog-overlay")).to_be_hidden()
            _wait_until(
                lambda: client.get("/api/v1/library").json()["ungrouped"]["decks"][0]["name"]
                == "Uploaded deck",
                "Uploaded deck did not appear at the front of Ungrouped",
                timeout=20,
            )
            library = client.get("/api/v1/library").json()
            assert [deck["id"] for deck in library["ungrouped"]["decks"]][1] == older["id"]
            uploaded = library["ungrouped"]["decks"][0]
            assert len(upload_requests) == before_uploads + 1
            expect(_card(page, uploaded["id"])).to_be_visible()

            _card(page, uploaded["id"]).click()
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#viewer-more")).to_be_visible()
            expect(page.locator("#download-version")).to_be_enabled()
            assert page.locator("#download-version").get_attribute("aria-label") == _label(page, "download")

            def upload_version(payload: bytes, expected_active: int) -> None:
                before = len(upload_requests)
                page.locator("#viewer-more").click()
                _menu_action(page, "upload-version").click()
                expect(page.locator("#dialog-title")).to_have_text(_label(page, "uploadVersion"))
                expect(page.locator("#dialog-confirm")).to_be_disabled()
                page.locator(".upload-dropzone").evaluate(
                    """(dropzone, encoded) => {
                        const bytes = Uint8Array.from(atob(encoded), char => char.charCodeAt(0));
                        const transfer = new DataTransfer();
                        transfer.items.add(new File([bytes], "deck.zip", {type: "application/zip"}));
                        dropzone.dispatchEvent(new DragEvent("dragover", {
                            bubbles: true, cancelable: true, dataTransfer: transfer
                        }));
                        dropzone.dispatchEvent(new DragEvent("drop", {
                            bubbles: true, cancelable: true, dataTransfer: transfer
                        }));
                    }""",
                    base64.b64encode(payload).decode("ascii"),
                )
                expect(page.locator(".upload-file-name")).to_have_text("deck.zip")
                expect(page.locator("#dialog-confirm")).to_be_enabled()
                page.locator("#dialog-confirm").evaluate(
                    "button => { button.click(); button.click(); }"
                )
                _wait_until(
                    lambda: client.get(
                        f"/api/v1/decks/{uploaded['id']}/versions"
                    ).json()["active_version"]
                    == expected_active,
                    f"Uploaded version {expected_active} did not become active",
                    timeout=20,
                )
                expect(page.locator("#dialog-overlay")).to_be_hidden()
                expect(page.locator("#viewer-more")).to_be_enabled(timeout=20_000)
                assert len(upload_requests) == before + 1

            upload_version(version_two, 2)
            upload_version(version_three, 3)

            # Overview changed only in deck versions 1 and 3. Screen history labels
            # those entries v1/v2, but Download ZIP must still request deck version 3.
            page.locator('#scope-switch button[data-scope="screen"]').click()
            expect(page.locator("#history-count")).to_contain_text("2")
            expect(page.locator(".version-card.viewing .version-number")).to_have_text("v2")
            with page.expect_download(timeout=20_000) as download_info:
                page.locator("#download-version").click()
            downloaded = download_info.value
            assert downloaded.suggested_filename == f"{uploaded['id']}_v3.zip"
            downloaded_bytes = Path(downloaded.path()).read_bytes()
            with zipfile.ZipFile(io.BytesIO(downloaded_bytes)) as archive:
                assert b"overview three" in archive.read("overview/index.html")

            # The exact source archive returned by Download ZIP can be registered as
            # the next version of the same deck and becomes active.
            upload_version(downloaded_bytes, 4)
            history = client.get(f"/api/v1/decks/{uploaded['id']}/versions").json()
            assert [item["version"] for item in history["items"] if item["status"] == "ready"] == [
                1,
                2,
                3,
                4,
            ]

            # A malformed archive uses the custom error dialog and cannot disturb
            # the active pointer. The double confirm still produces one POST.
            before = len(upload_requests)
            page.locator("#viewer-more").click()
            _menu_action(page, "upload-version").click()
            page.locator('[data-dialog-field="file"]').set_input_files(
                {"name": "broken.zip", "mimeType": "application/zip", "buffer": b"not a zip"}
            )
            page.locator("#dialog-confirm").evaluate("button => { button.click(); button.click(); }")
            expect(page.locator("#dialog-title")).to_have_text(
                _label(page, "actionFailed"), timeout=20_000
            )
            expect(page.locator("#dialog-body")).not_to_be_empty()
            assert len(upload_requests) == before + 1
            assert client.get(
                f"/api/v1/decks/{uploaded['id']}/versions"
            ).json()["active_version"] == 4
            page.locator("#dialog-confirm").click()

            context.close()
            browser.close()
