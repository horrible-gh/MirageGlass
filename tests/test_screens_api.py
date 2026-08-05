"""Multi-screen decks: an optional screens.json manifest at the zip root groups
several entry points under one deck, each with its own derived version history.

These exercise the group 0012 R0001 scope: a plain zip keeps behaving exactly as
before (one implicit "main" screen), and a manifest zip's screens are relabeled
1..N independently, based only on where their own content actually changed.
"""

from __future__ import annotations

import json

from conftest import make_zip


def _files(zip_bytes: bytes):
    return {"file": ("deck.zip", zip_bytes, "application/zip")}


def _manifest_zip(screens: list[dict], contents: dict[str, str]) -> bytes:
    entries = {"screens.json": json.dumps({"screens": screens})}
    entries.update(contents)
    return make_zip(entries)


def _create(client, headers, zip_bytes: bytes, key: str, name: str = "multi-screen deck"):
    res = client.post(
        "/api/v1/decks",
        headers=headers,
        files=_files(zip_bytes),
        data={"name": name, "idempotency_key": key},
    )
    assert res.status_code == 201, res.text
    return res.json()


def test_plain_zip_reports_one_implicit_screen(app_client, auth_headers):
    deck = _create(
        app_client,
        auth_headers,
        make_zip({"index.html": "<html><body>solo</body></html>"}),
        "screens-implicit",
        name="Solo deck",
    )
    deck_id = deck["id"]

    screens = app_client.get(f"/api/v1/decks/{deck_id}/screens")
    assert screens.status_code == 200, screens.text
    body = screens.json()
    assert body["items"] == [{"key": "main", "tag": "Solo deck", "viewer_path": ""}]

    history = app_client.get(f"/api/v1/decks/{deck_id}/screens/main/versions")
    assert history.status_code == 200, history.text
    items = history.json()["items"]
    assert len(items) == 1
    assert items[0]["version"] == 1
    assert items[0]["is_active"] is True


def test_manifest_zip_registers_multiple_screens(app_client, auth_headers):
    screens = [
        {"key": "overview", "tag": "Overview", "entry": "overview/index.html"},
        {"key": "settings", "tag": "Settings", "entry": "settings/index.html"},
    ]
    contents = {
        "overview/index.html": "<html><body>overview v1</body></html>",
        "settings/index.html": "<html><body>settings v1</body></html>",
    }
    deck = _create(app_client, auth_headers, _manifest_zip(screens, contents), "screens-multi")
    deck_id = deck["id"]

    listed = app_client.get(f"/api/v1/decks/{deck_id}/screens")
    assert listed.status_code == 200, listed.text
    items = {item["key"]: item for item in listed.json()["items"]}
    assert items["overview"] == {"key": "overview", "tag": "Overview", "viewer_path": "overview/"}
    assert items["settings"] == {"key": "settings", "tag": "Settings", "viewer_path": "settings/"}

    assert "overview v1" in app_client.get(f"/v/{deck_id}/overview/").text
    assert "settings v1" in app_client.get(f"/v/{deck_id}/settings/").text


def test_screen_history_only_counts_its_own_changes(app_client, auth_headers):
    screens = [
        {"key": "a", "tag": "Screen A", "entry": "a/index.html"},
        {"key": "b", "tag": "Screen B", "entry": "b/index.html"},
    ]
    v1 = _create(
        app_client,
        auth_headers,
        _manifest_zip(screens, {"a/index.html": "a-1", "b/index.html": "b-1"}),
        "screens-hist-1",
    )
    deck_id = v1["id"]

    # v2 only changes screen A.
    res = app_client.post(
        f"/api/v1/decks/{deck_id}/versions",
        headers=auth_headers,
        files=_files(_manifest_zip(screens, {"a/index.html": "a-2", "b/index.html": "b-1"})),
        data={"idempotency_key": "screens-hist-2"},
    )
    assert res.status_code == 201, res.text

    # v3 only changes screen B.
    res = app_client.post(
        f"/api/v1/decks/{deck_id}/versions",
        headers=auth_headers,
        files=_files(_manifest_zip(screens, {"a/index.html": "a-2", "b/index.html": "b-3"})),
        data={"idempotency_key": "screens-hist-3"},
    )
    assert res.status_code == 201, res.text
    assert res.json()["version"] == 3

    a_history = app_client.get(f"/api/v1/decks/{deck_id}/screens/a/versions").json()
    b_history = app_client.get(f"/api/v1/decks/{deck_id}/screens/b/versions").json()

    # Screen A changed at deck versions 1 and 2 only - two entries, still active
    # at v3 because its content has not changed since v2.
    assert [item["deck_version"] for item in a_history["items"]] == [1, 2]
    assert a_history["items"][-1]["is_active"] is True
    assert a_history["latest_version"] == 2

    # Screen B changed at deck versions 1 and 3 - two entries, matching the deck's
    # own version count even though it skipped v2 entirely.
    assert [item["deck_version"] for item in b_history["items"]] == [1, 3]
    assert b_history["items"][-1]["is_active"] is True
    assert b_history["latest_version"] == 2


def test_unknown_screen_key_is_404(app_client, auth_headers):
    deck = _create(
        app_client, auth_headers, make_zip({"index.html": "<html></html>"}), "screens-404"
    )
    res = app_client.get(f"/api/v1/decks/{deck['id']}/screens/missing/versions")
    assert res.status_code == 404, res.text
    assert app_client.get("/api/v1/decks/zzzzzzzz/screens").status_code == 404


def test_manifest_validation_rejects_bad_entries(app_client, auth_headers):
    missing_entry = _manifest_zip(
        [{"key": "a", "tag": "A", "entry": "a/index.html"}], {}
    )
    res = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(missing_entry),
        data={"name": "bad", "idempotency_key": "screens-bad-entry"},
    )
    assert res.status_code == 400, res.text

    duplicate_key = _manifest_zip(
        [
            {"key": "a", "tag": "A", "entry": "a/index.html"},
            {"key": "a", "tag": "A again", "entry": "b/index.html"},
        ],
        {"a/index.html": "x", "b/index.html": "y"},
    )
    res = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(duplicate_key),
        data={"name": "bad", "idempotency_key": "screens-bad-dup"},
    )
    assert res.status_code == 400, res.text
