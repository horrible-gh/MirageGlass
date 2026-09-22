-- MirageGlass 004: library groups and server-owned sibling order.
-- Existing decks start ungrouped in the exact order used by GET /api/v1/decks:
-- newest created_at first, then id descending. This file is applied once by the
-- migration ledger, so later user ordering is never backfilled again.

CREATE TABLE deck_groups (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL
                CHECK (length(trim(name)) BETWEEN 1 AND 120),
    sort_order  INTEGER NOT NULL
                CHECK (sort_order >= 0),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

ALTER TABLE decks
    ADD COLUMN group_id TEXT REFERENCES deck_groups(id) ON DELETE SET NULL;

ALTER TABLE decks
    ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0
    CHECK (sort_order >= 0);

UPDATE decks
SET sort_order = (
    SELECT COUNT(*)
    FROM decks AS preceding
    WHERE preceding.created_at > decks.created_at
       OR (preceding.created_at = decks.created_at AND preceding.id > decks.id)
);

CREATE INDEX idx_deck_groups_order
    ON deck_groups(sort_order ASC, id ASC);

CREATE INDEX idx_decks_group_order
    ON decks(group_id, sort_order ASC, id ASC);