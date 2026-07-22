"""One full create/read/delete loop plus the auth regression.

The 401 behaviour is the key signal in this file. Back when the token was empty,
require_token fell through to a 500 — that was how the "`.env` never loaded" bug
surfaced. It has to be a 401 now.
"""

from __future__ import annotations

from conftest import make_zip


def _files(zip_bytes: bytes):
    return {"file": ("deck.zip", zip_bytes, "application/zip")}


def test_create_without_token_is_401_not_500(app_client, deck_zip):
    res = app_client.post(
        "/api/v1/decks",
        files=_files(deck_zip),
        data={"name": "no-token", "idempotency_key": "tc-no-token"},
    )
    assert res.status_code == 401, res.text


def test_create_with_wrong_token_is_401(app_client, deck_zip):
    res = app_client.post(
        "/api/v1/decks",
        headers={"Authorization": "Bearer nope"},
        files=_files(deck_zip),
        data={"name": "bad-token", "idempotency_key": "tc-bad-token"},
    )
    assert res.status_code == 401, res.text


def test_delete_without_token_is_401(app_client):
    res = app_client.delete("/api/v1/decks/whatever")
    assert res.status_code == 401, res.text


def test_full_lifecycle(app_client, auth_headers, deck_zip):
    """create -> list -> viewer -> nested asset -> thumbnail -> idempotent replay -> delete."""
    key = "tc-lifecycle"
    # A deliberately non-ASCII name: this asserts the UTF-8 multipart round trip.
    name = "Lifecycle deck — café デッキ"
    res = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(deck_zip),
        data={"name": name, "idempotency_key": key},
    )
    assert res.status_code == 201, res.text
    deck = res.json()
    deck_id = deck["id"]
    assert deck["status"] == "ready"
    assert deck["name"] == name                        # UTF-8 multipart round trip
    assert deck["viewer_url"] == f"/v/{deck_id}/"

    # It shows up in the list as ready
    items = app_client.get("/api/v1/decks").json()["items"]
    assert deck_id in [i["id"] for i in items]

    # Single fetch
    assert app_client.get(f"/api/v1/decks/{deck_id}").status_code == 200

    # 307 without the trailing slash, 200 with it
    res = app_client.get(f"/v/{deck_id}", follow_redirects=False)
    assert res.status_code == 307
    assert res.headers["location"] == f"/v/{deck_id}/"
    assert app_client.get(f"/v/{deck_id}/").status_code == 200

    # Relative asset inside the zip
    assert app_client.get(f"/v/{deck_id}/assets/style.css").status_code == 200

    # Thumbnail (conftest swaps capture for a fake)
    assert app_client.get(f"/thumbs/{deck_id}.png").status_code == 200

    # Same idempotency key -> nothing new is created, 200 with the same id
    again = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(deck_zip),
        data={"name": "should be ignored", "idempotency_key": key},
    )
    assert again.status_code == 200, again.text
    assert again.json()["id"] == deck_id

    # Gone after the delete
    assert app_client.delete(f"/api/v1/decks/{deck_id}", headers=auth_headers).status_code == 204
    assert app_client.get(f"/api/v1/decks/{deck_id}").status_code == 404
    assert app_client.get(f"/v/{deck_id}/").status_code == 404


def test_zip_without_root_index_is_rejected_400(app_client, auth_headers):
    bad = make_zip({"sub/index.html": "<html></html>"})
    res = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(bad),
        data={"name": "no-index", "idempotency_key": "tc-no-index"},
    )
    assert res.status_code == 400, res.text
    assert "index.html" in res.json()["detail"]


def test_unknown_deck_is_404(app_client):
    assert app_client.get("/api/v1/decks/zzzzzzzz").status_code == 404
    assert app_client.get("/thumbs/zzzzzzzz.png").status_code == 404


def test_healthz(app_client):
    assert app_client.get("/healthz").json() == {"ok": True}
