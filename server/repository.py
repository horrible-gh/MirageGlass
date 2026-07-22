"""Access to the decks table. All SQL lives in sql/queries/queries.json.

The call shape is exactly sqloader 0.2.17's SQLoader.execute/fetch_one/fetch_all
(file, query_name, params). file="queries" points at queries.json.
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

    # --- reads ------------------------------------------------------------
    def get(self, deck_id: str) -> Optional[dict[str, Any]]:
        return self.sq.fetch_one(QUERY_FILE, "decks.get_by_id", (deck_id,))

    def get_by_idempotency_key(self, key: str) -> Optional[dict[str, Any]]:
        return self.sq.fetch_one(QUERY_FILE, "decks.get_by_idempotency_key", (key,))

    def list_ready(self) -> list[dict[str, Any]]:
        return self.sq.fetch_all(QUERY_FILE, "decks.list_ready_latest")

    def list_all(self) -> list[dict[str, Any]]:
        return self.sq.fetch_all(QUERY_FILE, "decks.list_all_latest")

    # --- writes -----------------------------------------------------------
    def create_processing(self, deck_id: str, name: str, idempotency_key: str) -> dict[str, Any]:
        ts = now_iso()
        self.sq.execute(
            QUERY_FILE,
            "decks.create",
            (deck_id, name, None, "processing", idempotency_key, ts, ts),
        )
        return self.get(deck_id)

    def set_status(self, deck_id: str, status: str, error_message: Optional[str] = None) -> None:
        self.sq.execute(
            QUERY_FILE, "decks.update_status", (status, error_message, now_iso(), deck_id)
        )

    def set_thumb(self, deck_id: str, has_thumb: bool) -> None:
        self.sq.execute(
            QUERY_FILE, "decks.update_thumb", (1 if has_thumb else 0, now_iso(), deck_id)
        )

    def delete(self, deck_id: str) -> None:
        self.sq.execute(QUERY_FILE, "decks.delete", (deck_id,))


def to_public(row: dict[str, Any]) -> dict[str, Any]:
    """Public response shape. Paths derive from the id, so they are not stored in the database."""
    deck_id = row["id"]
    return {
        "id": deck_id,
        "name": row["name"],
        "status": row["status"],
        "viewer_url": f"/v/{deck_id}/",
        "thumb_url": f"/thumbs/{deck_id}.png" if row.get("has_thumb") else None,
        "created_at": row["created_at"],
    }
