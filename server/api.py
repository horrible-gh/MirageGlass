"""Create/list/delete API plus the public serving routes.

Asymmetric auth: create and delete require a Bearer token; list, single fetch,
deck viewing and thumbnails are open. The endpoints are plain `def` — FastAPI
runs them in a threadpool, so the synchronous sqloader and the Playwright sync
API can be used as-is.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse

from . import storage, thumbnail
from .config import database, settings
from .repository import DeckRepository, new_deck_id, to_public

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1", tags=["decks"])
public_router = APIRouter(tags=["public"])


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


@api_router.post("/decks", status_code=201)
def create_deck(
    response: Response,
    file: UploadFile = File(...),
    name: str = Form(...),
    idempotency_key: str = Form(...),
    _: None = Depends(require_token),
    repo: DeckRepository = Depends(get_repo),
):
    # 1) Idempotency key first — an automated uploader firing the same request
    #    twice must not create a second deck.
    existing = repo.get_by_idempotency_key(idempotency_key)
    if existing:
        if existing["status"] != "failed":
            # A replay did not create anything, so it is 200, not 201.
            response.status_code = 200
            return to_public(existing)
        # If a failed registration held the idempotency key forever, a fixed zip
        # could never be re-uploaded under the same key. Clear the leftovers and
        # start over with that key.
        storage.remove_deck(settings, existing["id"])
        repo.delete(existing["id"])

    # 2) Claim a processing row. A UNIQUE conflict here means a concurrent
    #    request won the race.
    deck_id = new_deck_id()
    try:
        repo.create_processing(deck_id, name, idempotency_key)
    except Exception:
        existing = repo.get_by_idempotency_key(idempotency_key)
        if existing:
            return to_public(existing)
        raise

    settings.ensure_dirs()
    work_dir = Path(tempfile.mkdtemp(prefix=f"{deck_id}_", dir=str(settings.tmp_dir)))
    try:
        # 3) Store into tmp -> validate -> extract
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

        # 4) A failed capture must not block the registration.
        has_thumb = thumbnail.capture(index_html, work_dir / "thumb.png", settings)

        # 5) Move to the final location and mark it ready
        storage.publish(work_dir, storage.deck_dir(settings, deck_id))
        repo.set_thumb(deck_id, has_thumb)
        repo.set_status(deck_id, "ready")

    except storage.ZipRejected as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        repo.set_status(deck_id, "failed", str(e))
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.exception("Upload processing failed: %s", deck_id)
        repo.set_status(deck_id, "failed", str(e))
        raise HTTPException(status_code=500, detail="Failed to process the upload.")

    return to_public(repo.get(deck_id))


@api_router.get("/decks")
def list_decks(repo: DeckRepository = Depends(get_repo)):
    return {"items": [to_public(row) for row in repo.list_ready()]}


@api_router.get("/decks/{deck_id}")
def get_deck(deck_id: str, repo: DeckRepository = Depends(get_repo)):
    row = repo.get(deck_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    return to_public(row)


@api_router.delete("/decks/{deck_id}", status_code=204)
def delete_deck(
    deck_id: str,
    _: None = Depends(require_token),
    repo: DeckRepository = Depends(get_repo),
):
    if not repo.get(deck_id):
        raise HTTPException(status_code=404, detail="not found")
    # Files first, database second. The other order would orphan the files of a
    # row that is already gone.
    storage.remove_deck(settings, deck_id)
    repo.delete(deck_id)
    return None


@public_router.get("/api/v1/help", tags=["help"])
def help_document():
    """The server hands out its own usage guide.

    /docs (OpenAPI) is a schema, so it never says "do X, then Y". This endpoint
    covers the procedure and the traps we actually hit. Keep it parseable enough
    that an automation agent can read it and call straight through. No auth here
    on purpose — requiring a token just to read the instructions makes no sense.
    """
    return {
        "service": "MirageGlass",
        "version": "0.1.0",
        "summary": "One zip = one deck. The entry point is a single index.html at the zip root. No versioning, no overwrite.",
        "auth": {
            "scheme": "Bearer",
            "header": "Authorization: Bearer <MIRAGEGLASS_TOKEN>",
            "required_for": ["POST /api/v1/decks", "DELETE /api/v1/decks/{deck_id}"],
            "note": "Read operations (GET /api/v1/decks, /v, /thumbs, /api/v1/help) need no auth.",
        },
        "workflow": [
            "1) POST /api/v1/decks to register a zip (multipart: file, name, idempotency_key)",
            "2) Build the viewer URL /v/{id}/ from the id in the response (the trailing slash is required)",
            "3) GET /api/v1/decks for the list, newest first",
            "4) DELETE /api/v1/decks/{id} once you no longer need it",
        ],
        "endpoints": [
            {
                "method": "POST",
                "path": "/api/v1/decks",
                "auth": True,
                "description": "Register a zip as one deck. multipart/form-data.",
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
                "method": "GET",
                "path": "/api/v1/decks",
                "auth": False,
                "description": "List of registered decks, newest first, shaped as {\"items\": [...]}.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}",
                "auth": False,
                "description": "A single deck. 404 if it does not exist.",
            },
            {
                "method": "DELETE",
                "path": "/api/v1/decks/{deck_id}",
                "auth": True,
                "description": "Delete a deck. 204 on success. There is no overwrite, so updating means delete then re-register.",
            },
            {
                "method": "GET",
                "path": "/v/{deck_id}/",
                "auth": False,
                "description": "Deck viewer (static serving). Without the trailing slash you get a 307 redirect.",
            },
            {
                "method": "GET",
                "path": "/thumbs/{deck_id}.png",
                "auth": False,
                "description": "Thumbnail PNG. 404 for decks whose capture failed.",
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
            "viewer_url": "http://127.0.0.1:8100/v/{deck_id}/",
            "thumb_url": "http://127.0.0.1:8100/thumbs/{deck_id}.png",
        },
        "gotchas": [
            "/v/{id} without the trailing slash is answered with a 307 redirect. Relative paths inside the deck can break, so call /v/{id}/ from the start.",
            "Multipart fields must be sent as UTF-8. If the console code page is not UTF-8 (cp932, cp949, ...), curl.exe -F with a non-ASCII name stores mojibake or '?'. The server writes the bytes it receives, unchanged.",
            "A failed thumbnail capture still leaves the registration successful (ready). Check the thumb field to see whether one exists.",
            "A zip without index.html at its root is rejected with 400. Do not nest it inside a subfolder.",
            "A registration that ended as failed is retried from scratch under the same idempotency_key: the leftovers are cleaned up first.",
        ],
    }


@public_router.get("/v/{deck_id}")
def view_root_redirect(deck_id: str):
    # Always end with a slash so relative asset paths keep working.
    return RedirectResponse(url=f"/v/{deck_id}/")


@public_router.get("/v/{deck_id}/{asset_path:path}")
def view_asset(deck_id: str, asset_path: str = ""):
    target = storage.resolve_asset(settings, deck_id, asset_path)
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


@public_router.get("/thumbs/{deck_id}.png")
def view_thumb(deck_id: str):
    target = storage.thumb_path(settings, deck_id)
    if not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target, media_type="image/png")
