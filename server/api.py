"""Deck create/list/delete, deck version management, and the public serving routes.

Asymmetric auth: creating a deck, adding a version, rolling back and deleting all
require a Bearer token; listing, single fetch, version listing, downloads, the
deck viewer and thumbnails are open. The endpoints are plain `def` - FastAPI runs
them in a threadpool, so the synchronous sqloader and the Playwright sync API can
be used as-is.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from starlette.background import BackgroundTask

from . import storage, thumbnail
from .config import database, settings
from .repository import DeckRepository, new_deck_id, to_public

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1", tags=["decks"])
public_router = APIRouter(tags=["public"])

# How many times to recompute MAX+1 when a concurrent upload grabs the same
# version number before giving up.
_VERSION_CLAIM_ATTEMPTS = 5


def get_repo() -> DeckRepository:
    if database.sqloader is None:
        raise HTTPException(status_code=500, detail="database not initialized")
    return DeckRepository(database.sqloader)


def require_token(authorization: str = Header(default="")) -> None:
    if not settings.upload_token:
        raise HTTPException(status_code=500, detail="MIRAGEGLASS_TOKEN is not configured.")
    expected = f"Bearer {settings.upload_token}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


# --- version helpers ------------------------------------------------------

def _clear_failed_version(repo: DeckRepository, row: dict) -> None:
    """Drop a failed version's files and row so its idempotency key can be reused."""
    storage.remove_version(settings, row["deck_id"], row["version_no"])
    repo.delete_version(row["deck_id"], row["version_no"])


def _claim_next_version(repo: DeckRepository, deck_id: str, idempotency_key: str):
    """Insert a fresh processing version row and return its number.

    ``(deck_id, version_no)`` is the primary key, so a concurrent uploader that
    grabbed the same number makes this insert fail; recompute MAX+1 and retry. A
    clash on the globally unique idempotency key instead means another request
    already owns this exact upload - return that row so the caller replays it.

    Returns ``(version_no, None)`` on a fresh claim or ``(None, replay_row)`` when
    a non-failed version already holds the key.
    """
    for _ in range(_VERSION_CLAIM_ATTEMPTS):
        next_no = (repo.max_version(deck_id) or 0) + 1
        try:
            repo.create_version(deck_id, next_no, idempotency_key)
            return next_no, None
        except Exception:
            replay = repo.get_version_by_idempotency_key(idempotency_key)
            if replay and replay["status"] != "failed":
                return None, replay
            # Otherwise a number race: someone else took next_no. Retry.
    raise HTTPException(status_code=500, detail="Failed to allocate a version number.")


