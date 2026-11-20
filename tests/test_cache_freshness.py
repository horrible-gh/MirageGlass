"""Regression coverage for version-aware viewers and HTTP cache headers."""

from __future__ import annotations

from conftest import make_zip


def _files(mark: str):
    return {
        "file": (
            "deck.zip",
            make_zip({
                "index.html": (
                    "<!doctype html><html><head>"
                    "<link rel='stylesheet' href='static/site.css'>"
                    "</head><body>" + mark + "</body></html>"
                ),
                "static/site.css": "body{color:blue}",
            }),
            "application/zip",
        )
    }


def test_shell_and_dynamic_api_are_not_cached(app_client):
    shell = app_client.get("/")
    assert shell.status_code == 200
    assert shell.headers["cache-control"] == "no-store"
    # The existing bookmark format remains hash-based, not query-versioned.
    assert "location.hash.slice(1)" in shell.text
    assert "function versionedThumbUrl(deck)" in shell.text
    assert "if (version != null) return versionUrl(version)" in shell.text

    for path in ("/api/v1/decks", "/api/v1/library", "/api/v1/help"):
        response = app_client.get(path)
        assert response.status_code == 200, path
        assert response.headers["cache-control"] == "no-store", path


def test_active_viewer_reloads_and_versioned_assets_are_immutable(app_client, auth_headers):
    create = app_client.post(
        "/api/v1/decks",
        headers=auth_headers,
        files=_files("CACHE-V1"),
        data={"name": "Cache regression", "idempotency_key": "tc-cache-v1-20261120"},
    )
    assert create.status_code == 201, create.text
    deck_id = create.json()["id"]

    try:
        active = f"/v/{deck_id}/"
        v1 = f"/v/{deck_id}/v1/"
        assert app_client.get(active).headers["cache-control"] == "no-store"
        first = app_client.get(v1)
        assert first.status_code == 200
        assert first.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "CACHE-V1" in first.text
        css = app_client.get(v1 + "static/site.css")
        assert css.status_code == 200
        assert css.headers["cache-control"] == "public, max-age=31536000, immutable"

        for path in (
            f"/api/v1/decks/{deck_id}",
            f"/api/v1/decks/{deck_id}/versions",
            f"/api/v1/decks/{deck_id}/screens",
            f"/api/v1/decks/{deck_id}/screens/main/versions",
            f"/thumbs/{deck_id}.png",
        ):
            response = app_client.get(path)
            assert response.status_code == 200, path
            assert response.headers["cache-control"] == "no-store", path

        missing = app_client.get(f"/v/{deck_id}/v99/")
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"

        update = app_client.post(
            f"/api/v1/decks/{deck_id}/versions",
            headers=auth_headers,
            files=_files("CACHE-V2"),
            data={"idempotency_key": "tc-cache-v2-20261120"},
        )
        assert update.status_code == 201, update.text
        assert update.json()["active_version"] == 2

        # The stable share URL tracks the active version; the old path does not.
        current = app_client.get(active)
        assert current.status_code == 200
        assert current.headers["cache-control"] == "no-store"
        assert "CACHE-V2" in current.text
        second = app_client.get(f"/v/{deck_id}/v2/")
        assert second.status_code == 200
        assert second.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert "CACHE-V2" in second.text
        assert "CACHE-V1" in app_client.get(v1).text

        restore = app_client.post(
            f"/api/v1/decks/{deck_id}/versions/1/activate",
            headers=auth_headers,
        )
        assert restore.status_code == 200
        assert "CACHE-V1" in app_client.get(active).text
        assert "CACHE-V2" in app_client.get(f"/v/{deck_id}/v2/").text
    finally:
        deleted = app_client.delete(f"/api/v1/decks/{deck_id}", headers=auth_headers)
        assert deleted.status_code == 204
