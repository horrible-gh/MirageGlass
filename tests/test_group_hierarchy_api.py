"""Hierarchy compatibility, legacy migration, and atomic tree operations."""

from __future__ import annotations

import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from conftest import PROJECT_ROOT
from test_library_api import _create_deck, _library_deck, _section_ids


def _group(client, headers, name, parent_id=None):
    response = client.post(
        "/api/v1/groups", headers=headers, json={"name": name, "parent_id": parent_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _move(client, headers, group_id, **payload):
    return client.post(
        f"/api/v1/groups/{group_id}/move", headers=headers, json={"index": 0, **payload}
    )


def _siblings(client, parent_id):
    return sorted(
        (group for group in client.get("/api/v1/library").json()["groups"]
         if group["parent_id"] == parent_id),
        key=lambda group: group["sort_order"],
    )


def _conflict(response, code):
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == code


def test_hierarchy_migration_preserves_legacy_duplicates_metadata_and_ledger(
    tmp_path, app_client, monkeypatch
):
    from server import config
    from server.repository import DeckRepository, OrganizationConflict

    legacy_migrations = tmp_path / "legacy-migrations"
    legacy_migrations.mkdir()
    for migration in sorted((PROJECT_ROOT / "server/sql/migrations/sqlite").glob("00[1-5]_*.sql")):
        shutil.copyfile(migration, legacy_migrations / migration.name)
    storage = tmp_path / "legacy-hierarchy"
    isolated = replace(config.settings, storage_dir=storage, db_path=storage / "mirageglass.db")
    with monkeypatch.context() as patch:
        patch.setattr(config, "MIGRATION_PATH", str(legacy_migrations))
        config.Database(isolated).init()

    timestamp = "2026-01-02T03:04:05+00:00"
    with sqlite3.connect(isolated.db_path) as connection:
        connection.executemany(
            "INSERT INTO deck_groups (id,name,sort_order,created_at,updated_at) VALUES (?,?,?,?,?)",
            [("legacy-a", "Legacy", 7, timestamp, timestamp),
             ("legacy-b", "Legacy", 3, timestamp, timestamp)],
        )
        connection.execute(
            "INSERT INTO decks (id,name,active_version,created_at,updated_at,group_id,"
            "sort_order,favorite,last_viewed_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("legacy-deck", "Saved deck", 2, timestamp, timestamp, "legacy-a", 4, 1, timestamp),
        )
        connection.executemany(
            "INSERT INTO deck_versions (deck_id,version_no,status,has_thumb,idempotency_key,"
            "created_at,updated_at,screens_json,note) VALUES (?,?,?,?,?,?,?,?,?)",
            [("legacy-deck", version, "ready", 1, f"legacy-key-{version}", timestamp,
              timestamp, '[{"key":"main","entry":"index.html"}]', f"Saved note {version}")
             for version in (1, 2)],
        )
        before = {table: connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                  for table in ("decks", "deck_versions", "deck_groups")}

    db, loader, _ = config.Database(isolated).init()
    with sqlite3.connect(isolated.db_path) as connection:
        for table in ("decks", "deck_versions"):
            assert connection.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall() == before[table]
        groups = connection.execute("SELECT * FROM deck_groups ORDER BY 1,2").fetchall()
        assert [row[:-1] for row in groups] == before["deck_groups"]
        assert all(row[-1] is None for row in groups)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    repo = DeckRepository(loader, db)
    # Old duplicate IDs remain usable; new collisions are rejected.
    assert repo.rename_group("legacy-a", "Legacy")["id"] == "legacy-a"
    assert repo.move_group("legacy-b", 0)["parent_id"] is None
    with pytest.raises(OrganizationConflict) as duplicate:
        repo.create_group("legacy-new", "Legacy")
    assert duplicate.value.code == "duplicate_group_name"
    repo.create_group("new-parent", "New parent")
    repo.move_group("legacy-a", 0, "new-parent")
    before_reopen = repo.list_library()
    reopened_db, reopened_loader, _ = config.Database(isolated).init()
    reopened = DeckRepository(reopened_loader, reopened_db)
    assert reopened.list_library() == before_reopen
    assert reopened.get("legacy-deck")["group_id"] == "legacy-a"
    assert reopened.get_version("legacy-deck", 2)["note"] == "Saved note 2"


def test_names_are_trimmed_case_sensitive_and_scoped_to_each_parent(app_client, auth_headers):
    left = _group(app_client, auth_headers, "Names left")
    right = _group(app_client, auth_headers, "Names right")
    first = _group(app_client, auth_headers, "  Shared  ", left["id"])
    second = _group(app_client, auth_headers, "Shared", right["id"])
    assert first["name"] == second["name"] == "Shared"
    _group(app_client, auth_headers, "shared", left["id"])
    before = app_client.get("/api/v1/library").json()
    for name, parent in [(" Shared ", left["id"]), ("Names left", None)]:
        _conflict(app_client.post(
            "/api/v1/groups", headers=auth_headers, json={"name": name, "parent_id": parent}
        ), "duplicate_group_name")
    _conflict(_move(app_client, auth_headers, second["id"], parent_id=left["id"]),
              "duplicate_group_name")
    _conflict(app_client.patch(
        f"/api/v1/groups/{right['id']}", headers=auth_headers, json={"name": " Names left "}
    ), "duplicate_group_name")
    assert app_client.get("/api/v1/library").json() == before


def test_omitted_parent_preserves_nested_scope_explicit_null_restores_root(app_client, auth_headers):
    parent = _group(app_client, auth_headers, "Ordering parent")
    other = _group(app_client, auth_headers, "Ordering other")
    children = [_group(app_client, auth_headers, name, parent["id"])
                for name in ("First", "Second", "Third")]
    unrelated = _group(app_client, auth_headers, "Untouched", other["id"])
    untouched_before = _siblings(app_client, other["id"])
    # The legacy request shape must only reorder within the existing parent.
    moved = _move(app_client, auth_headers, children[0]["id"], index=999)
    assert moved.status_code == 200 and moved.json()["parent_id"] == parent["id"]
    assert [group["id"] for group in _siblings(app_client, parent["id"])] == [
        children[2]["id"], children[1]["id"], children[0]["id"]
    ]
    renamed = app_client.patch(
        f"/api/v1/groups/{children[0]['id']}", headers=auth_headers, json={"name": "First renamed"}
    )
    assert renamed.status_code == 200 and renamed.json()["parent_id"] == parent["id"]
    moved = _move(app_client, auth_headers, children[1]["id"], parent_id=None, index=0)
    assert moved.status_code == 200 and moved.json()["parent_id"] is None
    assert _siblings(app_client, None)[0]["id"] == children[1]["id"]
    assert [group["sort_order"] for group in _siblings(app_client, parent["id"])] == [0, 1]
    assert _siblings(app_client, other["id"]) == untouched_before
    assert unrelated["sort_order"] == 0


def test_deep_cycle_rejection_and_reparent_preserve_subtree_decks_and_links(app_client, auth_headers):
    root = _group(app_client, auth_headers, "Deep project")
    chain = [root]
    for depth in range(1, 13):
        chain.append(_group(app_client, auth_headers, f"Level {depth}", chain[-1]["id"]))
    leaf_deck = _create_deck(app_client, auth_headers, "hierarchy-deep-leaf")
    direct_deck = _create_deck(app_client, auth_headers, "hierarchy-deep-direct")
    for deck, group in ((leaf_deck, chain[-1]), (direct_deck, root)):
        assert app_client.post(
            f"/api/v1/decks/{deck['id']}/move", headers=auth_headers,
            json={"group_id": group["id"], "index": 0},
        ).status_code == 200
    before = app_client.get("/api/v1/library").json()
    for group, target in [(root, root), (root, chain[-1]), (chain[3], chain[-1])]:
        _conflict(_move(app_client, auth_headers, group["id"], parent_id=target["id"]), "group_cycle")
    assert app_client.get("/api/v1/library").json() == before
    assert _section_ids(app_client, root["id"]) == [direct_deck["id"]]
    assert _section_ids(app_client, chain[-1]["id"]) == [leaf_deck["id"]]
    assert leaf_deck["id"] not in _section_ids(app_client, None)
    versions = app_client.get(f"/api/v1/decks/{leaf_deck['id']}/versions").json()
    stable = app_client.get(f"/v/{leaf_deck['id']}/").content
    fixed = app_client.get(f"/v/{leaf_deck['id']}/v1/").content
    target = _group(app_client, auth_headers, "Deep archive")
    moved = _move(app_client, auth_headers, root["id"], parent_id=target["id"])
    assert moved.status_code == 200 and moved.json()["id"] == root["id"]
    after = app_client.get("/api/v1/library").json()
    by_id = {group["id"]: group for group in after["groups"]}
    assert by_id[root["id"]]["parent_id"] == target["id"]
    for parent, child in zip(chain, chain[1:]):
        assert by_id[child["id"]]["parent_id"] == parent["id"]
    for deck in (leaf_deck, direct_deck):
        assert sum(item["id"] == deck["id"] for group in [*after["groups"], after["ungrouped"]]
                   for item in group["decks"]) == 1
    assert _library_deck(app_client, leaf_deck["id"])["group_id"] == chain[-1]["id"]
    assert app_client.get(f"/api/v1/decks/{leaf_deck['id']}/versions").json() == versions
    for path, content in [(f"/v/{leaf_deck['id']}/", stable), (f"/v/{leaf_deck['id']}/v1/", fixed)]:
        response = app_client.get(path)
        assert response.status_code == 200 and response.content == content


def test_delete_requires_no_children_and_no_decks_even_when_not_ready(app_client, auth_headers):
    from server.config import database
    from server.repository import DeckRepository

    parent = _group(app_client, auth_headers, "Deletion parent")
    child = _group(app_client, auth_headers, "Deletion child", parent["id"])
    repo = DeckRepository(database.sqloader, database.db_instance)
    repo.create_deck("hierarchy-processing", "Still processing")
    repo.move_deck("hierarchy-processing", child["id"], 0)
    before = app_client.get("/api/v1/library").json()
    assert _section_ids(app_client, child["id"]) == []
    for group in (parent, child):
        _conflict(app_client.delete(f"/api/v1/groups/{group['id']}", headers=auth_headers),
                  "group_not_empty")
    assert app_client.get("/api/v1/library").json() == before
    assert repo.get("hierarchy-processing")["group_id"] == child["id"]
    repo.move_deck("hierarchy-processing", None, 0)
    assert app_client.delete(f"/api/v1/groups/{child['id']}", headers=auth_headers).status_code == 204
    assert app_client.delete(f"/api/v1/groups/{parent['id']}", headers=auth_headers).status_code == 204
    assert repo.get("hierarchy-processing") is not None
    repo.delete_deck("hierarchy-processing")


@pytest.mark.parametrize("parent_id", ["", 7, True, [], {}])
def test_invalid_parent_types_reject_without_creating_or_moving(app_client, auth_headers, parent_id):
    group = _group(app_client, auth_headers, f"Invalid parent {parent_id!r}")
    before = app_client.get("/api/v1/library").json()
    response = app_client.post(
        "/api/v1/groups", headers=auth_headers, json={"name": "Bad parent", "parent_id": parent_id}
    )
    assert response.status_code == 422
    assert _move(app_client, auth_headers, group["id"], parent_id=parent_id).status_code == 422
    assert app_client.get("/api/v1/library").json() == before


def test_missing_parent_and_unauthorized_hierarchy_writes_do_not_change_state(app_client, auth_headers):
    parent = _group(app_client, auth_headers, "Hierarchy auth parent")
    child = _group(app_client, auth_headers, "Hierarchy auth child", parent["id"])
    before = app_client.get("/api/v1/library").json()
    assert app_client.post(
        "/api/v1/groups", headers=auth_headers,
        json={"name": "Missing parent", "parent_id": "missing-parent"},
    ).status_code == 404
    assert _move(app_client, auth_headers, child["id"], parent_id="missing-parent").status_code == 404
    for headers in ({}, {"Origin": "https://other.example"}):
        assert app_client.post(
            "/api/v1/groups", headers=headers, json={"name": "Unauthorized", "parent_id": parent["id"]}
        ).status_code == 401
        assert _move(app_client, headers, child["id"], parent_id=None).status_code == 401
    assert app_client.get("/api/v1/library").json() == before


@pytest.mark.parametrize("checkpoint", ["group_move.after_source", "group_move.after_parent"])
def test_reparent_rolls_back_parent_and_both_sibling_orders(app_client, auth_headers, monkeypatch, checkpoint):
    from server.config import database
    from server.repository import DeckRepository

    source = _group(app_client, auth_headers, f"Source {checkpoint}")
    target = _group(app_client, auth_headers, f"Target {checkpoint}")
    moving = _group(app_client, auth_headers, "Moving", source["id"])
    _group(app_client, auth_headers, "Remaining", source["id"])
    _group(app_client, auth_headers, "Destination sibling", target["id"])
    _group(app_client, auth_headers, "Descendant", moving["id"])
    before = app_client.get("/api/v1/library").json()

    def fail(self, name):
        if name == checkpoint:
            raise RuntimeError("Injected hierarchy failure")

    monkeypatch.setattr(DeckRepository, "_checkpoint", fail)
    with pytest.raises(RuntimeError, match="Injected hierarchy failure"):
        DeckRepository(database.sqloader, database.db_instance).move_group(moving["id"], 0, target["id"])
    assert app_client.get("/api/v1/library").json() == before


def test_concurrent_reciprocal_moves_revalidate_after_first_commit(app_client, auth_headers, monkeypatch):
    from server.config import database
    from server.repository import DeckRepository, OrganizationConflict

    left = _group(app_client, auth_headers, "Concurrent left")
    right = _group(app_client, auth_headers, "Concurrent right")
    first_repo = DeckRepository(database.sqloader, database.db_instance)
    second_repo = DeckRepository(database.sqloader, database.db_instance)
    entered, release, second_started = threading.Event(), threading.Event(), threading.Event()

    def checkpoint(self, name):
        if name == "group_move.after_parent" and self is first_repo:
            entered.set()
            assert release.wait(timeout=5)

    def second_move():
        second_started.set()
        return second_repo.move_group(right["id"], 0, left["id"])

    monkeypatch.setattr(DeckRepository, "_checkpoint", checkpoint)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(first_repo.move_group, left["id"], 0, right["id"])
        try:
            assert entered.wait(timeout=5)
            second_future = pool.submit(second_move)
            assert second_started.wait(timeout=5)
        finally:
            release.set()
        assert first_future.result(timeout=5)["parent_id"] == right["id"]
        with pytest.raises(OrganizationConflict) as cycle:
            second_future.result(timeout=5)
        assert cycle.value.code == "group_cycle"
    assert first_repo.get_group(right["id"])["parent_id"] is None
    assert first_repo.get_group(left["id"])["parent_id"] == right["id"]


def test_concurrent_same_name_creates_one_sibling(app_client, auth_headers):
    from server.config import database
    from server.repository import DeckRepository, OrganizationConflict

    parent = _group(app_client, auth_headers, "Concurrent names parent")
    barrier = threading.Barrier(2)

    def create(identifier):
        repo = DeckRepository(database.sqloader, database.db_instance)
        barrier.wait(timeout=5)
        try:
            repo.create_group(identifier, "Only one", parent["id"])
            return "created"
        except OrganizationConflict as conflict:
            return conflict.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(create, ["concurrent-name-a", "concurrent-name-b"]))
    assert sorted(results) == ["created", "duplicate_group_name"]
    siblings = _siblings(app_client, parent["id"])
    assert len(siblings) == 1 and siblings[0]["name"] == "Only one"


def test_library_read_keeps_groups_and_memberships_in_one_snapshot(tmp_path, app_client, monkeypatch):
    from server.config import Database, settings
    from server.repository import DeckRepository

    storage = tmp_path / "snapshot-hierarchy"
    isolated = replace(settings, storage_dir=storage, db_path=storage / "mirageglass.db")
    db, loader, _ = Database(isolated).init()
    with sqlite3.connect(isolated.db_path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer = DeckRepository(loader, db)
    reader = DeckRepository(loader, db)
    writer.create_group("snapshot-original", "Original")
    writer.create_deck("snapshot-deck", "Saved design")
    writer.create_version("snapshot-deck", 1, "snapshot-version")
    writer.set_version_status("snapshot-deck", 1, "ready")
    writer.set_active("snapshot-deck", 1)
    writer.move_deck("snapshot-deck", "snapshot-original", 0)
    before = reader.list_library()
    entered, release = threading.Event(), threading.Event()

    def checkpoint(self, name):
        if self is reader and name == "library.after_groups":
            entered.set()
            assert release.wait(timeout=5)

    monkeypatch.setattr(DeckRepository, "_checkpoint", checkpoint)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(reader.list_library)
        try:
            assert entered.wait(timeout=5)
            # Commit after the reader has fetched groups but before it fetches
            # memberships. A split read would falsely make this deck Ungrouped.
            writer.create_group("snapshot-new", "New parent")
            writer.move_deck("snapshot-deck", "snapshot-new", 0)
        finally:
            release.set()
        assert pending.result(timeout=5) == before
    after = writer.list_library()
    new_group = next(group for group in after["groups"] if group["id"] == "snapshot-new")
    assert [deck["id"] for deck in new_group["decks"]] == ["snapshot-deck"]
    assert after["ungrouped"]["decks"] == []
