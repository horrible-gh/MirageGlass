"""Atomic selection operations and durable metadata shared by API and console."""

from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from conftest import PROJECT_ROOT, make_zip
from test_library_api import _create_deck, _create_group, _library_deck, _section_ids


def _bulk_move(client, headers, ids, group_id, index=0):
    return client.post(
        "/api/v1/decks/move",
        headers=headers,
        json={"deck_ids": ids, "group_id": group_id, "index": index},
    )


def test_metadata_migration_preserves_legacy_rows_and_order(tmp_path):
    migrations = PROJECT_ROOT / "server/sql/migrations/sqlite"
    with sqlite3.connect(tmp_path / "legacy-metadata.db") as connection:
        connection.executescript((migrations / "001_create_decks.sql").read_text())
        connection.execute(
            "INSERT INTO decks (id,name,status,has_thumb,idempotency_key,created_at,updated_at) "
            "VALUES ('legacy','Legacy','ready',1,'old-upload','2026-01-01','2026-01-01')"
        )
        for filename in ["002_deck_versions.sql", "003_deck_screens.sql", "004_library_groups.sql"]:
            connection.executescript((migrations / filename).read_text())
        connection.execute(
            "INSERT INTO deck_groups VALUES ('legacy-group','Existing group',0,'2026-01-01','2026-01-01')"
        )
        connection.execute("UPDATE decks SET group_id='legacy-group',sort_order=3")
        before = connection.execute("SELECT * FROM decks").fetchone()
        version_before = connection.execute("SELECT * FROM deck_versions").fetchone()
        connection.executescript((migrations / "005_library_metadata.sql").read_text())
        after = connection.execute("SELECT * FROM decks").fetchone()
        version_after = connection.execute("SELECT * FROM deck_versions").fetchone()
        assert after[:-2] == before
        assert after[-2:] == (0, None)
        assert version_after[:-1] == version_before
        assert version_after[-1] == ""


def test_bulk_move_preserves_selection_order_normalizes_sources_and_replays(
    app_client, auth_headers
):
    decks = [_create_deck(app_client, auth_headers, f"bulk-order-{i}") for i in range(6)]
    ids = [deck["id"] for deck in decks]
    left = _create_group(app_client, auth_headers, "Bulk left")["id"]
    right = _create_group(app_client, auth_headers, "Bulk right")["id"]
    for group, selection in [(left, ids[:3]), (right, ids[3:])]:
        assert _bulk_move(app_client, auth_headers, selection, group).status_code == 200
    versions_before = {id: app_client.get(f"/api/v1/decks/{id}/versions").json() for id in ids}

    # Selection includes multiple sources and one deck already in the target.
    selected = [ids[1], ids[4], ids[0]]
    response = _bulk_move(app_client, auth_headers, selected, right, 1)
    assert response.status_code == 200, response.text
    assert _section_ids(app_client, left) == [ids[2]]
    assert _section_ids(app_client, right) == [ids[3], *selected, ids[5]]
    for group in response.json()["groups"]:
        assert [deck["sort_order"] for deck in group["decks"]] == list(range(len(group["decks"])))
    repeated = _bulk_move(app_client, auth_headers, selected, right, 1)
    assert repeated.status_code == 200
    assert repeated.json() == response.json()
    assert {id: app_client.get(f"/api/v1/decks/{id}/versions").json() for id in ids} == versions_before

    # Same-group reordering and null membership use the same single transaction.
    assert _bulk_move(app_client, auth_headers, [ids[5], ids[4]], right, 0).status_code == 200
    assert _section_ids(app_client, right) == [ids[5], ids[4], ids[3], ids[1], ids[0]]
    assert _bulk_move(app_client, auth_headers, selected, None, 9999).status_code == 200
    assert _section_ids(app_client, None)[-3:] == selected
    assert all(_library_deck(app_client, id)["group_id"] is None for id in selected)


def test_bulk_move_missing_member_or_group_changes_nothing(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "bulk-invalid-target")
    group = _create_group(app_client, auth_headers, "Bulk validation")
    before = app_client.get("/api/v1/library").json()
    missing_deck = _bulk_move(app_client, auth_headers, [deck["id"], "does-not-exist"], group["id"])
    assert missing_deck.status_code == 404
    assert app_client.get("/api/v1/library").json() == before
    missing_group = _bulk_move(app_client, auth_headers, [deck["id"]], "missing-group")
    assert missing_group.status_code == 404
    assert app_client.get("/api/v1/library").json() == before


@pytest.mark.parametrize("payload", [
    {"deck_ids": [], "index": 0},
    {"deck_ids": ["duplicate", "duplicate"], "index": 0},
    {"deck_ids": [""], "index": 0},
    {"deck_ids": [12], "index": 0},
    {"deck_ids": ["a"], "index": -1},
    {"deck_ids": ["a"], "index": True},
    {"deck_ids": ["a"], "index": "1"},
])
def test_bulk_move_rejects_invalid_selection(app_client, auth_headers, payload):
    response = app_client.post("/api/v1/decks/move", headers=auth_headers, json=payload)
    assert response.status_code == 422


