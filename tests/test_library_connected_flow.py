"""Connected browser regression for Library organization and Viewer continuity."""

from __future__ import annotations

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


def _dom_section_ids(page: Page, group_id: str | None) -> list[str]:
    value = "" if group_id is None else group_id
    return page.locator(
        f'#lib-groups > .lib-group[data-group-id="{value}"] .lib-card[data-organizable="true"]'
    ).evaluate_all("cards => cards.map(card => card.dataset.deckId)")


def _wait_for_section(
    client: httpx.Client,
    page: Page,
    group_id: str | None,
    expected: list[str],
) -> None:
    try:
        _wait_until(
            lambda: _section_ids(client.get("/api/v1/library").json(), group_id) == expected
            and _dom_section_ids(page, group_id) == expected,
            f"Section {group_id!r} did not reach {expected!r}",
        )
    except AssertionError as error:
        server_ids = _section_ids(client.get("/api/v1/library").json(), group_id)
        dom_ids = _dom_section_ids(page, group_id)
        raise AssertionError(f"{error}; server={server_ids!r}; dom={dom_ids!r}") from error


def _assert_dom_matches_server(client: httpx.Client, page: Page) -> dict:
    library = client.get("/api/v1/library").json()
    dom_groups = page.locator("#lib-groups > .lib-group").evaluate_all(
        "groups => groups.map(group => group.dataset.groupId)"
    )
    assert dom_groups == [group["id"] for group in library["groups"]] + [""]
    for group in library["groups"]:
        assert _dom_section_ids(page, group["id"]) == [deck["id"] for deck in group["decks"]]
        assert [deck["sort_order"] for deck in group["decks"]] == list(range(len(group["decks"])))
    assert _dom_section_ids(page, None) == [deck["id"] for deck in library["ungrouped"]["decks"]]
    assert [deck["sort_order"] for deck in library["ungrouped"]["decks"]] == list(
        range(len(library["ungrouped"]["decks"]))
    )
    return library


def _card(page: Page, deck_id: str):
    return page.locator(
        f'#lib-groups .lib-card[data-deck-id="{deck_id}"][data-organizable="true"]'
    )


def _open_move_dialog(page: Page, deck_id: str) -> None:
    _card(page, deck_id).locator(".lib-kebab").click()
    page.get_by_role("menuitem", name="Move…", exact=True).click()
    expect(page.locator("#dialog-overlay")).to_be_visible()
    expect(page.locator("#dialog-cancel")).to_be_focused()


def _confirm_dialog_move(page: Page, group_id: str | None, index: int) -> None:
    group_value = "ungrouped" if group_id is None else f"group:{group_id}"
    page.locator("#dialog-move-group").select_option(group_value)
    page.locator("#dialog-move-index").select_option(str(index))
    page.get_by_role("button", name="Move", exact=True).click()


