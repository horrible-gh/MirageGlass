-- MirageGlass 005: shared library metadata and editable version notes.
-- Defaults preserve every existing deck, group, version, and ordering value.
-- Browser-only pins can be imported through the atomic favorites endpoint.

ALTER TABLE decks ADD COLUMN favorite INTEGER NOT NULL DEFAULT 0
    CHECK (favorite IN (0, 1));

ALTER TABLE decks ADD COLUMN last_viewed_at TEXT;

ALTER TABLE deck_versions ADD COLUMN note TEXT NOT NULL DEFAULT ''
    CHECK (length(note) <= 4000);

CREATE INDEX idx_decks_favorite ON decks(favorite);
CREATE INDEX idx_decks_last_viewed ON decks(last_viewed_at DESC);