def _process_upload_into_version(
    file: UploadFile, repo: DeckRepository, deck_id: str, version_no: int
) -> None:
    """Validate/extract/capture an upload and publish it as this deck version.

    On success the version row is marked ready and the deck's active pointer is
    moved to it. A rejected zip becomes a 400 and any other error a 500; in both
    cases the version stays failed and the deck's active version is left untouched
    - a failed upload must never disturb what the shared link is already serving.
    """
    settings.ensure_dirs()
    work_dir = Path(
        tempfile.mkdtemp(prefix=f"{deck_id}_v{version_no}_", dir=str(settings.tmp_dir))
    )
    try:
        # 1) Store into tmp -> validate -> extract
        zip_path = work_dir / "upload.zip"
        with zip_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)

        storage.inspect_zip(zip_path, settings)
        src_dir = work_dir / "src"
        storage.extract_zip(zip_path, src_dir)
        zip_path.unlink(missing_ok=True)

        index_html = src_dir / "index.html"
        if not index_html.is_file():
            raise storage.ZipRejected("No index.html at the zip root.")

        # 2) A failed capture must not block the registration.
        has_thumb = thumbnail.capture(index_html, work_dir / "thumb.png", settings)

        # 3) Move to the final location, mark ready, then switch the active pointer.
        #    Ordering matters: the active version must never reference a non-ready
        #    row, so the status flips to ready before set_active runs.
        storage.publish(work_dir, storage.version_dir(settings, deck_id, version_no))
        repo.set_version_thumb(deck_id, version_no, has_thumb)
        repo.set_version_status(deck_id, version_no, "ready")
        repo.set_active(deck_id, version_no)
    except storage.ZipRejected as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        repo.set_version_status(deck_id, version_no, "failed", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("Upload processing failed: %s v%s", deck_id, version_no)
        repo.set_version_status(deck_id, version_no, "failed", str(e))
        raise HTTPException(status_code=500, detail="Failed to process the upload.")


def _resolve_read_version(repo: DeckRepository, deck_id: str, version: Optional[int]) -> int:
    """Pick the version a read targets. version=None means the active one.

    404 if the deck or a named version is missing; 409 if the target is not ready
    (an unreadied active deck, or a named non-ready version).
    """
    deck = repo.get(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="not found")
    if version is None:
        active = deck.get("active_version")
        if active is None or deck.get("active_status") != "ready":
            raise HTTPException(status_code=409, detail="deck is not ready")
        return active
    row = repo.get_version(deck_id, version)
    if not row:
        raise HTTPException(status_code=404, detail="version not found")
    if row["status"] != "ready":
        raise HTTPException(status_code=409, detail="version is not ready")
    return version


def _active_version(repo: DeckRepository, deck_id: str) -> Optional[int]:
    """The version the public routes serve, or None if the deck has no ready one."""
    deck = repo.get(deck_id)
    if not deck:
        return None
    active = deck.get("active_version")
    if active is None or deck.get("active_status") != "ready":
        return None
    return active


# --- deck + version write endpoints ---------------------------------------

@api_router.post("/decks", status_code=201)
def create_deck(
    response: Response,
    file: UploadFile = File(...),
    name: str = Form(...),
    idempotency_key: str = Form(...),
    _: None = Depends(require_token),
    repo: DeckRepository = Depends(get_repo),
):
    # 1) Idempotency key first - an automated uploader firing the same request
    #    twice must not create a second deck.
    existing = repo.get_version_by_idempotency_key(idempotency_key)
    if existing:
        if existing["status"] != "failed":
            # A replay did not create anything, so it is 200, not 201.
            response.status_code = 200
            return to_public(repo.get(existing["deck_id"]))
        # A failed registration must not hold the key forever. Clear the version,
        # and if that empties its deck (the usual case for a failed create) drop
        # the empty container too, then start over.
        prev_deck = existing["deck_id"]
        _clear_failed_version(repo, existing)
        if repo.max_version(prev_deck) is None:
            storage.remove_deck(settings, prev_deck)
            repo.delete_deck(prev_deck)

    # 2) A brand new deck is a container plus its version 1.
    deck_id = new_deck_id()
    repo.create_deck(deck_id, name)
    try:
        repo.create_version(deck_id, 1, idempotency_key)
    except Exception:
        # A concurrent create won the key. Our empty container is useless.
        repo.delete_deck(deck_id)
        replay = repo.get_version_by_idempotency_key(idempotency_key)
        if replay:
            response.status_code = 200
            return to_public(repo.get(replay["deck_id"]))
        raise

    _process_upload_into_version(file, repo, deck_id, 1)
    return to_public(repo.get(deck_id))


@api_router.post("/decks/{deck_id}/versions", status_code=201)
def add_version(
    deck_id: str,
    response: Response,
    file: UploadFile = File(...),
    idempotency_key: str = Form(...),
    _: None = Depends(require_token),
    repo: DeckRepository = Depends(get_repo),
):
    # name is intentionally not accepted here: the deck name is stable and a
    # version upload only carries file + idempotency_key. Any extra field is
    # ignored by FastAPI.
    if not repo.get(deck_id):
        raise HTTPException(status_code=404, detail="not found")

    existing = repo.get_version_by_idempotency_key(idempotency_key)
    if existing:
        if existing["status"] != "failed":
            response.status_code = 200
            return to_public(repo.get(existing["deck_id"]))
        _clear_failed_version(repo, existing)

    version_no, replay = _claim_next_version(repo, deck_id, idempotency_key)
    if replay is not None:
        response.status_code = 200
        return to_public(repo.get(replay["deck_id"]))

    _process_upload_into_version(file, repo, deck_id, version_no)
    return to_public(repo.get(deck_id))


@api_router.post("/decks/{deck_id}/versions/{version_no}/activate")
def activate_version(
    deck_id: str,
    version_no: int,
    _: None = Depends(require_token),
    repo: DeckRepository = Depends(get_repo),
):
    if not repo.get(deck_id):
        raise HTTPException(status_code=404, detail="not found")
    row = repo.get_version(deck_id, version_no)
    if not row:
        raise HTTPException(status_code=404, detail="version not found")
    if row["status"] != "ready":
        raise HTTPException(status_code=409, detail="version is not ready")
    # Rollback only moves the active pointer; history stays intact.
    repo.set_active(deck_id, version_no)
    return to_public(repo.get(deck_id))


# --- deck + version read endpoints ----------------------------------------

@api_router.get("/decks")
def list_decks(repo: DeckRepository = Depends(get_repo)):
    return {"items": [to_public(row) for row in repo.list_ready()]}


@api_router.get("/decks/{deck_id}")
def get_deck(deck_id: str, repo: DeckRepository = Depends(get_repo)):
    row = repo.get(deck_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return to_public(row)


@api_router.get("/decks/{deck_id}/versions")
def list_versions(deck_id: str, repo: DeckRepository = Depends(get_repo)):
    deck = repo.get(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="not found")
    active = deck.get("active_version")
    items = [
        {
            "version": row["version_no"],
            "status": row["status"],
            "has_thumb": bool(row["has_thumb"]),
            "is_active": row["version_no"] == active,
            "created_at": row["created_at"],
        }
        for row in repo.list_versions(deck_id)
    ]
    return {
        "deck_id": deck_id,
        "active_version": active,
        "latest_version": deck.get("latest_version"),
        "items": items,
    }


@api_router.get("/decks/{deck_id}/download")
def download_deck(
    deck_id: str,
    version: Optional[int] = Query(default=None),
    repo: DeckRepository = Depends(get_repo),
):
    target = _resolve_read_version(repo, deck_id, version)
    settings.ensure_dirs()
    archive_file = tempfile.NamedTemporaryFile(
        prefix=f"{deck_id}_v{target}_", suffix=".zip", dir=settings.tmp_dir, delete=False
    )
    archive_path = Path(archive_file.name)
    archive_file.close()
    try:
        if not storage.build_version_archive(settings, deck_id, target, archive_path):
            raise HTTPException(status_code=404, detail="source files not found")
    except HTTPException:
        archive_path.unlink(missing_ok=True)
        raise
    except (FileNotFoundError, PermissionError):
        archive_path.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="source files not found")
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise

    # Active downloads keep the plain {id}.zip name; a named version is tagged.
    filename = f"{deck_id}.zip" if version is None else f"{deck_id}_v{target}.zip"
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@api_router.get("/decks/{deck_id}/files")
def list_deck_files(
    deck_id: str,
    version: Optional[int] = Query(default=None),
    repo: DeckRepository = Depends(get_repo),
):
    target = _resolve_read_version(repo, deck_id, version)
    items = storage.iter_version_files(settings, deck_id, target)
    if items is None:
        raise HTTPException(status_code=404, detail="source files not found")
    return {"items": [{"path": path, "size": size} for path, size in items]}


@api_router.delete("/decks/{deck_id}", status_code=204)
def delete_deck(
    deck_id: str,
    _: None = Depends(require_token),
    repo: DeckRepository = Depends(get_repo),
):
    if not repo.get(deck_id):
        raise HTTPException(status_code=404, detail="not found")
    # Files first, database second. The other order would orphan the files of a
    # row that is already gone. Every version lives under the deck dir, so one
    # removal clears them all.
    storage.remove_deck(settings, deck_id)
    repo.delete_deck(deck_id)
    return None


@public_router.get("/api/v1/help", tags=["help"])
def help_document():
    """The server hands out its own usage guide.

    /docs (OpenAPI) is a schema, so it never says "do X, then Y". This endpoint
    covers the procedure and the traps we actually hit. Keep it parseable enough
    that an automation agent can read it and call straight through. No auth here
    on purpose - requiring a token just to read the instructions makes no sense.
    """
    return {
        "service": "MirageGlass",
        "version": "0.1.0",
        "summary": "One deck keeps a stable link across many versions. Each upload adds a version to a deck; the viewer, thumbnail and downloads follow the deck's active version. The entry point is a single index.html at the zip root.",
        "auth": {
            "scheme": "Bearer",
            "header": "Authorization: Bearer <MIRAGEGLASS_TOKEN>",
            "required_for": [
                "POST /api/v1/decks",
                "POST /api/v1/decks/{deck_id}/versions",
                "POST /api/v1/decks/{deck_id}/versions/{version_no}/activate",
                "DELETE /api/v1/decks/{deck_id}",
            ],
            "note": "Read operations, including version listing, deck downloads and file manifests, need no auth.",
        },
        "workflow": [
            "1) POST /api/v1/decks to register a zip as a new deck = version 1 (multipart: file, name, idempotency_key)",
            "2) POST /api/v1/decks/{id}/versions to add a new version to the same deck (multipart: file, idempotency_key); the shared /v/{id}/ link keeps working and starts serving the new version",
            "3) GET /api/v1/decks/{id}/download?version=n to retrieve a version's source zip before editing it",
            "4) GET /api/v1/decks/{id}/versions to list every version and see which one is active",
            "5) POST /api/v1/decks/{id}/versions/{n}/activate to roll the active version back to an earlier one",
        ],
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/v1/decks",
                "auth": True,
                "description": "Register a zip as a new deck (its version 1). multipart/form-data.",
                "form_fields": {
                    "file": "The zip file. It must contain index.html at its root.",
                    "name": "Display name of the deck. Send it as UTF-8.",
                    "idempotency_key": "Replaying the same key returns the existing deck with 200 instead of creating a new one.",
                },
                "status_codes": {
                    "201": "Newly registered",
                    "200": "Replay of the same idempotency_key - existing deck returned",
                    "400": "Input error or zip rejected (no index.html, size/entry limits exceeded, ...)",
                    "401": "Token mismatch",
                    "500": "Processing failure",
                },
            },
            {
                "method": "POST",
                "path": "/api/v1/decks/{deck_id}/versions",
                "auth": True,
                "description": "Add a new version to an existing deck. multipart/form-data (file, idempotency_key). The deck name is not changed and any name field is ignored. 201 when a new version is created, 200 on a replay of the same idempotency_key, 404 if the deck does not exist. A rejected upload (400/500) leaves the active version untouched.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}/versions",
                "auth": False,
                "description": "List every version of a deck in ascending order, with active_version, latest_version and per-version status/has_thumb/is_active. 404 if the deck does not exist.",
            },
            {
                "method": "POST",
                "path": "/api/v1/decks/{deck_id}/versions/{version_no}/activate",
                "auth": True,
                "description": "Roll the deck's active version back to an earlier, ready version. 200 on success, 404 if the deck or version is missing, 409 if that version is not ready. History is preserved.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks",
                "auth": False,
                "description": "List of registered decks, newest first, shaped as {\"items\": [...]}.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}",
                "auth": False,
                "description": "A single deck, including its active version and latest_version. 404 if it does not exist.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}/download",
                "auth": False,
                "description": "Download the active deck source as a re-registerable zip. Add ?version=n to download a specific version (read only). Filename is {id}.zip for the active version and {id}_v{n}.zip for a named one. Returns 404 if missing and 409 if not ready.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}/files",
                "auth": False,
                "description": "List source-relative file paths and sizes for the active version. Add ?version=n for a specific version (read only). Returns 404 if missing and 409 if not ready.",
            },
            {
                "method": "DELETE",
                "path": "/api/v1/decks/{deck_id}",
                "auth": True,
                "description": "Delete a deck and all of its versions. 204 on success.",
            },
            {
                "method": "GET",
                "path": "/v/{deck_id}/",
                "auth": False,
                "description": "Deck viewer (static serving) of the active version. Without the trailing slash you get a 307 redirect.",
            },
            {
                "method": "GET",
                "path": "/thumbs/{deck_id}.png",
                "auth": False,
                "description": "Thumbnail PNG of the active version. 404 for decks whose active capture is missing.",
            },
            {
                "method": "GET",
                "path": "/api/v1/help",
                "auth": False,
                "description": "This document.",
            },
            {
                "method": "GET",
                "path": "/healthz",
                "auth": False,
                "description": "Health check. {\"ok\": true}.",
            },
        ],
        "examples": {
            "create_deck_python": (
                "import requests\n"
                "r = requests.post(\n"
                "    'http://127.0.0.1:8100/api/v1/decks',\n"
                "    headers={'Authorization': f'Bearer {token}'},\n"
                "    files={'file': ('deck.zip', open('deck.zip', 'rb'), 'application/zip')},\n"
                "    data={'name': 'Main landing deck', 'idempotency_key': 'build-2026-07-22-01'},\n"
                ")\n"
                "deck_id = r.json()['id']"
            ),
            "add_version_python": (
                "import requests\n"
                "r = requests.post(\n"
                "    f'http://127.0.0.1:8100/api/v1/decks/{deck_id}/versions',\n"
                "    headers={'Authorization': f'Bearer {token}'},\n"
                "    files={'file': ('deck-v2.zip', open('deck-v2.zip', 'rb'), 'application/zip')},\n"
                "    data={'idempotency_key': 'build-2026-07-22-02'},\n"
                ")\n"
                "# same viewer_url, r.json()['version'] is now 2"
            ),
            "viewer_url": "http://127.0.0.1:8100/v/{deck_id}/",
            "thumb_url": "http://127.0.0.1:8100/thumbs/{deck_id}.png",
        },
        "gotchas": [
            "/v/{id} without the trailing slash is answered with a 307 redirect. Relative paths inside the deck can break, so call /v/{id}/ from the start.",
            "Multipart fields must be sent as UTF-8, and index.html must be at the zip root. If the console code page is not UTF-8 (cp932, cp949, ...), curl.exe -F with a non-ASCII name stores mojibake or '?'.",
            "A failed thumbnail capture still leaves the registration successful (ready). Check the thumb field to see whether one exists.",
            "A registration that ended as failed is retried from scratch under the same idempotency_key: the leftovers are cleaned up first.",
            "A downloaded zip is rebuilt from the stored source rather than copied byte-for-byte from the upload. Re-registering it through POST /api/v1/decks creates a new deck id and viewer URL; use POST .../versions to keep the same link.",
            "The deck name does not change when you upload a new version. name is a deck attribute; a version upload sends only file and idempotency_key, and any name field is ignored.",
            "/v/{id}/ and /thumbs/{id}.png always serve the deck's active version. There is no version parameter on those paths - the shared link is stable by design.",
            "A failed version upload leaves the active version untouched: the shared link keeps serving whatever was ready before.",
            "?version=n is read only. It works on /download and /files; the only way to change the active version is POST .../versions/{n}/activate.",
        ],
    }


@public_router.get("/v/{deck_id}")
def view_root_redirect(deck_id: str):
    # Always end with a slash so relative asset paths keep working.
    return RedirectResponse(url=f"/v/{deck_id}/")


@public_router.get("/v/{deck_id}/{asset_path:path}")
def view_asset(
    deck_id: str,
    asset_path: str = "",
    repo: DeckRepository = Depends(get_repo),
):
    active = _active_version(repo, deck_id)
    if active is None:
        raise HTTPException(status_code=404, detail="not found")
    target = storage.resolve_asset(settings, deck_id, active, asset_path)
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


@public_router.get("/thumbs/{deck_id}.png")
def view_thumb(deck_id: str, repo: DeckRepository = Depends(get_repo)):
    active = _active_version(repo, deck_id)
    if active is None:
        raise HTTPException(status_code=404, detail="not found")
    target = storage.version_thumb_path(settings, deck_id, active)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target, media_type="image/png")
