"""Deck version management: add a version, list, download by version, rollback.

These exercise the group 0006 protocol scenarios: the shared /v/{id}/ link follows
the active version, a rejected upload leaves the active version intact, reads can
target a specific version, and rollback moves the active pointer without dropping
history.
"""

from __future__ import annotations

import io
import zipfile

from conftest import make_zip


def _files(zip_bytes: bytes):
    return {"file": ("deck.zip", zip_bytes, "application/zip")}


def _deck(index_html: str, **extra) -> bytes:
    entries = {"index.html": index_html}
    entries.update(extra)
    return make_zip(entries)


def _create_deck(client, headers, marker, key):
    res = client.post(
        "/api/v1/decks",
        headers=headers,
        files=_files(_deck(f"<!doctype html><html><body>{marker}</body></html>")),
        data={"name": "versioned deck", "idempotency_key": key},
    )
    assert res.status_code == 201, res.text
    return res.json()


def _add_version(client, headers, deck_id, marker, key, **extra):
    return client.post(
        f"/api/v1/decks/{deck_id}/versions",
        headers=headers,
        files=_files(_deck(f"<!doctype html><html><body>{marker}</body></html>", **extra)),
        data={"idempotency_key": key},
    )


def test_create_deck_exposes_version_fields(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "v1", "ver-fields")
    assert deck["version"] == 1
    assert deck["latest_version"] == 1


def test_add_version_switches_the_active_link(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "first version", "ver-switch")
    deck_id = deck["id"]
    assert "first version" in app_client.get(f"/v/{deck_id}/").text

    res = _add_version(app_client, auth_headers, deck_id, "second version", "ver-switch-2")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["id"] == deck_id
    assert body["version"] == 2
    assert body["latest_version"] == 2
    # The stable link now serves version 2.
    assert "second version" in app_client.get(f"/v/{deck_id}/").text


def test_add_version_is_idempotent(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "base", "ver-idem")
    deck_id = deck["id"]
    key = "ver-idem-2"
    first = _add_version(app_client, auth_headers, deck_id, "added", key)
    assert first.status_code == 201, first.text
    again = _add_version(app_client, auth_headers, deck_id, "added", key)
    assert again.status_code == 200, again.text
    assert again.json()["latest_version"] == 2


def test_rejected_version_leaves_active_untouched(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "good version", "ver-reject")
    deck_id = deck["id"]
    bad = make_zip({"sub/index.html": "<html></html>"})  # no root index.html
    res = app_client.post(
        f"/api/v1/decks/{deck_id}/versions",
        headers=auth_headers,
        files=_files(bad),
        data={"idempotency_key": "ver-reject-2"},
    )
    assert res.status_code == 400, res.text
    # Active version is still 1 and the link still serves it.
    assert app_client.get(f"/api/v1/decks/{deck_id}").json()["version"] == 1
    assert "good version" in app_client.get(f"/v/{deck_id}/").text


def test_add_version_requires_deck_and_token(app_client, auth_headers, deck_zip):
    # Unknown deck, valid token -> 404.
    res = app_client.post(
        "/api/v1/decks/zzzzzzzz/versions",
        headers=auth_headers,
        files=_files(deck_zip),
        data={"idempotency_key": "ver-404"},
    )
    assert res.status_code == 404, res.text
    # No token -> 401 (auth is checked before the deck lookup).
    res = app_client.post(
        "/api/v1/decks/zzzzzzzz/versions",
        files=_files(deck_zip),
        data={"idempotency_key": "ver-401"},
    )
    assert res.status_code == 401, res.text


def test_version_list_reports_active_and_history(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "one", "ver-list")
    deck_id = deck["id"]
    _add_version(app_client, auth_headers, deck_id, "two", "ver-list-2")

    listing = app_client.get(f"/api/v1/decks/{deck_id}/versions")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["deck_id"] == deck_id
    assert body["active_version"] == 2
    assert body["latest_version"] == 2
    numbers = [item["version"] for item in body["items"]]
    assert numbers == [1, 2]
    active_flags = {item["version"]: item["is_active"] for item in body["items"]}
    assert active_flags == {1: False, 2: True}
    # Exactly one active version.
    assert sum(1 for item in body["items"] if item["is_active"]) == 1

    assert app_client.get("/api/v1/decks/zzzzzzzz/versions").status_code == 404


def test_read_specific_version(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "orig", "ver-read")
    deck_id = deck["id"]
    _add_version(
        app_client, auth_headers, deck_id, "latest", "ver-read-2", **{"assets/x.txt": "two"}
    )

    # Default download targets the active version (2) -> {id}.zip
    active = app_client.get(f"/api/v1/decks/{deck_id}/download")
    assert active.status_code == 200
    assert f'filename="{deck_id}.zip"' in active.headers["content-disposition"]

    # ?version=1 targets version 1 -> {id}_v1.zip
    v1 = app_client.get(f"/api/v1/decks/{deck_id}/download?version=1")
    assert v1.status_code == 200
    assert f'filename="{deck_id}_v1.zip"' in v1.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(v1.content)) as zf:
        assert "index.html" in zf.namelist()
        assert b"orig" in zf.read("index.html")

    files_v1 = app_client.get(f"/api/v1/decks/{deck_id}/files?version=1")
    assert files_v1.status_code == 200
    assert "index.html" in [i["path"] for i in files_v1.json()["items"]]

    # Missing version -> 404.
    assert app_client.get(f"/api/v1/decks/{deck_id}/files?version=99").status_code == 404


def test_rollback_activates_an_earlier_version(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "page one", "ver-roll")
    deck_id = deck["id"]
    _add_version(app_client, auth_headers, deck_id, "page two", "ver-roll-2")
    assert "page two" in app_client.get(f"/v/{deck_id}/").text

    res = app_client.post(
        f"/api/v1/decks/{deck_id}/versions/1/activate", headers=auth_headers
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["version"] == 1
    assert body["latest_version"] == 2  # history is kept
    assert "page one" in app_client.get(f"/v/{deck_id}/").text


def test_rollback_failure_cases(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "solo", "ver-rollfail")
    deck_id = deck["id"]
    # Unknown version -> 404.
    assert (
        app_client.post(
            f"/api/v1/decks/{deck_id}/versions/9/activate", headers=auth_headers
        ).status_code
        == 404
    )
    # No token -> 401.
    assert (
        app_client.post(f"/api/v1/decks/{deck_id}/versions/1/activate").status_code == 401
    )