@pytest.mark.parametrize("checkpoint", ["bulk_move.after_sources", "bulk_move.after_first_membership"])
def test_bulk_move_rolls_back_mid_transaction_failure(app_client, auth_headers, monkeypatch, checkpoint):
    from server.config import database
    from server.repository import DeckRepository

    ids = [_create_deck(app_client, auth_headers, f"{checkpoint}-{i}")["id"] for i in range(3)]
    source = _create_group(app_client, auth_headers, f"Atomic source {checkpoint}")["id"]
    target = _create_group(app_client, auth_headers, f"Atomic target {checkpoint}")["id"]
    assert _bulk_move(app_client, auth_headers, ids, source).status_code == 200
    before = app_client.get("/api/v1/library").json()

    def fail(self, name):
        if name == checkpoint:
            raise RuntimeError("Injected bulk failure")

    monkeypatch.setattr(DeckRepository, "_checkpoint", fail)
    repo = DeckRepository(database.sqloader, database.db_instance)
    with pytest.raises(RuntimeError, match="Injected bulk failure"):
        repo.move_decks(ids[:2], target, 0)
    assert app_client.get("/api/v1/library").json() == before


def test_shared_favorites_are_explicit_atomic_and_available_in_all_reads(app_client, auth_headers):
    first = _create_deck(app_client, auth_headers, "favorites-first")
    second = _create_deck(app_client, auth_headers, "favorites-second")
    assert first["favorite"] is False and first["last_viewed_at"] is None
    for _ in range(2):
        response = app_client.put(
            f"/api/v1/decks/{first['id']}/favorite", headers=auth_headers, json={"favorite": True}
        )
        assert response.status_code == 200 and response.json()["favorite"] is True
    assert _library_deck(app_client, first["id"])["favorite"] is True
    deck_read = app_client.get(f"/api/v1/decks/{first['id']}").json()
    listing = app_client.get("/api/v1/decks").json()["items"]
    assert deck_read["favorite"] is True
    assert next(deck for deck in listing if deck["id"] == first["id"])["favorite"] is True
    # Favorites must not cause content to look newly uploaded.
    assert deck_read["updated_at"] == first["updated_at"]

    response = app_client.put(
        "/api/v1/decks/favorites", headers=auth_headers,
        json={"deck_ids": [second["id"], "missing"], "favorite": True},
    )
    assert response.status_code == 404
    assert _library_deck(app_client, second["id"])["favorite"] is False
    assert app_client.put(
        "/api/v1/decks/favorites", headers=auth_headers,
        json={"deck_ids": [first["id"], second["id"]], "favorite": True},
    ).status_code == 200
    assert app_client.put(
        f"/api/v1/decks/{first['id']}/favorite", headers=auth_headers, json={"favorite": False}
    ).json()["favorite"] is False
    assert _library_deck(app_client, second["id"])["favorite"] is True
    assert app_client.put(
        f"/api/v1/decks/{first['id']}/favorite", headers=auth_headers, json={"favorite": "false"}
    ).status_code == 422


def test_favorites_transaction_rolls_back(app_client, auth_headers, monkeypatch):
    from server.config import database
    from server.repository import DeckRepository

    ids = [_create_deck(app_client, auth_headers, f"favorite-rollback-{i}")["id"] for i in range(2)]

    def fail(self, name):
        if name == "favorites.after_first":
            raise RuntimeError("Injected favorite failure")

    monkeypatch.setattr(DeckRepository, "_checkpoint", fail)
    with pytest.raises(RuntimeError, match="Injected favorite failure"):
        DeckRepository(database.sqloader, database.db_instance).set_favorites(ids, True)
    assert all(not _library_deck(app_client, id)["favorite"] for id in ids)


def test_recency_is_explicit_shared_and_does_not_modify_versions(app_client, auth_headers):
    deck = _create_deck(app_client, auth_headers, "recency-explicit")
    id = deck["id"]
    assert app_client.get(f"/v/{id}/").status_code == 200
    assert app_client.get(f"/api/v1/decks/{id}").json()["last_viewed_at"] is None
    versions = app_client.get(f"/api/v1/decks/{id}/versions").json()
    visited = app_client.post(f"/api/v1/decks/{id}/visit", headers=auth_headers)
    assert visited.status_code == 200
    timestamp = visited.json()["last_viewed_at"]
    assert timestamp and timestamp.endswith("+00:00")
    assert _library_deck(app_client, id)["last_viewed_at"] == timestamp
    assert app_client.get(f"/api/v1/decks/{id}").json()["last_viewed_at"] == timestamp
    assert app_client.get(f"/api/v1/decks/{id}/versions").json() == versions
    assert visited.json()["updated_at"] == deck["updated_at"]


