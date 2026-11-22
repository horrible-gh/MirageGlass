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

`groups` remains a flat array of stored groups, with a nullable `parent_id` on
every group. A `null` parent identifies a top-level group. Use `parent_id` to
build the hierarchy and `sort_order` to order siblings; do not infer hierarchy
from names or array positions. Each group's `decks` array contains only its
direct members, in server order. `ungrouped.decks` contains records with `group_id: null`;
Ungrouped does not have a group ID. Deck records include their ID, name,
membership, order, active/latest version, timestamps, favorite state, and
`last_viewed_at`. The legacy `GET /api/v1/decks` remains newest-first and is not
an organization-order endpoint.

Use IDs for subsequent writes. Group and deck names are editable display text,
and groups under different parents can have the same name. Resolve a group by its
full ancestry and ID before changing
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

## Create subgroups, move groups, and reorder siblings

```python
project = call("POST", "/api/v1/groups", json={"name": "Project"})
version = call("POST", "/api/v1/groups", json={
    "name": "v0.2",
    "parent_id": project["id"],
})
work = call("POST", "/api/v1/groups", json={
    "name": "Settings",
    "parent_id": version["id"],
})

# Reorder inside the current parent: omitting parent_id preserves it.
call("POST", f"/api/v1/groups/{work['id']}/move", json={"index": 0})

# Move the entire group and its descendants to another parent.
call("POST", f"/api/v1/groups/{work['id']}/move", json={
    "parent_id": project["id"],
    "index": 0,
})

# An explicit null moves a group to the top level.
call("POST", f"/api/v1/groups/{work['id']}/move", json={
    "parent_id": None,
    "index": 0,
})
```

Creation defaults to the top level and inserts the group at the front of its
sibling order. A group's move index addresses the destination's remaining
siblings, excluding the moving group, and is clamped to their range. Moving a
group leaves its ID, descendants, deck memberships, versions, and viewer URLs
unchanged. Validation and changes happen in one transaction. A group cannot be
moved beneath itself or any descendant, at any depth.

Names are trimmed and compared case-sensitively within the same parent. A new
group, changed name, or changed parent cannot introduce a matching sibling name.
Names under different parents may match. Legacy duplicate siblings are retained
on upgrade; their unchanged names and same-parent reorders remain valid.

For an aggregate view, walk the chosen group's descendants using `parent_id`
and concatenate their direct `decks` arrays. Each deck belongs to exactly one
group, so the flat library response never repeats a deck in its ancestors.
The console's **Include subgroups** option uses this approach; its default group
view remains direct members only.

## Rename and remove groups

```python
call("PATCH", f"/api/v1/groups/{group_id}", json={"name": "Landing pages"})
call("POST", f"/api/v1/groups/{group_id}/move", json={"index": 0})
call("PATCH", f"/api/v1/decks/{deck_id}", json={"name": "Landing / desktop"})
```

`DELETE /api/v1/groups/{id}` deletes only an empty group. Any child group or
assigned deck blocks deletion with `409 group_not_empty`, including decks that
are processing or failed and therefore absent from the ready library. Move the
contents first, then delete the empty group. This deliberately replaces the old
behavior that silently moved members to Ungrouped. Group deletion never deletes
decks or versions. `DELETE /api/v1/decks/{id}` still removes the explicitly named
deck and its version files.

## Upgrading and organizing existing decks

1. Before upgrading, stop writes and take a recoverable copy of the complete
   configured `MIRAGEGLASS_STORAGE` directory, including `mirageglass.db` and
   `decks/`. Stop the server while copying so database and archive files represent
   the same point in time. A library JSON export alone cannot restore source
   archives or version history.
2. Read and save `GET /api/v1/library` and, where needed, each deck's version
   listing to record existing IDs, names, memberships, and order. Verify actual
   counts from this instance rather than relying on an earlier report.
3. Test the upgrade on a copy. Migration 006 only adds the parent link and its
   indexes: all old groups remain top-level with their original IDs, names,
   ordering, and decks. Startup does not reorganize or delete existing content.
4. Validate nested creation, direct and aggregate browsing, group/deck moves,
   empty-only deletion, and existing active/version-specific links before
   applying the same code to the live instance.
5. Prepare a proposed project/version mapping with exact source and destination
   IDs and a reason for each move. Obtain agreement on the live movement scope,
   then try a small approved selection and re-read the library to verify it.
   Deletion requires its own explicit authorization.

Rollback before any new writes can restore the complete backup alongside the
previous code. Do not restore an old database over newer uploads without first
accounting for those changes. The hierarchy feature itself has no rollback,
cleanup, status inference, or automatic data-reclassification job.

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
new_version = updated["version"]
call("PATCH", f"/api/v1/decks/{deck_id}/versions/{new_version}", json={
    "note": "Adjusted spacing; reviewed at desktop and mobile widths.",
})
earliest_ready = next(item["version"] for item in versions["items"] if item["status"] == "ready")
call("POST", f"/api/v1/decks/{deck_id}/versions/{earliest_ready}/activate")
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
- `409`: a requested version is not ready, or an organization rule rejects the
  request. Structured `detail.code` values are `duplicate_group_name` for a
  sibling-name conflict, `group_cycle` for a self/descendant move, and
  `group_not_empty` for deleting a group with decks or children. A rejected
  organization write leaves memberships and ordering unchanged.
- A rejected archive remains a `400`; existing upload retry rules are unchanged.
- Group creation has no idempotency key. If its response is lost, re-read the
  library before retrying. A sibling-name conflict does not prove that the
  existing group was created by your request; confirm its identity and parent.
- A network failure after any write leaves the result uncertain. Read the
  server state before issuing a compensating operation.

Use JSON for organization writes and UTF-8 multipart for ZIP uploads. Never
embed the operation token into a shared viewer URL or public client-side code.
