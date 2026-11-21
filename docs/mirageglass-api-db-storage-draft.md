# API, database, and storage contract

This document records the implemented contract. For request schemas use
`/openapi.json`; for the complete route catalog use `GET /api/v1/help`; for
working agent examples see [automation.md](automation.md).

## Authorization and compatibility

All management writes accept the configured operation Bearer token. The
first-party browser can alternatively use the signed, HttpOnly, origin-bound
console cookie when `MIRAGEGLASS_CONSOLE_WRITE` is enabled. A cookie alone does
not authorize a cross-origin write. Reads do not require authentication.

Registration, version upload, activation, downloads, manifests, and stable
viewer URLs retain their existing request semantics. Optional multipart
`note` and response metadata are additive. Existing agents need no browser
session, new token, or different upload format.

## Library model

`GET /api/v1/library` returns `groups` in server order and a separate
`ungrouped` object. Each real group has a stable ID, display name, timestamps,
`sort_order`, and an ordered `decks` array. Ungrouped is the set of decks whose
`group_id` is SQL NULL; it has no row or ID in `deck_groups`.

Library deck fields:

| Field | Meaning |
| --- | --- |
| `id`, `name` | Stable identity and editable display name |
| `group_id`, `sort_order` | Nullable group membership and sibling position |
| `status` | Ready active version status |
| `viewer_url`, `thumb_url` | Stable viewer and optional thumbnail |
| `active_version`, `latest_version`, `total_versions` | Active pointer, greatest version number, and stored version count |
| `screen_count` | Screens in the active version |
| `created_at`, `updated_at`, `latest_version_at` | Deck and latest version timestamps |
| `favorite` | Shared persisted boolean |
| `last_viewed_at` | Shared persisted visit timestamp, or null |

The legacy deck endpoints retain `version` for the active version and their
original fields. They additionally expose `updated_at`, `group_id`,
`sort_order`, `favorite`, and `last_viewed_at`. `GET /api/v1/decks` remains
newest-first; use the library endpoint for server organization order.

Names are not identifiers and need not be unique. Rename endpoints trim and
validate names at 1–120 characters. The client must render names and version
notes as text or escape them before inserting HTML.

## Organization and metadata writes

| Operation | Endpoint | JSON |
| --- | --- | --- |
| Create group | `POST /api/v1/groups` | `{name}` |
| Rename group | `PATCH /api/v1/groups/{id}` | `{name}` |
| Reorder group | `POST /api/v1/groups/{id}/move` | `{index}` |
| Delete group, retain its decks | `DELETE /api/v1/groups/{id}` | None |
| Rename deck | `PATCH /api/v1/decks/{id}` | `{name}` |
| Move one deck | `POST /api/v1/decks/{id}/move` | `{group_id,index}` |
| Move multiple decks | `POST /api/v1/decks/move` | `{deck_ids,group_id,index}` |
| Set favorite | `PUT /api/v1/decks/{id}/favorite` | `{favorite}` |
| Set favorites together | `PUT /api/v1/decks/favorites` | `{deck_ids,favorite}` |
| Record visit | `POST /api/v1/decks/{id}/visit` | None |
| Edit version note | `PATCH /api/v1/decks/{id}/versions/{version_no}` | `{note}` |

Organization writes use the existing process lock plus an SQLite
`BEGIN IMMEDIATE` transaction. The database transaction is the atomicity
boundary, including across processes. Bulk selection endpoints accept 1–1000
unique nonempty IDs. They validate the entire selection before writing; a
missing member or target fails the complete request.

Bulk move order follows the supplied `deck_ids`. Its nonnegative integer
`index` addresses the destination after removing selected IDs; values beyond
the destination size append. Source and target sibling orders are normalized.
Repeated requests yield the same placement if no other write intervenes.
The legacy single-item/group move endpoints retain their clamped index behavior.
Deleting a group appends its ordered contents to Ungrouped, without deleting
decks or versions.

