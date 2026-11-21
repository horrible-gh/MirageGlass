"""Database access for decks, versions, groups, and server-owned library order.

All SQL stays in queries.json. Organization mutations acquire a process lock and
an SQLite BEGIN IMMEDIATE transaction. The database transaction is the atomicity
boundary; the lock also gives deterministic arrival ordering to threads in one
server process.
"""

from __future__ import annotations

import json
import secrets
import string
import threading
from datetime import datetime, timezone
from typing import Any, Optional

QUERY_FILE = "queries"

_ID_ALPHABET = string.ascii_lowercase + string.digits
_ORGANIZATION_LOCK = threading.RLock()


class OrganizationNotFound(Exception):
    def __init__(self, entity: str) -> None:
        super().__init__(entity)
        self.entity = entity


def now_iso() -> str:
    """Return the UTC ISO8601 form used by all database timestamps."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_deck_id(length: int = 8) -> str:
    """Return a compact URL-safe random identifier."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


def new_group_id() -> str:
    return "g" + new_deck_id(11)


class DeckRepository:
    def __init__(self, sqloader, db_instance=None) -> None:
        self.sq = sqloader
        self.db = db_instance or sqloader.db

    def _sql(self, query_name: str) -> str:
        return self.sq.load_sql(QUERY_FILE, query_name)

    def _checkpoint(self, name: str) -> None:
        """No-op seam used by rollback tests to inject a mid-transaction failure."""

    def _begin_immediate(self, tx) -> None:
        tx.execute("BEGIN IMMEDIATE")

    def _deck_sibling_ids(self, tx, group_id: Optional[str]) -> list[str]:
        rows = tx.fetch_all(
            self._sql("organization.list_deck_siblings"),
            (group_id,),
        )
        return [row["id"] for row in rows]

    def _group_ids(self, tx) -> list[str]:
        rows = tx.fetch_all(self._sql("deck_groups.list_ordered"))
        return [row["id"] for row in rows]

    def _write_deck_order(self, tx, deck_ids: list[str]) -> None:
        sql = self._sql("decks.update_order")
        for index, deck_id in enumerate(deck_ids):
            tx.execute(sql, (index, deck_id))
            if index == 0:
                self._checkpoint("deck_order.after_first")

    def _write_group_order(self, tx, group_ids: list[str]) -> None:
        sql = self._sql("deck_groups.update_order")
        for index, group_id in enumerate(group_ids):
            tx.execute(sql, (index, group_id))
            if index == 0:
                self._checkpoint("group_order.after_first")

    # --- deck reads -------------------------------------------------------
    def get(self, deck_id: str) -> Optional[dict[str, Any]]:
        return self.sq.fetch_one(QUERY_FILE, "decks.get_by_id", (deck_id,))

    def get_library_deck(self, deck_id: str) -> Optional[dict[str, Any]]:
        row = self.sq.fetch_one(QUERY_FILE, "library.get_ready_deck", (deck_id,))
        return library_deck_to_public(row) if row else None

    def list_ready(self) -> list[dict[str, Any]]:
        """Keep the legacy newest-first listing independent of library order."""
        return self.sq.fetch_all(QUERY_FILE, "decks.list_ready_latest")

    def list_library(self) -> dict[str, Any]:
        groups = self.sq.fetch_all(QUERY_FILE, "deck_groups.list_ordered")
        public_groups = [
            {
                "id": row["id"],
                "name": row["name"],
                "sort_order": row["sort_order"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "decks": [],
            }
            for row in groups
        ]
        by_id = {group["id"]: group for group in public_groups}
        ungrouped = []
        for row in self.sq.fetch_all(QUERY_FILE, "library.list_ready_decks"):
            item = library_deck_to_public(row)
            group = by_id.get(item["group_id"])
            if group is None:
                item["group_id"] = None
                ungrouped.append(item)
            else:
                group["decks"].append(item)
        return {
            "groups": public_groups,
            "ungrouped": {"name": "Ungrouped", "decks": ungrouped},
        }

    # --- deck writes ------------------------------------------------------
    def create_deck(self, deck_id: str, name: str) -> dict[str, Any]:
        """Create at the front of Ungrouped without disturbing another scope."""
        ts = now_iso()
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                siblings = self._deck_sibling_ids(tx, None)
                tx.execute(
                    self._sql("decks.create"),
                    (deck_id, name, None, 0, ts, ts),
                )
                self._write_deck_order(tx, [deck_id, *siblings])
        return self.get(deck_id)

    def rename_deck(self, deck_id: str, name: str) -> dict[str, Any]:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                cursor = tx.execute(
                    self._sql("decks.update_name"),
                    (name, now_iso(), deck_id),
                )
                if cursor.rowcount != 1:
                    raise OrganizationNotFound("deck")
        return self.get(deck_id)

    def set_active(self, deck_id: str, version_no: int) -> None:
        self.sq.execute(QUERY_FILE, "decks.set_active", (version_no, now_iso(), deck_id))

    def delete_deck(self, deck_id: str) -> None:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                organization = tx.fetch_one(
                    self._sql("decks.get_organization"),
                    (deck_id,),
                )
                if not organization:
                    return
                group_id = organization["group_id"]
                tx.execute(self._sql("decks.delete"), (deck_id,))
                # Keep the explicit cleanup because SQLite foreign keys are
                # connection-local and older sqloader connections may omit it.
                tx.execute(self._sql("deck_versions.delete_by_deck"), (deck_id,))
                remaining = self._deck_sibling_ids(tx, group_id)
                self._write_deck_order(tx, remaining)

    def move_deck(
        self,
        deck_id: str,
        target_group_id: Optional[str],
        target_index: int,
    ) -> dict[str, Any]:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                organization = tx.fetch_one(
                    self._sql("decks.get_organization"),
                    (deck_id,),
                )
                if not organization:
                    raise OrganizationNotFound("deck")
                if target_group_id is not None:
                    group = tx.fetch_one(
                        self._sql("deck_groups.get"),
                        (target_group_id,),
                    )
                    if not group:
                        raise OrganizationNotFound("group")

                source_group_id = organization["group_id"]
                if source_group_id == target_group_id:
                    ordered = self._deck_sibling_ids(tx, source_group_id)
                    ordered.remove(deck_id)
                    index = max(0, min(target_index, len(ordered)))
                    ordered.insert(index, deck_id)
                    self._write_deck_order(tx, ordered)
                else:
                    source = self._deck_sibling_ids(tx, source_group_id)
                    source.remove(deck_id)
                    target = self._deck_sibling_ids(tx, target_group_id)
                    index = max(0, min(target_index, len(target)))
                    target.insert(index, deck_id)

                    self._write_deck_order(tx, source)
                    self._checkpoint("deck_move.after_source")
                    tx.execute(
                        self._sql("decks.set_membership"),
                        (target_group_id, index, now_iso(), deck_id),
                    )
                    self._checkpoint("deck_move.after_target_membership")
                    self._write_deck_order(tx, target)
        row = self.get_library_deck(deck_id)
        return row if row is not None else self.get(deck_id)

    def move_decks(
        self,
        deck_ids: list[str],
        target_group_id: Optional[str],
        target_index: int,
    ) -> dict[str, Any]:
        """Move an ordered selection in one transaction, including cross-group selections.

        The insertion index addresses the target after removing selected decks.
        Replaying the same request produces the same membership/order when no
        concurrent write intervenes, including selections already in the target.
        There is no revision precondition; clients must reconcile concurrent
        changes before retrying an operation with an uncertain outcome.
        """
        selected = set(deck_ids)
        if not deck_ids or len(selected) != len(deck_ids) or target_index < 0:
            raise ValueError("Expected unique deck IDs and a nonnegative index.")
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                if target_group_id is not None and not tx.fetch_one(
                    self._sql("deck_groups.get"), (target_group_id,)
                ):
                    raise OrganizationNotFound("group")
                organizations = []
                for deck_id in deck_ids:
                    row = tx.fetch_one(self._sql("decks.get_organization"), (deck_id,))
                    if row is None:
                        raise OrganizationNotFound("deck")
                    organizations.append(row)

                # Validate the entire selection before changing any row.
                sources = dict.fromkeys(row["group_id"] for row in organizations)
                target = [
                    deck_id for deck_id in self._deck_sibling_ids(tx, target_group_id)
                    if deck_id not in selected
                ]
                index = min(target_index, len(target))
                target[index:index] = deck_ids
                for source_group_id in sources:
                    if source_group_id == target_group_id:
                        continue
                    remaining = [
                        deck_id for deck_id in self._deck_sibling_ids(tx, source_group_id)
                        if deck_id not in selected
                    ]
                    self._write_deck_order(tx, remaining)
                self._checkpoint("bulk_move.after_sources")

                timestamp = now_iso()
                for offset, organization in enumerate(organizations):
                    if organization["group_id"] != target_group_id:
                        tx.execute(
                            self._sql("decks.set_membership"),
                            (target_group_id, index + offset, timestamp, organization["id"]),
                        )
                    if offset == 0:
                        self._checkpoint("bulk_move.after_first_membership")
                self._write_deck_order(tx, target)
            return self.list_library()

    def set_favorites(self, deck_ids: list[str], favorite: bool) -> dict[str, Any]:
        """Set an explicit shared value; repeated requests never toggle it back."""
        if not deck_ids or len(set(deck_ids)) != len(deck_ids):
            raise ValueError("Expected a nonempty selection of unique deck IDs.")
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                for deck_id in deck_ids:
                    if not tx.fetch_one(self._sql("decks.get_organization"), (deck_id,)):
                        raise OrganizationNotFound("deck")
                for index, deck_id in enumerate(deck_ids):
                    tx.execute(self._sql("decks.set_favorite"), (int(favorite), deck_id))
                    if index == 0:
                        self._checkpoint("favorites.after_first")
            return self.list_library()

    def record_visit(self, deck_id: str) -> dict[str, Any]:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                timestamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
                cursor = tx.execute(self._sql("decks.record_visit"), (timestamp, deck_id))
                if cursor.rowcount != 1:
                    raise OrganizationNotFound("deck")
            return self.get_library_deck(deck_id) or to_public(self.get(deck_id))

    # --- group reads and writes ------------------------------------------
    def get_group(self, group_id: str) -> Optional[dict[str, Any]]:
        return self.sq.fetch_one(QUERY_FILE, "deck_groups.get", (group_id,))

    def create_group(self, group_id: str, name: str) -> dict[str, Any]:
        ts = now_iso()
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                group_ids = self._group_ids(tx)
                tx.execute(
                    self._sql("deck_groups.create"),
                    (group_id, name, 0, ts, ts),
                )
                self._write_group_order(tx, [group_id, *group_ids])
        return self.get_group(group_id)

    def rename_group(self, group_id: str, name: str) -> dict[str, Any]:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                cursor = tx.execute(
                    self._sql("deck_groups.update_name"),
                    (name, now_iso(), group_id),
                )
                if cursor.rowcount != 1:
                    raise OrganizationNotFound("group")
        return self.get_group(group_id)

    def move_group(self, group_id: str, target_index: int) -> dict[str, Any]:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                if not tx.fetch_one(self._sql("deck_groups.get"), (group_id,)):
                    raise OrganizationNotFound("group")
                ordered = self._group_ids(tx)
                ordered.remove(group_id)
                index = max(0, min(target_index, len(ordered)))
                ordered.insert(index, group_id)
                self._write_group_order(tx, ordered)
        return self.get_group(group_id)

    def delete_group(self, group_id: str) -> None:
        """Append the group's decks to Ungrouped and delete only the group row."""
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                if not tx.fetch_one(self._sql("deck_groups.get"), (group_id,)):
                    raise OrganizationNotFound("group")
                ungrouped = self._deck_sibling_ids(tx, None)
                moved = self._deck_sibling_ids(tx, group_id)
                combined = [*ungrouped, *moved]
                membership_sql = self._sql("decks.set_membership")
                timestamp = now_iso()
                for index, deck_id in enumerate(combined):
                    tx.execute(membership_sql, (None, index, timestamp, deck_id))
                self._checkpoint("group_delete.after_decks")
                tx.execute(self._sql("deck_groups.delete"), (group_id,))
                groups = self._group_ids(tx)
                self._write_group_order(tx, groups)

    # --- version reads ----------------------------------------------------
    def get_version(self, deck_id: str, version_no: int) -> Optional[dict[str, Any]]:
        return self.sq.fetch_one(QUERY_FILE, "deck_versions.get", (deck_id, version_no))

    def get_version_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        return self.sq.fetch_one(QUERY_FILE, "deck_versions.get_by_idempotency_key", (key,))

    def list_versions(self, deck_id: str) -> list[dict[str, Any]]:
        return self.sq.fetch_all(QUERY_FILE, "deck_versions.list_by_deck", (deck_id,))

    def max_version(self, deck_id: str) -> Optional[int]:
        row = self.sq.fetch_one(QUERY_FILE, "deck_versions.max_version", (deck_id,))
        if not row or row.get("n") is None:
            return None
        return row["n"]

    # --- version writes ---------------------------------------------------
    def create_version(
        self, deck_id: str, version_no: int, idempotency_key: str, note: str = ""
    ) -> None:
        ts = now_iso()
        self.sq.execute(
            QUERY_FILE,
            "deck_versions.create",
            (deck_id, version_no, idempotency_key, ts, ts, note),
        )

    def set_version_note(self, deck_id: str, version_no: int, note: str) -> dict[str, Any]:
        with _ORGANIZATION_LOCK:
            with self.db.begin_transaction() as tx:
                self._begin_immediate(tx)
                cursor = tx.execute(
                    self._sql("deck_versions.update_note"),
                    (note, now_iso(), deck_id, version_no),
                )
                if cursor.rowcount != 1:
                    raise OrganizationNotFound("version")
            return self.get_version(deck_id, version_no)

    def set_version_status(
        self, deck_id: str, version_no: int, status: str, error_message: Optional[str] = None
    ) -> None:
        self.sq.execute(
            QUERY_FILE,
            "deck_versions.update_status",
            (status, error_message, now_iso(), deck_id, version_no),
        )

    def set_version_thumb(self, deck_id: str, version_no: int, has_thumb: bool) -> None:
        self.sq.execute(
            QUERY_FILE,
            "deck_versions.update_thumb",
            (1 if has_thumb else 0, now_iso(), deck_id, version_no),
        )

    def set_version_screens(self, deck_id: str, version_no: int, screens_json: Optional[str]) -> None:
        self.sq.execute(
            QUERY_FILE,
            "deck_versions.update_screens",
            (screens_json, now_iso(), deck_id, version_no),
        )

    def delete_version(self, deck_id: str, version_no: int) -> None:
        self.sq.execute(QUERY_FILE, "deck_versions.delete_one", (deck_id, version_no))


def to_public(row: dict[str, Any]) -> dict[str, Any]:
    """Preserve legacy fields while exposing shared organization metadata."""
    deck_id = row["id"]
    active_status = row.get("active_status")
    return {
        "id": deck_id,
        "name": row["name"],
        "status": active_status if active_status is not None else "processing",
        "viewer_url": f"/v/{deck_id}/",
        "thumb_url": f"/thumbs/{deck_id}.png" if row.get("active_has_thumb") else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "version": row.get("active_version"),
        "latest_version": row.get("latest_version"),
        "group_id": row.get("group_id"),
        "sort_order": row.get("sort_order", 0),
        "favorite": bool(row.get("favorite", False)),
        "last_viewed_at": row.get("last_viewed_at"),
    }


def library_deck_to_public(row: dict[str, Any]) -> dict[str, Any]:
    raw_screens = row.get("active_screens_json")
    screen_count = 1
    if raw_screens:
        try:
            parsed = json.loads(raw_screens)
            if isinstance(parsed, list) and parsed:
                screen_count = len(parsed)
        except (TypeError, json.JSONDecodeError):
            screen_count = 1
    deck_id = row["id"]
    return {
        "id": deck_id,
        "name": row["name"],
        "status": row["active_status"],
        "viewer_url": f"/v/{deck_id}/",
        "thumb_url": f"/thumbs/{deck_id}.png" if row.get("active_has_thumb") else None,
        "active_version": row.get("active_version"),
        "latest_version": row.get("latest_version"),
        "total_versions": row.get("total_versions", 0),
        "latest_version_at": row.get("latest_version_at"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "screen_count": screen_count,
        "group_id": row.get("group_id"),
        "sort_order": row["sort_order"],
        "favorite": bool(row.get("favorite", False)),
        "last_viewed_at": row.get("last_viewed_at"),
    }