def test_metadata_survives_database_reopen(tmp_path, app_client):
    from server.config import Database, settings
    from server.repository import DeckRepository

    directory = tmp_path / "durable-metadata"
    isolated = replace(settings, storage_dir=directory, db_path=directory / "mirageglass.db")
    first = Database(isolated)
    db, sql, _ = first.init()
    repo = DeckRepository(sql, db)
    repo.create_deck("persistent", "Persistent")
    repo.create_version("persistent", 1, "persistent-key", "Initial note")
    repo.set_version_status("persistent", 1, "ready")
    repo.set_active("persistent", 1)
    repo.set_favorites(["persistent"], True)
    timestamp = repo.record_visit("persistent")["last_viewed_at"]
    repo.set_version_note("persistent", 1, "Saved note")
    second = Database(isolated)
    db2, sql2, _ = second.init()
    fresh = DeckRepository(sql2, db2)
    assert fresh.get("persistent")["favorite"] == 1
    assert fresh.get("persistent")["last_viewed_at"] == timestamp
    assert fresh.get_version("persistent", 1)["note"] == "Saved note"


def test_version_notes_upload_edit_clear_and_idempotent_replay(app_client, auth_headers):
    def upload(path, key, html, note, name=None):
        data = {"idempotency_key": key, "note": note}
        if name:
            data["name"] = name
        return app_client.post(
            path, headers=auth_headers,
            files={"file": ("deck.zip", make_zip({"index.html": html}), "application/zip")},
            data=data,
        )

    first = upload("/api/v1/decks", "notes-v1", "<h1>First</h1>", "First note", "Notes")
    assert first.status_code == 201
    id = first.json()["id"]
    second = upload(f"/api/v1/decks/{id}/versions", "notes-v2", "<h1>Second</h1>", "Second note")
    assert second.status_code == 201
    replay = upload(f"/api/v1/decks/{id}/versions", "notes-v2", "<h1>Ignored</h1>", "Ignored note")
    assert replay.status_code == 200
    versions = app_client.get(f"/api/v1/decks/{id}/versions").json()
    assert [item["note"] for item in versions["items"]] == ["First note", "Second note"]
    before_files = app_client.get(f"/v/{id}/v1/").content
    patch = app_client.patch(
        f"/api/v1/decks/{id}/versions/1", headers=auth_headers,
        json={"note": "Updated summary\nSecond line"},
    )
    assert patch.status_code == 200
    assert patch.json()["note"] == "Updated summary\nSecond line"
    assert patch.json()["is_active"] is False
    assert app_client.get(f"/v/{id}/v1/").content == before_files
    assert app_client.get(f"/api/v1/decks/{id}").json()["version"] == 2
    history = app_client.get(f"/api/v1/decks/{id}/screens/main/versions").json()
    assert history["items"][0]["note"] == patch.json()["note"]
    assert history["items"][1]["note"] == "Second note"
    assert app_client.patch(
        f"/api/v1/decks/{id}/versions/1", headers=auth_headers, json={"note": ""}
    ).json()["note"] == ""
    for invalid in ["x" * 4001, "a\u0000b", 42, None]:
        assert app_client.patch(
            f"/api/v1/decks/{id}/versions/1", headers=auth_headers, json={"note": invalid}
        ).status_code == 422
    assert app_client.patch(
        f"/api/v1/decks/{id}/versions/99", headers=auth_headers, json={"note": "Missing"}
    ).status_code == 404
    before_count = versions["latest_version"]
    assert upload(f"/api/v1/decks/{id}/versions", "notes-invalid", "<p>Bad note</p>", "x" * 4001).status_code == 422
    assert app_client.get(f"/api/v1/decks/{id}/versions").json()["latest_version"] == before_count


def test_all_new_writes_use_existing_token_and_console_auth(app_client, auth_headers, monkeypatch):
    from server.config import settings

    deck = _create_deck(app_client, auth_headers, "management-auth")
    id = deck["id"]
    operations = [
        ("post", "/api/v1/decks/move", {"deck_ids": [id], "group_id": None, "index": 0}),
        ("put", "/api/v1/decks/favorites", {"deck_ids": [id], "favorite": True}),
        ("put", f"/api/v1/decks/{id}/favorite", {"favorite": False}),
        ("post", f"/api/v1/decks/{id}/visit", None),
        ("patch", f"/api/v1/decks/{id}/versions/1", {"note": "Authorized"}),
    ]
    app_client.get("/")
    for method, path, body in operations:
        kwargs = {"json": body} if body is not None else {}
        assert app_client.request(method, path, **kwargs).status_code == 401
        assert app_client.request(method, path, headers={"Origin": "https://other.example"}, **kwargs).status_code == 401
        assert app_client.request(method, path, headers={"Origin": "http://testserver"}, **kwargs).status_code == 200
    monkeypatch.setattr(settings, "console_write_enabled", False)
    for method, path, body in operations:
        kwargs = {"json": body} if body is not None else {}
        assert app_client.request(method, path, headers={"Origin": "http://testserver"}, **kwargs).status_code == 401
        assert app_client.request(method, path, headers=auth_headers, **kwargs).status_code == 200
    app_client.cookies.clear()
