-- MirageGlass 006: groups form a tree with stable IDs and sibling-local order.
-- Every existing group remains at the top level. IDs, names, timestamps,
-- ordering, deck membership, and version metadata are not rewritten.
-- Older databases may contain duplicate names, so uniqueness is enforced for
-- new changes inside BEGIN IMMEDIATE transactions, not a destructive backfill
-- or a unique index that could prevent an existing database from starting.

ALTER TABLE deck_groups ADD COLUMN parent_id TEXT
    REFERENCES deck_groups(id) ON DELETE RESTRICT
    CHECK (parent_id IS NULL OR parent_id <> id);

CREATE INDEX idx_deck_groups_parent_order
    ON deck_groups(parent_id, sort_order ASC, id ASC);

CREATE INDEX idx_deck_groups_parent_name
    ON deck_groups(parent_id, name);
