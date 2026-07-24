-- MirageGlass 002: deck versions (stable link + version history)
-- A deck becomes a container that keeps a stable address while stacking versions.
-- Version-scoped attributes (status, has_thumb, idempotency_key, error_message)
-- move to deck_versions; decks keeps only identity and the active version pointer.
-- Every existing v0 deck becomes "a deck that has exactly one version (v1)".

PRAGMA foreign_keys=OFF;

-- 1) versions table
CREATE TABLE IF NOT EXISTS deck_versions (
    deck_id         TEXT NOT NULL,
    version_no      INTEGER NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('processing', 'ready', 'failed')),
    has_thumb       INTEGER NOT NULL DEFAULT 0,     -- 0 when capture failed
    idempotency_key TEXT NOT NULL,
    error_message   TEXT,
    created_at      TEXT NOT NULL,                  -- UTC ISO8601
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (deck_id, version_no),
    FOREIGN KEY (deck_id) REFERENCES decks(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_deck_versions_idempotency_key
    ON deck_versions(idempotency_key);

-- 2) backfill: every existing deck -> its version 1 (carry status/thumb/key/error as-is)
INSERT INTO deck_versions
    (deck_id, version_no, status, has_thumb, idempotency_key, error_message, created_at, updated_at)
SELECT id, 1, status, has_thumb, idempotency_key, error_message, created_at, updated_at
FROM decks;

-- 3) rebuild decks without version-scoped columns; add active_version
CREATE TABLE decks_new (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    active_version INTEGER,               -- ready v1 -> 1, otherwise NULL
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
INSERT INTO decks_new (id, name, active_version, created_at, updated_at)
SELECT id, name,
       CASE WHEN status = 'ready' THEN 1 ELSE NULL END,
       created_at, updated_at
FROM decks;
DROP TABLE decks;
ALTER TABLE decks_new RENAME TO decks;

CREATE INDEX IF NOT EXISTS idx_decks_created_at ON decks(created_at DESC);

PRAGMA foreign_keys=ON;
