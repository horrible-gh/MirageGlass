"""Library groups, ordering, transaction, concurrency, and management auth."""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from conftest import PROJECT_ROOT, make_zip


def _files(payload: bytes):
    return {"file": ("deck.zip", payload, "application/zip")}


def _create_deck(client, headers, key: str, name: str | None = None) -> dict:
    payload = make_zip({"index.html": f"<html><body>{key}</body></html>"})
    response = client.post(
        "/api/v1/decks",
        headers=headers,
        files=_files(payload),
        data={"name": name or key, "idempotency_key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_group(client, headers, name: str) -> dict:
    response = client.post("/api/v1/groups", headers=headers, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def _library_deck(client, deck_id: str) -> dict:
    body = client.get("/api/v1/library").json()
    for section in [*body["groups"], body["ungrouped"]]:
        for deck in section["decks"]:
            if deck["id"] == deck_id:
                return deck
    raise AssertionError(f"{deck_id} missing from library")


def _section_ids(client, group_id: str | None) -> list[str]:
    body = client.get("/api/v1/library").json()
    if group_id is None:
        return [deck["id"] for deck in body["ungrouped"]["decks"]]
    group = next(group for group in body["groups"] if group["id"] == group_id)
    return [deck["id"] for deck in group["decks"]]


def _relative(ids: list[str], selected: set[str]) -> list[str]:
    return [item for item in ids if item in selected]


def test_004_migration_backfills_legacy_order_and_ungrouped(tmp_path):
    connection = sqlite3.connect(tmp_path / "legacy.db")
    migrations = PROJECT_ROOT / "server" / "sql" / "migrations" / "sqlite"
    connection.executescript((migrations / "001_create_decks.sql").read_text("utf-8"))
    rows = [
        ("old-a", "old", "ready", 0, "old-key", "2026-01-01T00:00:00+00:00"),
        ("same-a", "same a", "ready", 0, "same-a-key", "2026-02-01T00:00:00+00:00"),
        ("same-b", "same b", "ready", 0, "same-b-key", "2026-02-01T00:00:00+00:00"),
    ]
    for deck_id, name, status, thumb, key, timestamp in rows:
        connection.execute(
            "INSERT INTO decks "
            "(id, name, status, has_thumb, idempotency_key, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (deck_id, name, status, thumb, key, timestamp, timestamp),
        )
    connection.commit()
    for migration in ["002_deck_versions.sql", "003_deck_screens.sql", "004_library_groups.sql"]:
        connection.executescript((migrations / migration).read_text("utf-8"))

    actual = connection.execute(
        "SELECT id, group_id, sort_order FROM decks ORDER BY sort_order"
    ).fetchall()
    assert actual == [
        ("same-b", None, 0),
        ("same-a", None, 1),
        ("old-a", None, 2),
    ]
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(decks)").fetchall()
    }
    assert {"group_id", "sort_order"} <= columns
    connection.close()


def test_migration_ledger_reinit_preserves_user_order(tmp_path, app_client):
    from server.config import Database, settings
    from server.repository import DeckRepository

    storage = tmp_path / "reinit-storage"
    isolated = replace(settings, storage_dir=storage, db_path=storage / "mirageglass.db")
    first = Database(isolated)
    db_instance, sqloader, _ = first.init()
    repo = DeckRepository(sqloader, db_instance)
    repo.create_deck("ledger-a", "A")
    repo.create_deck("ledger-b", "B")
    repo.move_deck("ledger-a", None, 0)
    before = [
        row["id"]
        for row in sqloader.fetch_all(
            "queries", "organization.list_deck_siblings", (None,)
        )
    ]
    assert before == ["ledger-a", "ledger-b"]

    second = Database(isolated)
    _, second_loader, _ = second.init()
    after = [
        row["id"]
        for row in second_loader.fetch_all(
            "queries", "organization.list_deck_siblings", (None,)
        )
    ]
    assert after == before


def test_library_crud_move_delete_and_legacy_compatibility(
    app_client, auth_headers
):
    first = _create_deck(app_client, auth_headers, "lib-crud-first")
    second = _create_deck(app_client, auth_headers, "lib-crud-second")
    third = _create_deck(app_client, auth_headers, "lib-crud-third")
    selected = {first["id"], second["id"], third["id"]}

    legacy_before = app_client.get("/api/v1/decks").json()["items"]
    legacy_ids_before = [deck["id"] for deck in legacy_before]
    # Existing clients retain every legacy field; organization metadata is
    # additive so token-based agents can perform the console's same operations.
    assert {
        "id",
        "name",
        "status",
        "viewer_url",
        "thumb_url",
        "created_at",
        "version",
        "latest_version",
    } <= set(legacy_before[0])
    assert {"favorite", "last_viewed_at", "group_id", "sort_order", "updated_at"} <= set(legacy_before[0])
    assert isinstance(legacy_before[0]["favorite"], bool)

    alpha = _create_group(app_client, auth_headers, "  Duplicate  ")
    duplicate = app_client.post(
        "/api/v1/groups", headers=auth_headers, json={"name": "Duplicate"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["code"] == "duplicate_group_name"
    beta = _create_group(app_client, auth_headers, "Beta")
    assert alpha["name"] == "Duplicate"
    assert alpha["parent_id"] is beta["parent_id"] is None

    renamed = app_client.patch(
        f"/api/v1/groups/{alpha['id']}",
        headers=auth_headers,
        json={"name": "  Alpha  "},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Alpha"
    for bad_name in ["   ", "bad\nname", "x" * 121]:
        rejected = app_client.patch(
            f"/api/v1/groups/{alpha['id']}",
            headers=auth_headers,
            json={"name": bad_name},
        )
        assert rejected.status_code == 422
        assert rejected.json()["detail"]["code"] == "invalid_name"

    # Target indexes are clamped and every sibling range is normalized to 0..N-1.
    for deck_id in [first["id"], second["id"]]:
        moved = app_client.post(
            f"/api/v1/decks/{deck_id}/move",
            headers=auth_headers,
            json={"group_id": alpha["id"], "index": 999},
        )
        assert moved.status_code == 200, moved.text
    assert _relative(_section_ids(app_client, alpha["id"]), selected) == [
        first["id"],
        second["id"],
    ]
    moved = app_client.post(
        f"/api/v1/decks/{second['id']}/move",
        headers=auth_headers,
        json={"group_id": alpha["id"], "index": -999},
    )
    assert moved.status_code == 200
    assert _relative(_section_ids(app_client, alpha["id"]), selected) == [
        second["id"],
        first["id"],
    ]
    # A same-position request is a successful no-op.
    before_noop = _section_ids(app_client, alpha["id"])
    assert app_client.post(
        f"/api/v1/decks/{second['id']}/move",
        headers=auth_headers,
        json={"group_id": alpha["id"], "index": 0},
    ).status_code == 200
    assert _section_ids(app_client, alpha["id"]) == before_noop

    assert app_client.post(
        f"/api/v1/decks/{third['id']}/move",
        headers=auth_headers,
        json={"group_id": beta["id"], "index": 0},
    ).status_code == 200
    assert app_client.post(
        f"/api/v1/decks/{first['id']}/move",
        headers=auth_headers,
        json={"group_id": beta["id"], "index": 999},
    ).status_code == 200
    assert _relative(_section_ids(app_client, beta["id"]), selected) == [
        third["id"],
        first["id"],
    ]

    # Rename changes only the name; identity, version, viewer, membership and order stay put.
    before_rename = _library_deck(app_client, first["id"])
    versions_before = app_client.get(
        f"/api/v1/decks/{first['id']}/versions"
    ).json()
    renamed_deck = app_client.patch(
        f"/api/v1/decks/{first['id']}",
        headers=auth_headers,
        json={"name": "  Shared deck name  "},
    )
    assert renamed_deck.status_code == 200
    after_rename = _library_deck(app_client, first["id"])
    assert after_rename["name"] == "Shared deck name"
    for field in ["id", "viewer_url", "active_version", "latest_version", "group_id", "sort_order"]:
        assert after_rename[field] == before_rename[field]
    assert app_client.get(f"/api/v1/decks/{first['id']}/versions").json() == versions_before
    assert app_client.patch(
        f"/api/v1/decks/{third['id']}",
        headers=auth_headers,
        json={"name": "Shared deck name"},
    ).status_code == 200

    ungrouped_before = _section_ids(app_client, None)
    beta_members = _section_ids(app_client, beta["id"])
    deleted = app_client.delete(f"/api/v1/groups/{beta['id']}", headers=auth_headers)
    assert deleted.status_code == 409
    assert deleted.json()["detail"]["code"] == "group_not_empty"
    assert _section_ids(app_client, beta["id"]) == beta_members
    assert _section_ids(app_client, None) == ungrouped_before
    assert app_client.post(
        "/api/v1/decks/move", headers=auth_headers,
        # An end-clamped index also includes any not-yet-ready siblings, which
        # are intentionally absent from the public library response.
        json={"deck_ids": beta_members, "group_id": None, "index": 99999},
    ).status_code == 200
    assert app_client.delete(f"/api/v1/groups/{beta['id']}", headers=auth_headers).status_code == 204
    assert _section_ids(app_client, None) == [*ungrouped_before, *beta_members]
    library = app_client.get("/api/v1/library").json()
    assert beta["id"] not in [group["id"] for group in library["groups"]]
    assert all(app_client.get(f"/v/{deck_id}/").status_code == 200 for deck_id in beta_members)

    # Group single-target reorder clamps without accepting a full replacement array.
    assert app_client.post(
        f"/api/v1/groups/{alpha['id']}/move",
        headers=auth_headers,
        json={"index": 999},
    ).status_code == 200
    groups = [group for group in app_client.get("/api/v1/library").json()["groups"]
              if group["parent_id"] is None]
    assert [group["sort_order"] for group in groups] == list(range(len(groups)))

    assert app_client.post(
        "/api/v1/decks/missing-deck/move",
        headers=auth_headers,
        json={"group_id": None, "index": 0},
    ).status_code == 404
    missing_group = app_client.post(
        f"/api/v1/decks/{second['id']}/move",
        headers=auth_headers,
        json={"group_id": "missing-group", "index": 0},
    )
    assert missing_group.status_code == 404
    assert missing_group.json()["detail"]["code"] == "not_found"
    assert app_client.patch(
        "/api/v1/groups/missing-group",
        headers=auth_headers,
        json={"name": "x"},
    ).status_code == 404
    assert app_client.post(
        "/api/v1/groups/missing-group/move",
        headers=auth_headers,
        json={"index": 0},
    ).status_code == 404

    legacy_after = app_client.get("/api/v1/decks").json()["items"]
    assert [deck["id"] for deck in legacy_after] == legacy_ids_before


def test_transaction_failure_injection_rolls_back_all_organization_changes(
    app_client, auth_headers, monkeypatch
):
    from server.config import database
    from server.repository import DeckRepository

    one = _create_deck(app_client, auth_headers, "rollback-one")
    two = _create_deck(app_client, auth_headers, "rollback-two")
    source = _create_group(app_client, auth_headers, "Rollback source")
    target = _create_group(app_client, auth_headers, "Rollback target")
    repo = DeckRepository(database.sqloader, database.db_instance)
    repo.move_deck(one["id"], source["id"], 0)
    repo.move_deck(two["id"], source["id"], 1)
    original_checkpoint = DeckRepository._checkpoint

    def fail_cross(self, name):
        if name == "deck_move.after_source":
            raise RuntimeError("injected cross-group failure")

    monkeypatch.setattr(DeckRepository, "_checkpoint", fail_cross)
    with pytest.raises(RuntimeError, match="cross-group"):
        repo.move_deck(one["id"], target["id"], 0)
    assert repo.get(one["id"])["group_id"] == source["id"]
    assert _relative(_section_ids(app_client, source["id"]), {one["id"], two["id"]}) == [
        one["id"],
        two["id"],
    ]
    assert one["id"] not in _section_ids(app_client, target["id"])

    def fail_delete(self, name):
        if name == "group_delete.after_delete":
            raise RuntimeError("injected group-delete failure")

    monkeypatch.setattr(DeckRepository, "_checkpoint", fail_delete)
    with pytest.raises(RuntimeError, match="group-delete"):
        repo.delete_group(target["id"])
    assert repo.get_group(target["id"]) is not None
    assert repo.get_group(source["id"]) is not None
    assert repo.get(one["id"])["group_id"] == source["id"]
    assert repo.get(two["id"])["group_id"] == source["id"]

    before = _section_ids(app_client, source["id"])

    def fail_normalize(self, name):
        if name == "deck_order.after_first":
            raise RuntimeError("injected normalize failure")

    monkeypatch.setattr(DeckRepository, "_checkpoint", fail_normalize)
    with pytest.raises(RuntimeError, match="normalize"):
        repo.move_deck(two["id"], source["id"], 0)
    assert _section_ids(app_client, source["id"]) == before
    monkeypatch.setattr(DeckRepository, "_checkpoint", original_checkpoint)


def test_serialized_moves_apply_to_current_server_state(
    app_client, auth_headers, monkeypatch
):
    from server.config import database
    from server.repository import DeckRepository

    first = _create_deck(app_client, auth_headers, "race-first")
    second = _create_deck(app_client, auth_headers, "race-second")
    third = _create_deck(app_client, auth_headers, "race-third")
    left = _create_group(app_client, auth_headers, "Race left")
    right = _create_group(app_client, auth_headers, "Race right")
    repo = DeckRepository(database.sqloader, database.db_instance)
    for index, deck in enumerate([first, second, third]):
        repo.move_deck(deck["id"], left["id"], index)

    # Two sibling moves are incremental; the second sees and preserves the first.
    repo.move_deck(third["id"], left["id"], 0)
    repo.move_deck(first["id"], left["id"], 2)
    assert _relative(
        _section_ids(app_client, left["id"]),
        {first["id"], second["id"], third["id"]},
    ) == [third["id"], second["id"], first["id"]]

    # Hold the first request inside the transaction. The second has arrived but
    # waits on the organization lock, then runs against the committed first result.
    entered = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    original_checkpoint = DeckRepository._checkpoint

    def controlled_checkpoint(self, name):
        if name == "deck_move.after_target_membership" and threading.current_thread().name == "first-move":
            entered.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(DeckRepository, "_checkpoint", controlled_checkpoint)
    first_repo = DeckRepository(database.sqloader, database.db_instance)
    second_repo = DeckRepository(database.sqloader, database.db_instance)

    def move_first():
        first_repo.move_deck(second["id"], right["id"], 0)

    def move_second():
        second_started.set()
        second_repo.move_deck(second["id"], None, 0)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="unused") as pool:
        first_future = pool.submit(
            lambda: threading.current_thread().__setattr__("name", "first-move") or move_first()
        )
        assert entered.wait(timeout=5)
        second_future = pool.submit(move_second)
        assert second_started.wait(timeout=5)
        release.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    assert repo.get(second["id"])["group_id"] is None
    assert second["id"] not in _section_ids(app_client, right["id"])
    monkeypatch.setattr(DeckRepository, "_checkpoint", original_checkpoint)

    app_client.delete(f"/api/v1/groups/{right['id']}", headers=auth_headers)
    stale = app_client.post(
        f"/api/v1/decks/{first['id']}/move",
        headers=auth_headers,
        json={"group_id": right["id"], "index": 0},
    )
    assert stale.status_code == 404


def test_console_session_auth_matrix_expiry_and_renewal(
    app_client, auth_headers, deck_zip, monkeypatch
):
    from server import auth
    from server.config import settings

    base_time = int(auth.time.time())
    monkeypatch.setattr(auth.time, "time", lambda: base_time)
    # The session-scoped client may already hold a young cookie from a prior
    # shell test. Start this issuance/expiry scenario with a fresh browser jar.
    app_client.cookies.clear()
    root = app_client.get("/")
    assert root.status_code == 200
    cookie = root.headers["set-cookie"]
    assert "mirageglass_console=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert settings.upload_token not in cookie
    assert settings.upload_token not in root.text
    same_origin = {"Origin": "http://testserver"}

    group = _create_group(app_client, same_origin, "Console group")
    assert app_client.patch(
        f"/api/v1/groups/{group['id']}",
        headers=same_origin,
        json={"name": "Console renamed"},
    ).status_code == 200
    deck = _create_deck(app_client, auth_headers, "console-managed")
    assert app_client.post(
        f"/api/v1/decks/{deck['id']}/move",
        headers=same_origin,
        json={"group_id": group["id"], "index": 0},
    ).status_code == 200
    assert app_client.patch(
        f"/api/v1/decks/{deck['id']}",
        headers=same_origin,
        json={"name": "Console deck"},
    ).status_code == 200

    version = app_client.post(
        f"/api/v1/decks/{deck['id']}/versions",
        headers=auth_headers,
        files=_files(deck_zip),
        data={"idempotency_key": "console-managed-v2"},
    )
    assert version.status_code == 201
    assert app_client.post(
        f"/api/v1/decks/{deck['id']}/versions/1/activate",
        headers=same_origin,
    ).status_code == 200

    # The same HttpOnly console session authorizes multipart uploads without
    # exposing the operation token to JavaScript.
    console_create = app_client.post(
        "/api/v1/decks",
        headers=same_origin,
        files=_files(deck_zip),
        data={"name": "Console upload", "idempotency_key": "console-upload-create"},
    )
    assert console_create.status_code == 201, console_create.text
    uploaded = console_create.json()
    console_version = app_client.post(
        f"/api/v1/decks/{uploaded['id']}/versions",
        headers=same_origin,
        files=_files(deck_zip),
        data={"idempotency_key": "console-upload-version"},
    )
    assert console_version.status_code == 201, console_version.text
    assert console_version.json()["version"] == 2
    assert app_client.post(
        "/api/v1/groups",
        headers={"Origin": "https://evil.example"},
        json={"name": "Cross-site"},
    ).status_code == 401

    old_cookie = app_client.cookies.get("mirageglass_console")
    half_life = settings.console_session_ttl_seconds // 2 + 1
    monkeypatch.setattr(auth.time, "time", lambda: base_time + half_life)
    renewed = app_client.get("/")
    assert "mirageglass_console=" in renewed.headers.get("set-cookie", "")
    assert app_client.cookies.get("mirageglass_console") != old_cookie

    monkeypatch.setattr(
        auth.time,
        "time",
        lambda: base_time + half_life + settings.console_session_ttl_seconds + 1,
    )
    expired = app_client.post(
        "/api/v1/groups",
        headers=same_origin,
        json={"name": "Expired"},
    )
    assert expired.status_code == 401
    assert expired.json()["detail"]["code"] == "unauthorized"

    # The operation token is independent of the expired browser session.
    assert _create_group(app_client, auth_headers, "Token after expiry")
    # Re-establish a real-time console session for following tests.
    monkeypatch.setattr(auth.time, "time", lambda: base_time)
    app_client.get("/")
    assert app_client.delete(
        f"/api/v1/decks/{deck['id']}", headers=same_origin
    ).status_code == 204


def test_console_write_disabled_keeps_reads_and_operation_token(
    app_client, auth_headers, deck_zip, monkeypatch
):
    from server.config import settings

    app_client.get("/")
    same_origin = {"Origin": "http://testserver"}
    monkeypatch.setattr(settings, "console_write_enabled", False)

    denied = app_client.post(
        "/api/v1/groups",
        headers=same_origin,
        json={"name": "Disabled console"},
    )
    assert denied.status_code == 401
    assert denied.json()["detail"]["code"] == "unauthorized"
    denied_create = app_client.post(
        "/api/v1/decks",
        headers=same_origin,
        files=_files(deck_zip),
        data={"name": "Disabled upload", "idempotency_key": "disabled-upload-create"},
    )
    assert denied_create.status_code == 401
    token_deck = _create_deck(app_client, auth_headers, "token-upload-disabled")
    denied_version = app_client.post(
        f"/api/v1/decks/{token_deck['id']}/versions",
        headers=same_origin,
        files=_files(deck_zip),
        data={"idempotency_key": "disabled-upload-version"},
    )
    assert denied_version.status_code == 401
    assert app_client.get("/api/v1/library").status_code == 200
    token_group = _create_group(app_client, auth_headers, "Token while disabled")
    assert app_client.delete(
        f"/api/v1/groups/{token_group['id']}", headers=auth_headers
    ).status_code == 204