Favorite updates set an explicit boolean and never toggle. Recent visits are
recorded only by POST, not by read endpoints. Neither changes the active
version or deck upload timestamps. The browser's old `mg.library.pinned`
values are merged once through the bulk favorite API after successful
authorization; the server is authoritative afterward.

## Version content and notes

A deck is a stable container. Each ready version is immutable extracted source
under `storage/decks/{id}/versions/{n}/src/`. An upload claims a version number
and idempotency key, processes the ZIP, records screens and thumbnail state,
and activates the version after it becomes ready. A failed update leaves the
previous active pointer unchanged. Thumbnail failure is nonfatal.

`POST /api/v1/decks` accepts multipart `file`, `name`, `idempotency_key`, and
optional `note`. `POST /api/v1/decks/{id}/versions` accepts `file`,
`idempotency_key`, and optional `note`. Successful new uploads return 201;
replays return 200 and do not overwrite the saved note. The idempotency key is
globally unique across versions, so agents must use a new key for a new build.

Notes are plain text up to 4000 characters, with tabs/newlines allowed and
other control characters rejected. An empty string clears a note. Editing
metadata never rewrites source content or changes the active version.
Deck history includes `note`; screen history includes the note of its
`deck_version`.

A `screens.json` manifest declares `key`, `tag`, and `entry` per screen.
Without it, an upload has one implicit `main` screen at `index.html`.
Screen-specific history hashes each screen's entry HTML across ready versions,
collapses unchanged runs, and relabels changes 1..N. It does not hash the full
dependency tree. The underlying deck version remains available separately.

## Database migrations

| Migration | Purpose |
| --- | --- |
| `001_create_decks.sql` | Initial single-version deck table |
| `002_deck_versions.sql` | Move version data into `deck_versions`; create stable deck containers and backfill v1 |
| `003_deck_screens.sql` | Add nullable `deck_versions.screens_json` |
| `004_library_groups.sql` | Add groups, nullable membership, and ordered siblings |
| `005_library_metadata.sql` | Add favorite, recent visit, and version note metadata |

Migration 005 adds `decks.favorite INTEGER NOT NULL DEFAULT 0`,
`decks.last_viewed_at TEXT`, and
`deck_versions.note TEXT NOT NULL DEFAULT ''`. Existing identity, membership,
order, active pointers, archive files, and version rows are retained. The
migration ledger prevents repeated backfilling at startup. Back up the complete
`storage/` directory before upgrading a running instance.

All parameterized application SQL stays in `server/sql/queries/queries.json`.
The sqloader configuration key is `sqloder`; the installed package/import is
`sqloader`, pinned in `requirements.txt`.

## Serving and caching

- `/` serves the console and establishes its management session.
- `/static/` serves the console's split CSS, JavaScript, and locale catalog.
- `/v/{id}/` follows the active version.
- `/v/{id}/v{n}/` serves a specific ready version.
- `/thumbs/{id}.png` serves the active version's thumbnail. A query string may
  invalidate a browser cache entry but does not select another version.
- `/api/v1/decks/{id}/download?version=N` rebuilds a ready version as a ZIP.
- `/api/v1/decks/{id}/files?version=N` lists its source-relative files and sizes.

Console assets, API responses, active viewer paths, and thumbnails use
`Cache-Control: no-store`. Successful GETs under immutable versioned viewer
paths use `public, max-age=31536000, immutable`. Root viewer paths without a
trailing slash redirect with 307 so relative source assets resolve correctly.

## Verification

The automated suite covers additive migration, existing upload/download and
screen contracts, authorization, concurrent organization, atomic rollback,
bulk retry order, metadata persistence, and immutable viewer caching. Connected
browser tests exercise actual API-backed organization, favorites migration,
pagination, responsive layout, session renewal, and viewer races. Run:

```bash
python -m pip install -r requirements-dev.txt
python -m playwright install chromium
python -m pytest -q
```
