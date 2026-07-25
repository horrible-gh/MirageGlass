# MirageGlass v0

One zip upload = one deck version. The entry point is a single `index.html` at the zip root.
A deck keeps a stable viewer link while uploads add version history; listings are newest first,
and the API is the primary way to register a deck or add a version.

The full loop — register, list, view, thumbnail, delete — has been exercised against a
running server. The versions in `requirements.txt` are the combination that passed it.

## Layout

```
server/
  main.py            FastAPI app, database.init() in the lifespan
  config.py          Settings (environment variables) + database_init() wiring
  api.py             deck/version APIs, help, active and version-scoped viewers, thumbnails
  repository.py      decks table access (sqloader fetch_one/fetch_all/execute)
  storage.py         zip validation, extraction, static path resolution
  thumbnail.py       Playwright capture (registration still succeeds if it fails)
  sql/
    queries/queries.json            decks.* query keys
    migrations/sqlite/001_create_decks.sql
web/index.html       deck/version rail + active viewer and side-by-side comparison
requirements.txt
.env.example
```

## Running

```bash
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env      # set MIRAGEGLASS_TOKEN
uvicorn server.main:app --port 8100
```

`storage/` is created automatically on first start, and migrations are applied through
`auto_migration: true`.
`server/config.py` reads `.env` at start-up via `load_dotenv()`. Environment variables that
are already exported take precedence over `.env` (`override=False`).

## Looking up the usage guide

```
GET /api/v1/help     # no auth. Returns the endpoint list, the procedure and the gotchas as JSON
```

An automation agent can call this instead of hunting for the README. `/docs` (OpenAPI) is the
schema; `/api/v1/help` is the "do X, then Y" part.

## Registering a deck

Multipart fields **must be sent as UTF-8.** Use `requests` so the result does not depend on the
console code page.

```python
import requests

r = requests.post(
    "http://127.0.0.1:8100/api/v1/decks",
    headers={"Authorization": f"Bearer {token}"},
    files={"file": ("deck.zip", open("deck.zip", "rb"), "application/zip")},
    data={"name": "Main landing deck", "idempotency_key": "build-2026-07-22-01"},
)
r.raise_for_status()
print(r.status_code, r.json()["id"])   # 201 new / 200 idempotent replay
```

When checking things with curl, either keep the name ASCII-only or make sure the console is
really running in UTF-8 first.

```bash
curl -X POST http://127.0.0.1:8100/api/v1/decks \
  -H "Authorization: Bearer $MIRAGEGLASS_TOKEN" \
  -F "file=@deck.zip" \
  -F "name=landing-draft" \
  -F "idempotency_key=build-2026-07-22-01"
```

Firing the same `idempotency_key` again does not create a second deck; it returns the existing
one with **200** (a new registration is 201). One exception: if the previous registration under
that key ended as `failed`, the leftovers are cleaned up and the upload is processed again —
you have to be able to fix the zip and retry with the same key.

Response codes: `201` new / `200` idempotent replay / `400` bad input or rejected zip /
`401` token / `404` not found / `500` processing failure.

## Editing a deck

Download a ready version, edit the extracted files, then upload it as the next version of the
same deck. Downloads and file manifests are read operations and do not require authentication.

```bash
curl -o deck.zip "http://127.0.0.1:8100/api/v1/decks/$DECK_ID/download?version=1"
curl "http://127.0.0.1:8100/api/v1/decks/$DECK_ID/files?version=1"
curl -X POST "http://127.0.0.1:8100/api/v1/decks/$DECK_ID/versions" \
  -H "Authorization: Bearer $MIRAGEGLASS_TOKEN" \
  -F "file=@deck.zip" \
  -F "idempotency_key=build-2026-07-22-02"
```

The downloaded archive has `index.html` at its root and can be posted back without changing its
layout. It is rebuilt from `storage/decks/{id}/versions/{n}/src`, so it is not a byte-for-byte
copy of the original upload. Each version can be viewed without changing the active link at
`/v/{id}/v{n}/`; the web UI uses these paths for side-by-side comparison.

## Storage layout

```
storage/
  mirageglass.db
  decks/{id}/versions/{n}/src/index.html   <- exactly as extracted from the zip
  decks/{id}/versions/{n}/thumb.png
  tmp/                                      <- validation and extraction workspace
```

## Notes

- The sqloader config key really is spelled `sqloder` (verbatim from the library, not a typo here).
- The install and import name is `sqloader`; the upstream repository is `py_sqloader`.
- Your system Python may already have 0.2.15 installed. In 0.2.15 the `sqloader.init` import
  fails, so create a separate virtual environment and install from `requirements.txt`.
- `/v/{id}` and `/v/{id}/v{n}` answer with 307 redirects to their trailing-slash forms. Without
  the trailing slash, relative paths inside the deck break.
- If the console code page is not UTF-8 (cp932, cp949, ...), `curl.exe -F` with a non-ASCII value
  in `name` arrives mangled or as `?`. That is a client-side problem, not a server one. Automated
  registration scripts have to send UTF-8.
