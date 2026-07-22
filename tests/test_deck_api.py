"""One full create/read/delete loop plus the auth regression.

The 401 behaviour is the key signal in this file. Back when the token was empty,
require_token fell through to a 500 — that was how the "`.env` never loaded" bug
surfaced. It has to be a 401 now.
"""

from __future__ import annotations

import io
import shutil
import zipfile

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
    assert app_client.get("/api/v1/decks/zzzzzzzz/download").status_code == 404
    assert app_client.get("/api/v1/decks/zzzzzzzz/files").status_code == 404
    assert app_client.get("/thumbs/zzzzzzzz.png").status_code == 404


def test_download_round_trip_and_manifest(app_client, auth_headers):
    source_files = {
        "index.html": "<!doctype html><html><body>round trip</body></html>",
        "assets/images/pixel.txt": "nested asset",
        "assets/style.css": "body{color:#123}",
    }
    created = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(make_zip(source_files)),
        data={"name": "download source", "idempotency_key": "tc-download-source"},
    )
    assert created.status_code == 201, created.text
    source_id = created.json()["id"]

    manifest = app_client.get(f"/api/v1/decks/{source_id}/files")
    assert manifest.status_code == 200, manifest.text
    items = manifest.json()["items"]
    paths = [item["path"] for item in items]
    assert paths == sorted(source_files)
    assert all("\\" not in path for path in paths)
    assert {item["path"]: item["size"] for item in items} == {
        path: len(content.encode("utf-8")) for path, content in source_files.items()
    }

    downloaded = app_client.get(f"/api/v1/decks/{source_id}/download")
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.headers["content-type"] == "application/zip"
    assert f'filename="{source_id}.zip"' in downloaded.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(downloaded.content)) as zf:
        assert zf.namelist() == sorted(source_files)
        assert zf.read("assets/images/pixel.txt") == b"nested asset"

    recreated = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(downloaded.content),
        data={"name": "download replacement", "idempotency_key": "tc-download-replacement"},
    )
    assert recreated.status_code == 201, recreated.text
    recreated_id = recreated.json()["id"]
    assert recreated_id != source_id
    viewer = app_client.get(f"/v/{recreated_id}/index.html")
    assert viewer.status_code == 200
    assert viewer.text == source_files["index.html"]


def test_download_and_manifest_reject_non_ready_deck(app_client, auth_headers, deck_zip):
    created = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(deck_zip),
        data={"name": "not ready", "idempotency_key": "tc-not-ready"},
    )
    assert created.status_code == 201, created.text
    deck_id = created.json()["id"]

    from server.config import database
    from server.repository import DeckRepository

    DeckRepository(database.sqloader).set_status(deck_id, "processing")
    assert app_client.get(f"/api/v1/decks/{deck_id}/download").status_code == 409
    assert app_client.get(f"/api/v1/decks/{deck_id}/files").status_code == 409


def test_download_and_manifest_return_404_when_source_is_missing(
    app_client, auth_headers, deck_zip
):
    created = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files(deck_zip),
        data={"name": "missing source", "idempotency_key": "tc-missing-source"},
    )
    assert created.status_code == 201, created.text
    deck_id = created.json()["id"]

    from server import storage
    from server.config import settings

    shutil.rmtree(storage.deck_src_dir(settings, deck_id))
    assert app_client.get(f"/api/v1/decks/{deck_id}/download").status_code == 404
    assert app_client.get(f"/api/v1/decks/{deck_id}/files").status_code == 404


def test_healthz(app_client):
    assert app_client.get("/healthz").json() == {"ok": True}
