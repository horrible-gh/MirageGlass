# Organizing and editing MirageGlass through the API

The browser console and an automated agent operate on the same server-owned
library. Registering or updating a deck still uses a ZIP upload and an
idempotency key. Organization never requires a browser session or a UI click.

## Connection and discovery

Read `GET /api/v1/help` for the task guide and `GET /openapi.json` for the
machine-readable schema. Use the existing operation token on every write:

```python
import os
import requests

api = requests.Session()
api.headers["Authorization"] = "Bearer " + os.environ["MIRAGEGLASS_TOKEN"]
base = "http://127.0.0.1:8100"

def call(method, path, **kwargs):
    response = api.request(method, base + path, timeout=60, **kwargs)
    response.raise_for_status()
    return response.json() if response.content else None

library = call("GET", "/api/v1/library")
decks = [deck for group in library["groups"] for deck in group["decks"]]
decks.extend(library["ungrouped"]["decks"])
```

`groups` contains stored groups in server order. Each group's `decks` array is
also in server order. `ungrouped.decks` contains records with `group_id: null`;
Ungrouped does not have a group ID. Deck records include their ID, name,
membership, order, active/latest version, timestamps, favorite state, and
`last_viewed_at`. The legacy `GET /api/v1/decks` remains newest-first and is not
an organization-order endpoint.

Use IDs for subsequent writes. Group and deck names are editable display text,
and two groups can have the same name. Resolve an ambiguous name before changing
anything. A successful library read is a current listing that may become stale
as another client writes. It is not a revision-locked database snapshot.

## Create a group and move a selection together

The following continuation creates a group and moves all currently ungrouped
decks whose names start with `Landing /` into it:

```python
selected = [
    deck["id"] for deck in library["ungrouped"]["decks"]
    if deck["name"].startswith("Landing /")
]
if selected:
    group = call("POST", "/api/v1/groups", json={"name": "Landing"})
    group_id = group["id"]
    library = call("POST", "/api/v1/decks/move", json={
        "deck_ids": selected,
        "group_id": group_id,
        "index": 0,
    })
    deck_id = selected[0]
```

The examples below use `group_id` and `deck_id` obtained in that successful
selection branch. If the selection is empty, choose existing IDs from the
library before running them. Skip a bulk favorites request for an empty list.

The bulk move accepts 1–1000 IDs and preserves the order of `deck_ids` supplied by the caller. The
server removes those IDs from their existing scopes, then inserts them into the
destination at `index`. The index is zero-based **after removing selected
decks from the destination**. This also supports reordering a selection within
one group. An index beyond the remaining destination size appends the set.

All selected decks, the destination, memberships, and sibling orders are
validated or updated inside one transaction. Empty or duplicate ID lists and
invalid index types are rejected. Missing resources fail the entire request.
The success response is the updated library, so the agent can check the outcome
without reconstructing order from individual write responses.

To move the same selection to Ungrouped, send `"group_id": null`. To move a
single deck, the existing `POST /api/v1/decks/{id}/move` remains available with
`{"group_id": ..., "index": ...}`.

Replaying the same bulk move with the same IDs, destination, and absolute index
keeps the same placement when no other client changes that scope. Re-read the
library before resolving a concurrent user's changes; this API does not offer
an optimistic revision precondition.

## Rename, reorder, and remove groups

```python
call("PATCH", f"/api/v1/groups/{group_id}", json={"name": "Landing pages"})
call("POST", f"/api/v1/groups/{group_id}/move", json={"index": 0})
call("PATCH", f"/api/v1/decks/{deck_id}", json={"name": "Landing / desktop"})
```

`DELETE /api/v1/groups/{id}` deletes only the group. Its decks are appended to
Ungrouped in their existing order. `DELETE /api/v1/decks/{id}` removes the deck
and its version files. Both remain explicit operations; a group cleanup does
not imply deleting its contents.

## Favorites and recent visits

Favorites are durable metadata on the server, shared by browsers and agents:

```python
call("PUT", f"/api/v1/decks/{deck_id}/favorite", json={"favorite": True})
call("PUT", "/api/v1/decks/favorites", json={
    "deck_ids": selected,
    "favorite": False,
})
call("POST", f"/api/v1/decks/{deck_id}/visit")
```

Use an explicit boolean rather than a toggle, so a retry does not reverse the
intended state. The batch favorite request changes only the named IDs and is
atomic. The single-deck writes return the updated deck; the batch returns the
library. Favoriting does not alter upload order or activate a version.

Opening a deck in the console records `last_viewed_at`; an agent can do the
same with `/visit`. Ordinary GET requests do not record visits. Sort non-null
`last_viewed_at` values descending to obtain the recent list. This history is
instance-wide, consistent with MirageGlass's existing shared-library model.

## Uploads, notes, comparison, and activation

Upload fields and idempotency behavior are unchanged. The optional UTF-8
multipart `note` field records a version note at registration or upload:

```python
with open("deck.zip", "rb") as source:
    updated = call("POST", f"/api/v1/decks/{deck_id}/versions",
        files={"file": ("deck.zip", source, "application/zip")},
        data={
            "idempotency_key": "landing-build-20261121-02",
            "note": "Adjusted spacing and simplified the navigation.",
        })

versions = call("GET", f"/api/v1/decks/{deck_id}/versions")
call("PATCH", f"/api/v1/decks/{deck_id}/versions/2", json={
    "note": "Adjusted spacing; reviewed at desktop and mobile widths.",
})
call("POST", f"/api/v1/decks/{deck_id}/versions/1/activate")
```

Version notes are editable plain text of up to 4000 characters. Editing a note does not rewrite source
files, create a version, or change the active pointer. An empty string clears
the note. Notes are returned in deck version history and in screen history;
screen history's `deck_version` identifies the underlying deck version to use
when editing its note.

To inspect or compare versions through the API:

1. Read `/api/v1/decks/{id}/versions` and select two ready version numbers.
2. Read `/api/v1/decks/{id}/screens?version=N` for each version's screen paths.
3. Read `/api/v1/decks/{id}/files?version=N` or download
   `/api/v1/decks/{id}/download?version=N` for each version.
4. Render `/v/{id}/vN/<viewer_path>` when visual comparison is needed.

The console's comparison uses those same immutable version URLs. There is no
separate mutable comparison object to synchronize. The stable `/v/{id}/` link
continues to follow the active version. Screen history counts changes to the
screen's entry HTML, not every dependency file.

## Errors and retries

- `401`: the operation token is absent or invalid. Agents should fix their
  Bearer header; they do not need console cookies.
- `404`: a deck, version, or group no longer exists. Refresh the library before
  deciding how to repair the selection. Bulk operations make no partial writes.
- `422`: the JSON shape, index, ID selection, name, or note is invalid.
- `409`: a requested version is not ready for the requested read/activation.
- A rejected archive remains a `400`; existing upload retry rules are unchanged.
- Group creation has no idempotency key. If its response is lost, re-read the
  library before retrying to avoid creating a duplicate group.
- A network failure after any write leaves the result uncertain. Read the
  server state before issuing a compensating operation.

Use JSON for organization writes and UTF-8 multipart for ZIP uploads. Never
embed the operation token into a shared viewer URL or public client-side code.