def test_library_connected_flow_persists_rolls_back_and_preserves_viewer(connected_server):
    base_url, token = connected_server
    headers = {"Authorization": f"Bearer {token}"}
    manifest = {
        "screens": [
            {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
            {"key": "details", "tag": "Details", "entry": "details/index.html"},
        ]
    }
    multi_v1 = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>multi one</body></html>",
            "overview/index.html": "<!doctype html><html><body>overview one</body></html>",
            "details/index.html": "<!doctype html><html><body>details one</body></html>",
        }
    )
    multi_v2 = _zip(
        {
            "screens.json": json.dumps(manifest),
            "index.html": "<!doctype html><html><body>multi two</body></html>",
            "overview/index.html": "<!doctype html><html><body>overview two</body></html>",
            "details/index.html": "<!doctype html><html><body>details two</body></html>",
        }
    )

    with httpx.Client(base_url=base_url, headers=headers, timeout=20) as client:
        source = _create_group(client, "Source")
        target = _create_group(client, "Target")
        empty = _create_group(client, "Empty")
        first = _create_deck(client, "Visible A", "connected-a")
        hidden = _create_deck(client, "Hidden B", "connected-b")
        third = _create_deck(client, "Visible C", "connected-c")
        other = _create_deck(client, "Target D", "connected-d")
        multi = _create_deck(client, "Multi Viewer", "connected-multi", multi_v1)
        added = client.post(
            f"/api/v1/decks/{multi['id']}/versions",
            files={"file": ("deck.zip", multi_v2, "application/zip")},
            data={"idempotency_key": "connected-multi-v2"},
        )
        assert added.status_code == 201, added.text
        for index, deck in enumerate([first, hidden, third]):
            _move_deck(client, deck["id"], source["id"], index)
        _move_deck(client, other["id"], target["id"], 0)

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            context = browser.new_context(
                viewport={"width": 1100, "height": 800},
                has_touch=True,
                permissions=["clipboard-read", "clipboard-write"],
            )
            page = context.new_page()
            console_errors: list[str] = []
            page_errors: list[str] = []
            expected_move_failures = {"active": False}

            def record_console(message) -> None:
                if message.type != "error":
                    return
                url = (message.location or {}).get("url", "")
                if expected_move_failures["active"] and "/move" in url:
                    return
                console_errors.append(message.text)

            page.on("console", record_console)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            move_requests: list[str] = []
            library_reads: list[str] = []

            def record_request(request) -> None:
                if request.method == "POST" and request.url.endswith("/move"):
                    move_requests.append(request.url)
                if request.method == "GET" and request.url.endswith("/api/v1/library"):
                    library_reads.append(request.url)

            page.on("request", record_request)
            page.goto(base_url + "/", wait_until="networkidle")
            expect(page.locator("#library")).to_be_visible()
            _assert_dom_matches_server(client, page)

            # Search hides one sibling, but the actual drag index still comes from
            # the complete server list and the target deck id.
            page.locator("#lib-search").fill("Visible")
            before_posts, before_reads = len(move_requests), len(library_reads)
            _card(page, third["id"]).drag_to(
                _card(page, first["id"]), target_position={"x": 20, "y": 3}
            )
            expected_source = [third["id"], first["id"], hidden["id"]]
            _wait_until(
                lambda: _section_ids(client.get("/api/v1/library").json(), source["id"])
                == expected_source
                and _dom_section_ids(page, source["id"]) == [third["id"], first["id"]],
                "Filtered same-group drag did not preserve the hidden sibling",
            )
            assert len(move_requests) == before_posts + 1
            assert len(library_reads) >= before_reads + 1
            page.locator("#lib-search").fill("")
            _wait_for_section(client, page, source["id"], expected_source)
            page.reload(wait_until="networkidle")
            _wait_for_section(client, page, source["id"], expected_source)

            # Cross-group placement uses the visible target id, not a DOM-wide index.
            before_posts, before_reads = len(move_requests), len(library_reads)
            _card(page, hidden["id"]).drag_to(
                _card(page, other["id"]), target_position={"x": 20, "y": 3}
            )
            _wait_for_section(client, page, target["id"], [hidden["id"], other["id"]])
            assert len(move_requests) == before_posts + 1
            assert len(library_reads) >= before_reads + 1

            # A collapsed empty header accepts a deck at index zero and stays collapsed.
            empty_section = page.locator(f'.lib-group[data-group-id="{empty["id"]}"]')
            empty_section.locator(".lib-group-toggle").click()
            expect(empty_section).to_have_class("lib-group collapsed")
            collapsed_before = page.evaluate("localStorage.getItem('mg.library.collapsed')")
            _card(page, first["id"]).drag_to(empty_section.locator(".lib-group-header"))
            _wait_for_section(client, page, empty["id"], [first["id"]])
            expect(empty_section).to_have_class("lib-group collapsed")
            assert page.evaluate("localStorage.getItem('mg.library.collapsed')") == collapsed_before
            page.reload(wait_until="networkidle")
            expect(page.locator(f'.lib-group[data-group-id="{empty["id"]}"]')).to_have_class(
                "lib-group collapsed"
            )

            # Exact Ungrouped placement is also a single POST followed by a GET.
            before_posts, before_reads = len(move_requests), len(library_reads)
            _card(page, other["id"]).drag_to(
                _card(page, multi["id"]), target_position={"x": 20, "y": 3}
            )
            _wait_for_section(client, page, None, [other["id"], multi["id"]])
            assert len(move_requests) == before_posts + 1
            assert len(library_reads) >= before_reads + 1

            # Group drag reorders only real groups; Ungrouped remains last.
            empty_handle = page.locator(f'.lib-group[data-group-id="{empty["id"]}"] .lib-group-drag-handle')
            source_section = page.locator(f'.lib-group[data-group-id="{source["id"]}"]')
            before_posts = len(move_requests)
            groups_before_drag = [
                group["id"] for group in client.get("/api/v1/library").json()["groups"]
            ]
            empty_handle.drag_to(source_section, target_position={"x": 30, "y": 2})
            _wait_until(
                lambda: [group["id"] for group in client.get("/api/v1/library").json()["groups"]]
                != groups_before_drag,
                "Group drag did not persist",
            )
            dragged_groups = [
                group["id"] for group in client.get("/api/v1/library").json()["groups"]
            ]
            assert sorted(dragged_groups) == sorted(groups_before_drag)
            assert abs(dragged_groups.index(empty["id"]) - dragged_groups.index(source["id"])) == 1
            assert len(move_requests) == before_posts + 1
            assert page.locator("#lib-groups > .lib-group").evaluate_all(
                "groups => groups.map(group => group.dataset.groupId)"
            )[-1] == ""

            # Deck Move is fully keyboard-operable and restores focus after reload.
            deck_kebab = _card(page, third["id"]).locator(".lib-kebab")
            deck_kebab.focus()
            page.keyboard.press("Enter")
            expect(page.get_by_role("menuitem", name="Rename", exact=True)).to_be_focused()
            page.keyboard.press("ArrowDown")
            expect(page.get_by_role("menuitem", name="Move…", exact=True)).to_be_focused()
            page.keyboard.press("Enter")
            expect(page.locator("#dialog-cancel")).to_be_focused()
            page.keyboard.press("Shift+Tab")
            page.keyboard.press("Shift+Tab")
            expect(page.locator("#dialog-move-group")).to_be_focused()
            page.keyboard.press("Home")
            for _ in range(dragged_groups.index(target["id"])):
                page.keyboard.press("ArrowDown")
            page.keyboard.press("Tab")
            page.keyboard.press("End")
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            page.keyboard.press("Enter")
            _wait_for_section(client, page, target["id"], [hidden["id"], third["id"]])
            expect(_card(page, third["id"]).locator(".lib-kebab")).to_be_focused()

            # Group Reorder uses the same keyboard menu and dispatcher.
            keyboard_group_id = dragged_groups[0]
            expected_keyboard_groups = dragged_groups[1:] + [keyboard_group_id]
            group_kebab = page.locator(
                f'.lib-group[data-group-id="{keyboard_group_id}"] .lib-group-kebab'
            )
            group_kebab.focus()
            page.keyboard.press("Enter")
            expect(page.get_by_role("menuitem", name="Rename", exact=True)).to_be_focused()
            page.keyboard.press("ArrowDown")
            expect(page.get_by_role("menuitem", name="Reorder…", exact=True)).to_be_focused()
            page.keyboard.press("Enter")
            expect(page.locator("#dialog-cancel")).to_be_focused()
            page.keyboard.press("Shift+Tab")
            expect(page.locator("#dialog-group-index")).to_be_focused()
            page.keyboard.press("End")
            page.keyboard.press("Tab")
            page.keyboard.press("Tab")
            page.keyboard.press("Enter")
            _wait_until(
                lambda: [group["id"] for group in client.get("/api/v1/library").json()["groups"]]
                == expected_keyboard_groups,
                "Keyboard group reorder did not persist",
            )
            expect(
                page.locator(f'.lib-group[data-group-id="{keyboard_group_id}"] .lib-group-kebab')
            ).to_be_focused()

            # A 360px touch viewport can perform the same move path without overflow.
            page.set_viewport_size({"width": 360, "height": 740})
            _card(page, hidden["id"]).locator(".lib-kebab").tap()
            page.get_by_role("menuitem", name="Move…", exact=True).tap()
            page.locator("#dialog-move-group").select_option("ungrouped")
            page.locator("#dialog-move-index").select_option("2")
            dialog_box = page.locator("#dialog-box").bounding_box()
            assert dialog_box and dialog_box["x"] >= 0 and dialog_box["x"] + dialog_box["width"] <= 360
            page.get_by_role("button", name="Move", exact=True).tap()
            _wait_for_section(client, page, None, [other["id"], multi["id"], hidden["id"]])
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.set_viewport_size({"width": 1100, "height": 800})

            # Inject one failed move: optimistic state rolls back, then the GET record
            # wins before the custom failure dialog is shown.
            server_before_failure = client.get("/api/v1/library").json()
            failed_once = {"done": False}

            def fail_next_move(route) -> None:
                if not failed_once["done"]:
                    failed_once["done"] = True
                    route.fulfill(
                        status=500,
                        content_type="application/json",
                        body=json.dumps({"detail": {"code": "injected", "message": "Injected move failure."}}),
                    )
                else:
                    route.continue_()

            page.route("**/api/v1/decks/*/move", fail_next_move)
            expected_move_failures["active"] = True
            _open_move_dialog(page, third["id"])
            _confirm_dialog_move(page, source["id"], 0)
            expect(page.locator("#dialog-title")).to_have_text("Could not move this deck.")
            assert failed_once["done"]
            assert _section_ids(client.get("/api/v1/library").json(), target["id"]) == _section_ids(
                server_before_failure, target["id"]
            )
            assert _dom_section_ids(page, target["id"]) == _section_ids(server_before_failure, target["id"])
            page.get_by_role("button", name="OK", exact=True).click()
            page.unroute("**/api/v1/decks/*/move", fail_next_move)
            expected_move_failures["active"] = False

            # A second actor deletes the stale destination while its dialog is open.
            _open_move_dialog(page, other["id"])
            page.locator("#dialog-move-group").select_option(f"group:{source['id']}")
            page.locator("#dialog-move-index").select_option("0")
            deleted = client.delete(f"/api/v1/groups/{source['id']}")
            assert deleted.status_code == 204
            expected_move_failures["active"] = True
            page.get_by_role("button", name="Move", exact=True).click()
            expect(page.locator("#dialog-title")).to_have_text("Could not move this deck.")
            page.get_by_role("button", name="OK", exact=True).click()
            expected_move_failures["active"] = False
            final_library = _assert_dom_matches_server(client, page)
            assert source["id"] not in [group["id"] for group in final_library["groups"]]

            # Search temporarily expands a collapsed matching group without changing
            # its stored preference. The shared dialog ignores Escape and backdrop.
            page.locator("#lib-search").fill("Empty")
            visible_empty = page.locator(f'.lib-group[data-group-id="{empty["id"]}"]')
            expect(visible_empty.locator(f'.lib-card[data-deck-id="{first["id"]}"]')).to_be_visible()
            assert empty["id"] in json.loads(page.evaluate("localStorage.getItem('mg.library.collapsed')"))
            page.locator("#lib-search").fill("")
            expect(page.locator(f'.lib-group[data-group-id="{empty["id"]}"]')).to_have_class(
                "lib-group collapsed"
            )
            page.locator("#lib-new-group").click()
            expect(page.locator("#dialog-cancel")).to_be_focused()
            page.keyboard.press("Escape")
            expect(page.locator("#dialog-overlay")).to_be_visible()
            page.locator("#dialog-overlay").click(position={"x": 2, "y": 2})
            expect(page.locator("#dialog-overlay")).to_be_visible()
            page.locator("#dialog-cancel").click()

            # Core CRUD, pin, and recent behavior stays connected.
            page.locator("#lib-new-group").click()
            page.locator("#dialog-input").fill("Temporary")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(page.locator(".lib-group-name", has_text="Temporary")).to_be_visible()
            temporary_section = page.locator(".lib-group", has=page.locator(".lib-group-name", has_text="Temporary"))
            temporary_section.locator(".lib-group-kebab").click()
            page.get_by_role("menuitem", name="Delete", exact=True).click()
            expect(page.locator("#dialog-cancel")).to_be_focused()
            page.get_by_role("button", name="Delete group", exact=True).click()
            expect(page.locator(".lib-group-name", has_text="Temporary")).to_have_count(0)

            _card(page, third["id"]).locator(".lib-kebab").click()
            page.get_by_role("menuitem", name="Rename", exact=True).click()
            page.locator("#dialog-input").fill("Visible C renamed")
            page.get_by_role("button", name="Save", exact=True).click()
            expect(_card(page, third["id"]).locator(".lib-card-name")).to_have_text("Visible C renamed")
            _card(page, multi["id"]).locator(".lib-pin").click()
            expect(page.locator("#lib-pinned-row")).to_be_visible()

            # Direct hash entry and the complete Viewer surface keep their original URLs
            # and behaviors after Library organization changes.
            page.goto(base_url + "/#" + multi["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#bar .title")).to_have_text("Multi Viewer")
            expect(page.locator(".screen-tree .screen-item")).to_have_count(2)
            expect(page.locator("#version-list .version-card")).to_have_count(2)
            expect(page.locator("#scope-switch")).to_be_visible()
            expect(page.locator("#version-list .set-active-btn:visible")).to_have_count(1)
            assert f"/v/{multi['id']}/" in page.locator("#frame-a").get_attribute("src")
            page.locator('#mode-switch button[data-mode="compare"]').click()
            expect(page.locator("#viewer")).to_have_class("compare")
            assert f"/v/{multi['id']}/v" in page.locator("#frame-a").get_attribute("src")
            page.locator('.screen-item').nth(1).click()
            page.locator('#scope-switch button[data-scope="screen"]').click()
            expect(page.locator("#version-list .version-card")).to_have_count(2)
            page.locator("#copy").click()
            copied_viewer = page.evaluate("navigator.clipboard.readText()")
            assert copied_viewer == f"{base_url}/v/{multi['id']}/details/"
            stable = client.get(f"/v/{multi['id']}/")
            versioned = client.get(f"/v/{multi['id']}/v1/")
            assert stable.status_code == versioned.status_code == 200

            page.locator("#to-library").click()
            expect(page.locator("#library")).to_be_visible()
            expect(page.locator("#lib-recent-row")).to_be_visible()
            _card(page, multi["id"]).locator(".lib-kebab").click()
            page.get_by_role("menuitem", name="Copy link", exact=True).click()
            assert page.evaluate("navigator.clipboard.readText()") == f"{base_url}/v/{multi['id']}/"

            # Server order, not localStorage, restores on reload and in a new page.
            expected_final = _assert_dom_matches_server(client, page)
            storage_keys = set(page.evaluate("Object.keys(localStorage)"))
            assert storage_keys <= {
                "mg.library.pinned",
                "mg.library.recent",
                "mg.library.collapsed",
                "mg.rail.hidden",
            }
            page.reload(wait_until="networkidle")
            assert _assert_dom_matches_server(client, page) == expected_final
            second_page = context.new_page()
            second_page.goto(base_url + "/", wait_until="networkidle")
            _assert_dom_matches_server(client, second_page)
            second_page.close()

            assert page_errors == []
            assert console_errors == []
            context.close()
            browser.close()