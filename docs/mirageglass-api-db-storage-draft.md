# API, database and storage contract (draft)

This document is not a design spec — it is an index of what is already nailed down in the code.
The real draft lives in the source tree; what is written here is only the set of things that get
expensive if they drift.

## Endpoints — `server/api.py`

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/api/v1/decks` | Bearer | multipart: `file`, `name`, `idempotency_key` -> 201 new / 200 replay |
| GET | `/api/v1/decks` | none | `ready` only, `created_at DESC` |
| GET | `/api/v1/decks/{id}` | none | single deck |
| GET | `/api/v1/decks/{id}/download` | none | ready deck source as a re-registerable zip; 404 missing / 409 not ready |
| GET | `/api/v1/decks/{id}/files` | none | sorted source-relative file paths and sizes; 404 missing / 409 not ready |
| DELETE | `/api/v1/decks/{id}` | Bearer | files first, database second -> 204 |
| GET | `/v/{id}/{path}` | none | static serving of `storage/decks/{id}/src` |
| GET | `/thumbs/{id}.png` | none | captured png |

Public response fields: `id`, `name`, `status`, `viewer_url`, `thumb_url`, `created_at`.
`thumb_url` is `null` when the capture failed, and the UI falls back to a numbered card.

Response codes: 201 new / 200 idempotent replay / 400 bad input or rejected zip / 401 token /
404 not found / 409 deck not ready / 500 processing failure.

## Table — `server/sql/migrations/sqlite/001_create_decks.sql`

`decks(id, name, group_key, status, has_thumb, idempotency_key, error_message, created_at, updated_at)`

- `status`: `processing | ready | failed` (CHECK constraint)
- `group_key`: unused in v0. Reserved for a later "same name keeps the same link" feature
- `idempotency_key`: UNIQUE. Blocks duplicate automated registrations at the database level
- Timestamps are UTC ISO8601 strings
- Index: `(status, created_at DESC)`

## Query keys — `server/sql/queries/queries.json`

`decks.create` / `decks.get_by_id` / `decks.get_by_idempotency_key` /
`decks.list_ready_latest` / `decks.list_all_latest` / `decks.update_status` /
`decks.update_thumb` / `decks.delete`

Calls take the form `sq.fetch_one("queries", "decks.get_by_id", (id,))`
(sqloader 0.2.17 `SQLoader.execute/fetch_one/fetch_all(file, query_name, params)`).

## Upload processing order — `create_deck()`

1. Look up the `idempotency_key`
   - `ready`/`processing` -> return the existing record with **200** (no new deck)
   - `failed` -> clean up the files and the row, then proceed under the same key.
     If a failed registration held the key forever, fixing the zip would not help
2. Issue a random 8-character id, claim a `processing` row (a UNIQUE conflict yields to the concurrent request)
3. Save the zip into `storage/tmp/{id}_xxx/` -> `inspect_zip()` -> `extract_zip()`
4. Confirm `index.html` at the root
5. Playwright capture of the top 1280x800 screenful — **registration continues even if it fails**
6. Move into `storage/decks/{id}/` -> record `has_thumb` -> `ready`
7. On failure, delete the whole tmp directory and mark `failed`. Nothing is left in the final location

## Gotchas

- The sqloader config key is spelled **`sqloder`** (`sqloader/init.py` reads it with that spelling)
- The install and import name is `sqloader`; the upstream repository is `py_sqloader`
- A system Python may carry 0.2.15, which has no `sqloader.init`. The runtime has to be on **0.2.17**
- A non-zip upload raises `zipfile.BadZipFile`. Unless it is caught and turned into `ZipRejected` (400), it becomes a 500
- A deck `name` is a string chosen by the uploader. It must be escaped wherever `web/index.html` writes it through `innerHTML`
- `/v/{id}` redirects to `/v/{id}/`. Without the slash, relative assets inside the deck break

## Verification record

Confirmed by actually running against `sqloader 0.2.17`:

- `database_init()` -> SQLite initialised, `001_create_decks.sql` applied automatically
- `decks.create / get_by_id / get_by_idempotency_key / list_ready_latest / update_* / delete` all executed successfully
- A zip without a root `index.html` rejected, a zip containing `../` rejected, a well-formed zip accepted
- Static path resolution: `index.html` and nested assets served correctly, `../../mirageglass.db` escape blocked

The Playwright capture and the FastAPI routing were verified separately, once a browser was installed.
