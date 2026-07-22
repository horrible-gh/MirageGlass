-- MirageGlass 001: decks
-- One zip = one deck. No versioning, no overwrite; listings are created_at DESC.

CREATE TABLE IF NOT EXISTS decks (
    id              TEXT PRIMARY KEY,               -- URL-safe random 8 characters
    name            TEXT NOT NULL,
    group_key       TEXT,                           -- unused in v0. Reserved for stable-link grouping
    status          TEXT NOT NULL
                    CHECK (status IN ('processing', 'ready', 'failed')),
    has_thumb       INTEGER NOT NULL DEFAULT 0,     -- 0 when capture failed -> UI falls back to a numbered card
    idempotency_key TEXT NOT NULL,
    error_message   TEXT,
    created_at      TEXT NOT NULL,                  -- UTC ISO8601
    updated_at      TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_decks_idempotency_key
    ON decks(idempotency_key);

CREATE INDEX IF NOT EXISTS idx_decks_status_created_at
    ON decks(status, created_at DESC);
