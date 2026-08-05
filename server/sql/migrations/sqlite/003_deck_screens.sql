-- MirageGlass 003: multi-screen versions
-- Adds an optional multi-screen manifest to deck_versions. A version with no
-- manifest (every version created before this migration, and any plain
-- single-index.html zip since) is a single implicit screen named after the
-- deck, applied at the application layer rather than backfilled here.

ALTER TABLE deck_versions ADD COLUMN screens_json TEXT;
