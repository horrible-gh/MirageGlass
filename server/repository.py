"""Access to the decks and deck_versions tables. All SQL lives in queries.json.

The call shape is exactly sqloader 0.2.17's SQLoader.execute/fetch_one/fetch_all
(file, query_name, params). file="queries" points at queries.json.

A deck is a stable container: an id, a display name and the number of its active
version. Each upload adds a row to deck_versions, and the deck's active_version
decides which one the viewer, thumbnail and downloads serve.
"""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone
from typing import Any, Optional

QUERY_FILE = "queries"

_ID_ALPHABET = string.ascii_lowercase + string.digits


def now_iso() -> str:
    """UTC ISO8601 string. created_at/updated_at in the database use this format."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_deck_id(length: int = 8) -> str:
    """Random 8 characters, so nobody walks in by guessing the address."""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(length))


class DeckRepository:
    def __init__(self, sqloader) -> None:
        self.sq = sqloader

    # --- deck reads -------------------------------------------------------
    def get(self, deck_id: str) -> Optional[dict[str, Any]]:
        """The deck joined with its active version's status and thumbnail flag."""
        return self.sq.fetch_one(QUERY_FILE, "decks.get_by_id", (deck_id,))

    def list_ready(self) -> list[dict[str, Any]]:
        """Decks whose active version is ready, newest first."""
        return self.sq.fetch_all(QUERY_FILE, "decks.list_ready_latest")

    # --- deck writes ------------------------------------------------------
    def create_deck(self, deck_id: str, name: str) -> dict[str, Any]:
        """A container with no active version yet. The first ready version sets it."""
        ts = now_iso()
        self.sq.execute(QUERY_FILE, "decks.create", (deck_id, name, ts, ts))
        return self.get(deck_id)

    def set_active(self, deck_id: str, version_no: int) -> None:
        self.sq.execute(QUERY_FILE, "decks.set_active", (version_no, now_iso(), deck_id))

    def delete_deck(self, deck_id: str) -> None:
        self.sq.execute(QUERY_FILE, "decks.delete", (deck_id,))
        # FK ON DELETE CASCADE only fires with PRAGMA foreign_keys=ON, so clear the
        # version rows explicitly as a safety net.
        self.sq.execute(QUERY_FILE, "deck_versions.delete_by_deck", (deck_id,))

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
    def create_version(self, deck_id: str, version_no: int, idempotency_key: str) -> None:
        """Claim a processing row. A (deck_id, version_no) or idempotency_key clash raises."""
        ts = now_iso()
        self.sq.execute(
            QUERY_FILE,
            "deck_versions.create",
            (deck_id, version_no, idempotency_key, ts, ts),
        )

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
        """Store the version's screens.json manifest, or None for a single-screen zip."""
        self.sq.execute(
            QUERY_FILE,
            "deck_versions.update_screens",
            (screens_json, now_iso(), deck_id, version_no),
        )

    def delete_version(self, deck_id: str, version_no: int) -> None:
        self.sq.execute(QUERY_FILE, "deck_versions.delete_one", (deck_id, version_no))


def to_public(row: dict[str, Any]) -> dict[str, Any]:
    """Public response shape. Paths derive from the id, so they are not stored.

    ``status``, ``version`` and ``thumb_url`` describe the deck's active version -
    what the shared /v/{id}/ link is serving right now. ``latest_version`` is the
    highest version number the deck holds, which a rollback leaves unchanged.
    """
    deck_id = row["id"]
    active_status = row.get("active_status")
    return {
        "id": deck_id,
        "name": row["name"],
        # A deck with no ready active version yet (mid-processing or first upload
        # failed) reports "processing"; it is simply not serving.
        "status": active_status if active_status is not None else "processing",
        "viewer_url": f"/v/{deck_id}/",
        "thumb_url": f"/thumbs/{deck_id}.png" if row.get("active_has_thumb") else None,
        "created_at": row["created_at"],
        "version": row.get("active_version"),
        "latest_version": row.get("latest_version"),
    }
