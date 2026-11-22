"""Connected browser coverage for hierarchy navigation and safe group moves."""

from __future__ import annotations

import httpx
from playwright.sync_api import expect, sync_playwright

from test_library_connected_flow import (
    _card,
    _capture,
    _choose_scope,
    _create_deck,
    _create_group,
    _enable_selection,
    _launch_browser,
    _menu_action,
    _move_deck,
    _open_move_dialog,
    _project,
    _section_ids,
    connected_server,
)


def _create_child_in_browser(page, parent_id, name):
    _project(page, parent_id).locator(".lib-group-kebab").click()
    _menu_action(page, "new-subgroup").click()
    expect(page.locator("#dialog-group-parent")).to_have_value(f"group:{parent_id}")
    page.locator("#dialog-input").fill(name)
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url.endswith("/api/v1/groups")) as created:
        page.locator("#dialog-confirm").click()
    assert created.value.status == 201
    group = created.value.json()
    assert group["parent_id"] == parent_id
    expect(_project(page, group["id"])).to_be_visible()
    return group


def test_three_level_navigation_moves_and_viewer_paths(connected_server):
    base_url, token = connected_server
    with httpx.Client(base_url=base_url, headers={"Authorization": f"Bearer {token}"}, timeout=20) as client:
        project = _create_group(client, "FlowGate")
        archive = _create_group(client, "Archive")
        other = _create_group(client, "Other project")
        other_version = client.post("/api/v1/groups", json={"name": "v0.2", "parent_id": other["id"]})
        assert other_version.status_code == 201
        other_version = other_version.json()
        direct = _create_deck(client, "Project overview", "hierarchy-browser-direct")
        leaf = _create_deck(client, "Screen layout", "hierarchy-browser-leaf")
        loose = _create_deck(client, "Unclassified design", "hierarchy-browser-loose")
        _move_deck(client, direct["id"], project["id"], 0)

        with sync_playwright() as playwright:
            browser = _launch_browser(playwright)
            context = browser.new_context(viewport={"width": 1280, "height": 900})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base_url + "/", wait_until="networkidle")

            version = _create_child_in_browser(page, project["id"], "v0.2")
            layout = _create_child_in_browser(page, version["id"], "Layout")
            expect(_project(page, layout["id"])).to_have_attribute("data-depth", "2")
            _move_deck(client, leaf["id"], layout["id"], 0)
            page.reload(wait_until="networkidle")

            # Direct membership is the default even when descendants contain decks.
            _choose_scope(page, project["id"])
            expect(page.locator("#include-subgroups")).not_to_be_checked()
            expect(page.locator("#lib-groups .lib-card")).to_have_count(1)
            expect(_card(page, direct["id"])).to_be_visible()
            page.locator("#include-subgroups").check()
            expect(page.locator("#lib-groups .lib-card")).to_have_count(2)
            expect(_card(page, leaf["id"]).locator(".thumb-overlay")).to_have_text("FlowGate / v0.2 / Layout")
            expect(page.locator('#lib-sort option[value="manual"]')).to_be_disabled()
            _capture(page, "hierarchy-include-subgroups")
            page.locator("#include-subgroups").uncheck()
            expect(page.locator("#lib-groups .lib-card")).to_have_count(1)

            # Collapse is retained across a real reload, independently of selection.
            page.locator("#nav-all").click()
            _project(page, project["id"]).locator(".group-toggle").click()
            expect(_project(page, project["id"]).locator(".group-toggle")).to_have_attribute("aria-expanded", "false")
            expect(_project(page, version["id"])).to_have_count(0)
            page.reload(wait_until="networkidle")
            expect(_project(page, version["id"])).to_have_count(0)
            _project(page, project["id"]).locator(".group-toggle").click()
            expect(_project(page, layout["id"])).to_be_visible()
            _choose_scope(page, layout["id"])
            expect(page.locator("#current-crumb")).to_have_text("FlowGate / v0.2 / Layout")
            expect(_project(page, layout["id"]).locator(".side-link")).to_have_attribute("aria-current", "page")
            _capture(page, "hierarchy-three-level")

            # Group moves exclude the group and all descendants as destinations.
            _project(page, version["id"]).locator(".lib-group-kebab").click()
            _menu_action(page, "move").click()
            options = page.locator("#dialog-group-parent option").evaluate_all(
                "options => options.map(option => option.value)"
            )
            assert f"group:{version['id']}" not in options
            assert f"group:{layout['id']}" not in options
            page.locator("#dialog-group-parent").select_option(f"group:{archive['id']}")
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith(f"/groups/{version['id']}/move")) as moved:
                page.locator("#dialog-confirm").click()
            assert moved.value.status == 200
            expect(page.locator("#current-crumb")).to_have_text("Archive / v0.2 / Layout")
            library = client.get("/api/v1/library").json()
            assert _section_ids(library, layout["id"]) == [leaf["id"]]
            assert _section_ids(library, project["id"]) == [direct["id"]]
            assert next(group for group in library["groups"] if group["id"] == version["id"])["parent_id"] == archive["id"]

            # Nonempty-group deletion explains the block and preserves the tree.
            _project(page, archive["id"]).locator(".lib-group-kebab").click()
            _menu_action(page, "delete").click()
            expect(page.locator("#dialog-overlay")).to_be_visible()
            expect(page.locator("#dialog-cancel")).to_be_hidden()
            page.locator("#dialog-confirm").click()
            assert client.get("/api/v1/library").json() == library

            # Destination labels distinguish equal leaf names by their full paths.
            page.locator("#nav-ungrouped").click()
            expect(page.locator("#lib-groups .lib-card")).to_have_count(1)
            expect(_card(page, loose["id"])).to_be_visible()
            _open_move_dialog(page, loose["id"])
            expect(page.locator(f'#dialog-move-group option[value="group:{version["id"]}"]')).to_have_text("Archive / v0.2")
            expect(page.locator(f'#dialog-move-group option[value="group:{other_version["id"]}"]')).to_have_text("Other project / v0.2")
            page.locator("#dialog-move-group").select_option(f"group:{layout['id']}")
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith("/api/v1/decks/move")) as decks_moved:
                page.locator("#dialog-confirm").click()
            assert decks_moved.value.status == 200
            assert set(_section_ids(client.get("/api/v1/library").json(), layout["id"])) == {leaf["id"], loose["id"]}
            assert _section_ids(client.get("/api/v1/library").json(), None) == []

            # Existing deck hashes reveal the current group path after a full load.
            page.goto(base_url + "/#" + leaf["id"], wait_until="networkidle")
            expect(page.locator("#app")).to_be_visible()
            expect(page.locator("#viewer-group")).to_have_text("Archive / v0.2 / Layout")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("Screen layout")
            expect(_project(page, layout["id"]).locator(".side-link")).to_have_attribute("aria-current", "page")
            _capture(page, "hierarchy-viewer-path")
            assert client.get(f"/v/{leaf['id']}/v1/").status_code == 200

            # Select-all in an ancestor's aggregate view moves descendant
            # memberships together, without touching another project's deck.
            _choose_scope(page, archive["id"])
            expect(page.locator("#lib-groups .lib-card")).to_have_count(0)
            page.locator("#include-subgroups").check()
            expect(page.locator("#lib-groups .lib-card")).to_have_count(2)
            _enable_selection(page)
            page.locator("#select-all").click()
            expect(page.locator("#lib-groups .lib-card.selected")).to_have_count(2)
            expect(page.locator(f'#move-target option[value="group:{other_version["id"]}"]')).to_have_text("Other project / v0.2")
            page.locator("#move-target").select_option(f"group:{other_version['id']}")
            _capture(page, "hierarchy-subgroups-selection")
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith("/api/v1/decks/move")) as bulk_moved:
                page.locator("#bulk-move").click()
            assert bulk_moved.value.status == 200
            moved_ids = bulk_moved.value.request.post_data_json["deck_ids"]
            assert len(moved_ids) == 2 and set(moved_ids) == {leaf["id"], loose["id"]}
            expect(page.locator("#lib-groups .lib-card")).to_have_count(0)
            library = client.get("/api/v1/library").json()
            target_ids = _section_ids(library, other_version["id"])
            assert len(target_ids) == 2 and set(target_ids) == {leaf["id"], loose["id"]}
            for group in (archive, version, layout):
                assert _section_ids(library, group["id"]) == []
            assert _section_ids(library, project["id"]) == [direct["id"]]
            for deck in (leaf, loose):
                assert sum(item["id"] == deck["id"] for group in [*library["groups"], library["ungrouped"]]
                           for item in group["decks"]) == 1
            page.goto(base_url + "/#" + leaf["id"], wait_until="networkidle")
            expect(page.locator("#viewer-group")).to_have_text("Other project / v0.2")
            expect(page.frame_locator("#frame-a").locator("body")).to_have_text("Screen layout")

            # A reorder dialog may outlive another client's reparent. Its
            # cached source parent must never be sent back as a move request.
            sibling = client.post("/api/v1/groups", json={
                "name": "Reorder sibling", "parent_id": archive["id"]
            })
            assert sibling.status_code == 201
            page.goto(base_url + "/", wait_until="networkidle")
            _project(page, version["id"]).locator(".lib-group-kebab").click()
            _menu_action(page, "reorder").click()
            expect(page.locator("#dialog-group-index")).to_have_value("1")
            page.locator("#dialog-group-index").select_option("0")
            remote_move = client.post(f"/api/v1/groups/{version['id']}/move", json={
                "parent_id": project["id"], "index": 0
            })
            assert remote_move.status_code == 200
            with page.expect_response(lambda response: response.request.method == "POST"
                                      and response.url.endswith(f"/groups/{version['id']}/move")) as reordered:
                page.locator("#dialog-confirm").click()
            assert reordered.value.status == 200
            assert reordered.value.request.post_data_json == {"index": 0}
            assert reordered.value.json()["parent_id"] == project["id"]
            library = client.get("/api/v1/library").json()
            groups = {group["id"]: group for group in library["groups"]}
            assert groups[version["id"]]["parent_id"] == project["id"]
            assert groups[layout["id"]]["parent_id"] == version["id"]
            expect(_project(page, version["id"]).locator(".side-link")).to_have_attribute("title", "FlowGate / v0.2")
            assert errors == []
            context.close()
            browser.close()
