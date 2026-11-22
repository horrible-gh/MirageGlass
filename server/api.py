"""Deck create/list/delete, deck version management, and the public serving routes.

Write auth accepts either the configured operation Bearer token or the first-party
console session; listing, single fetch, version listing, downloads, the deck viewer
and thumbnails are open. The endpoints are plain `def` - FastAPI runs
them in a threadpool, so the synchronous sqloader and the Playwright sync API can
be used as-is.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field, StrictBool, StrictInt, StrictStr, field_validator
from starlette.background import BackgroundTask

from . import storage, thumbnail
from .auth import require_management_write
from .config import database, settings
from .repository import (
    DeckRepository,
    OrganizationConflict,
    OrganizationNotFound,
    new_deck_id,
    new_group_id,
    to_public,
)

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/api/v1", tags=["decks"])
public_router = APIRouter(tags=["public"])

# How many times to recompute MAX+1 when a concurrent upload grabs the same
# version number before giving up.
_VERSION_CLAIM_ATTEMPTS = 5
_NAME_MAX_LENGTH = 120
_NOTE_MAX_LENGTH = 4000
_DISALLOWED_NAME_CATEGORIES = {"Cc", "Cf", "Cs"}


class NamePayload(BaseModel):
    name: str


class GroupCreatePayload(NamePayload):
    parent_id: Optional[StrictStr] = Field(default=None, min_length=1)


class DeckMovePayload(BaseModel):
    group_id: Optional[str] = None
    index: StrictInt


class GroupMovePayload(BaseModel):
    index: StrictInt
    parent_id: Optional[StrictStr] = Field(default=None, min_length=1)


class DeckSelectionPayload(BaseModel):
    deck_ids: list[StrictStr] = Field(min_length=1, max_length=1000)

    @field_validator("deck_ids")
    @classmethod
    def unique_nonempty_ids(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("deck_ids must contain unique, nonempty IDs.")
        return values


class BulkDeckMovePayload(DeckSelectionPayload):
    group_id: Optional[StrictStr] = None
    index: StrictInt = Field(ge=0)


class FavoritePayload(BaseModel):
    favorite: StrictBool


class BulkFavoritePayload(DeckSelectionPayload, FavoritePayload):
    pass


class VersionNotePayload(BaseModel):
    note: StrictStr = Field(max_length=_NOTE_MAX_LENGTH)


def get_repo() -> DeckRepository:
    if database.sqloader is None or database.db_instance is None:
        raise HTTPException(status_code=500, detail="database not initialized")
    return DeckRepository(database.sqloader, database.db_instance)



def _normalize_name(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_name", "message": "Name cannot be empty."},
        )
    if len(normalized) > _NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_name",
                "message": f"Name cannot exceed {_NAME_MAX_LENGTH} characters.",
            },
        )
    if any(unicodedata.category(char) in _DISALLOWED_NAME_CATEGORIES for char in normalized):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_name",
                "message": "Name cannot contain control or formatting characters.",
            },
        )
    return normalized


def _organization_not_found(error: OrganizationNotFound) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": f"{error.entity} not found"},
    )


def _organization_conflict(error: OrganizationConflict) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": str(error)},
    )


def _normalize_note(value: str) -> str:
    if len(value) > _NOTE_MAX_LENGTH or any(
        unicodedata.category(char) == "Cs"
        or (unicodedata.category(char) == "Cc" and char not in "\t\n\r")
        for char in value
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_note",
                "message": "A note can contain up to 4000 characters and no control characters except whitespace.",
            },
        )
    return value


def _version_to_public(row: dict, active_version: Optional[int]) -> dict:
    return {
        "version": row["version_no"],
        "status": row["status"],
        "has_thumb": bool(row["has_thumb"]),
        "is_active": row["version_no"] == active_version,
        "created_at": row["created_at"],
        "note": row.get("note", ""),
    }


# --- version helpers ------------------------------------------------------

def _clear_failed_version(repo: DeckRepository, row: dict) -> None:
    """Drop a failed version's files and row so its idempotency key can be reused."""
    storage.remove_version(settings, row["deck_id"], row["version_no"])
    repo.delete_version(row["deck_id"], row["version_no"])


def _claim_next_version(repo: DeckRepository, deck_id: str, idempotency_key: str, note: str = ""):
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
            repo.create_version(deck_id, next_no, idempotency_key, note)
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

        screens = storage.inspect_zip(zip_path, settings)
        src_dir = work_dir / "src"
        storage.extract_zip(zip_path, src_dir)
        zip_path.unlink(missing_ok=True)

        # A manifest zip is thumbnailed from its first declared screen; a plain
        # zip keeps the root index.html it has always used.
        entry_html = src_dir / (screens[0]["entry"] if screens else "index.html")
        if not entry_html.is_file():
            raise storage.ZipRejected("No index.html at the zip root.")

        # 2) A failed capture must not block the registration.
        has_thumb = thumbnail.capture(entry_html, work_dir / "thumb.png", settings)

        # 3) Move to the final location, mark ready, then switch the active pointer.
        #    Ordering matters: the active version must never reference a non-ready
        #    row, so the status flips to ready before set_active runs.
        storage.publish(work_dir, storage.version_dir(settings, deck_id, version_no))
        repo.set_version_thumb(deck_id, version_no, has_thumb)
        if screens is not None:
            repo.set_version_screens(deck_id, version_no, json.dumps(screens, ensure_ascii=False))
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


def _version_screens(row: dict, deck_name: str) -> list[dict]:
    """A version's screens, from its manifest or the single implicit "main" screen.

    A version with no screens.json (every version uploaded before multi-screen
    support, and any plain single-index.html zip since) is one screen named after
    the deck.
    """
    raw = row.get("screens_json")
    if not raw:
        return [{"key": "main", "tag": deck_name, "entry": "index.html"}]
    return json.loads(raw)


def _screen_viewer_path(entry: str) -> str:
    """The /v/{deck_id}/ sub-path that serves a screen's entry file."""
    if entry == "index.html":
        return ""
    if entry.endswith("/index.html"):
        return entry[: -len("index.html")]
    return entry


def _view_root(repo: DeckRepository, deck_id: str, version_no: int, root_url: str):
    """Serve a version root, or redirect a rootless manifest to its first screen."""
    target = storage.resolve_asset(settings, deck_id, version_no, "")
    if target is not None:
        return FileResponse(target)

    row = repo.get_version(deck_id, version_no)
    if row and row.get("screens_json"):
        first_screen = _version_screens(row, "")[0]
        viewer_path = _screen_viewer_path(first_screen["entry"])
        return RedirectResponse(url=f"{root_url}{viewer_path}")

    raise HTTPException(status_code=404, detail="not found")


# --- deck + version write endpoints ---------------------------------------

@api_router.post("/decks", status_code=201)
def create_deck(
    response: Response,
    file: UploadFile = File(...),
    name: str = Form(...),
    idempotency_key: str = Form(...),
    note: str = Form(default=""),
    _: str = Depends(require_management_write),
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
        _normalize_note(note)
        # A failed registration must not hold the key forever. Clear the version,
        # and if that empties its deck (the usual case for a failed create) drop
        # the empty container too, then start over.
        prev_deck = existing["deck_id"]
        _clear_failed_version(repo, existing)
        if repo.max_version(prev_deck) is None:
            storage.remove_deck(settings, prev_deck)
            repo.delete_deck(prev_deck)

    # 2) A brand new deck is a container plus its version 1.
    note = _normalize_note(note)
    deck_id = new_deck_id()
    repo.create_deck(deck_id, name)
    try:
        repo.create_version(deck_id, 1, idempotency_key, note)
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
    note: str = Form(default=""),
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    # name is intentionally not accepted here: the deck name is stable and a
    # version upload carries file, idempotency_key and an optional note. A name is
    # ignored by FastAPI.
    if not repo.get(deck_id):
        raise HTTPException(status_code=404, detail="not found")

    existing = repo.get_version_by_idempotency_key(idempotency_key)
    if existing:
        if existing["status"] != "failed":
            response.status_code = 200
            return to_public(repo.get(existing["deck_id"]))
        _normalize_note(note)
        _clear_failed_version(repo, existing)

    note = _normalize_note(note)
    version_no, replay = _claim_next_version(repo, deck_id, idempotency_key, note)
    if replay is not None:
        response.status_code = 200
        return to_public(repo.get(replay["deck_id"]))

    _process_upload_into_version(file, repo, deck_id, version_no)
    return to_public(repo.get(deck_id))


@api_router.patch("/decks/{deck_id}/versions/{version_no}")
def update_version_note(
    deck_id: str,
    version_no: int,
    payload: VersionNotePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    deck = repo.get(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="not found")
    try:
        row = repo.set_version_note(deck_id, version_no, _normalize_note(payload.note))
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    return _version_to_public(row, deck.get("active_version"))


@api_router.post("/decks/{deck_id}/versions/{version_no}/activate")
def activate_version(
    deck_id: str,
    version_no: int,
    _: str = Depends(require_management_write),
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


# --- library organization endpoints --------------------------------------

@api_router.get("/library")
def get_library(repo: DeckRepository = Depends(get_repo)):
    return repo.list_library()


@api_router.post("/decks/move")
def move_decks(
    payload: BulkDeckMovePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        return repo.move_decks(payload.deck_ids, payload.group_id, payload.index)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)


@api_router.put("/decks/favorites")
def set_favorites(
    payload: BulkFavoritePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        return repo.set_favorites(payload.deck_ids, payload.favorite)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)


@api_router.put("/decks/{deck_id}/favorite")
def set_favorite(
    deck_id: str,
    payload: FavoritePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        repo.set_favorites([deck_id], payload.favorite)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    return repo.get_library_deck(deck_id) or to_public(repo.get(deck_id))


@api_router.post("/decks/{deck_id}/visit")
def record_visit(
    deck_id: str,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        return repo.record_visit(deck_id)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)


@api_router.post("/groups", status_code=201)
def create_group(
    payload: GroupCreatePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        return repo.create_group(new_group_id(), _normalize_name(payload.name), payload.parent_id)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    except OrganizationConflict as error:
        raise _organization_conflict(error)


@api_router.patch("/groups/{group_id}")
def rename_group(
    group_id: str,
    payload: NamePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        return repo.rename_group(group_id, _normalize_name(payload.name))
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    except OrganizationConflict as error:
        raise _organization_conflict(error)


@api_router.delete("/groups/{group_id}", status_code=204)
def delete_group(
    group_id: str,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        repo.delete_group(group_id)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    except OrganizationConflict as error:
        raise _organization_conflict(error)
    return None


@api_router.post("/groups/{group_id}/move")
def move_group(
    group_id: str,
    payload: GroupMovePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        if "parent_id" in payload.model_fields_set:
            return repo.move_group(group_id, payload.index, payload.parent_id)
        return repo.move_group(group_id, payload.index)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    except OrganizationConflict as error:
        raise _organization_conflict(error)


@api_router.patch("/decks/{deck_id}")
def rename_deck(
    deck_id: str,
    payload: NamePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        row = repo.rename_deck(deck_id, _normalize_name(payload.name))
    except OrganizationNotFound as error:
        raise _organization_not_found(error)
    return repo.get_library_deck(deck_id) or to_public(row)


@api_router.post("/decks/{deck_id}/move")
def move_deck(
    deck_id: str,
    payload: DeckMovePayload,
    _: str = Depends(require_management_write),
    repo: DeckRepository = Depends(get_repo),
):
    try:
        return repo.move_deck(deck_id, payload.group_id, payload.index)
    except OrganizationNotFound as error:
        raise _organization_not_found(error)


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
    items = [_version_to_public(row, active) for row in repo.list_versions(deck_id)]
    return {
        "deck_id": deck_id,
        "active_version": active,
        "latest_version": deck.get("latest_version"),
        "items": items,
    }


@api_router.get("/decks/{deck_id}/screens")
def list_screens(
    deck_id: str,
    version: Optional[int] = Query(default=None),
    repo: DeckRepository = Depends(get_repo),
):
    deck = repo.get(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="not found")
    target = _resolve_read_version(repo, deck_id, version)
    row = repo.get_version(deck_id, target)
    screens = _version_screens(row, deck["name"])
    items = [
        {
            "key": screen["key"],
            "tag": screen["tag"],
            "viewer_path": _screen_viewer_path(screen["entry"]),
        }
        for screen in screens
    ]
    return {"deck_id": deck_id, "version": target, "items": items}


@api_router.get("/decks/{deck_id}/screens/{screen_key}/versions")
def list_screen_versions(
    deck_id: str, screen_key: str, repo: DeckRepository = Depends(get_repo)
):
    """A screen's own change history, derived from the deck's version chain.

    Only deck versions where this screen's content actually differs from its
    immediately preceding appearance are listed, and relabeled 1..N - so two
    screens in the same deck can show different counts even though both are
    backed by the same shared deck_versions rows.
    """
    deck = repo.get(deck_id)
    if not deck:
        raise HTTPException(status_code=404, detail="not found")
    active = deck.get("active_version")

    history = []
    last_hash = None
    for row in repo.list_versions(deck_id):
        if row["status"] != "ready":
            continue
        screen = next(
            (s for s in _version_screens(row, deck["name"]) if s["key"] == screen_key), None
        )
        if screen is None:
            continue
        entry_path = storage.version_src_dir(settings, deck_id, row["version_no"]) / screen["entry"]
        digest = storage.hash_file(entry_path)
        if digest is None or digest == last_hash:
            continue
        last_hash = digest
        history.append({
            "deck_version": row["version_no"],
            "created_at": row["created_at"],
            "note": row.get("note", ""),
        })

    if not history:
        raise HTTPException(status_code=404, detail="screen not found")

    # The active deck version may not itself be a change entry (it can leave this
    # particular screen untouched); "active" then falls on the latest change at or
    # before it, since that is the content this screen is still showing.
    active_index = None
    if active is not None:
        for index, entry in enumerate(history):
            if entry["deck_version"] <= active:
                active_index = index
            else:
                break

    items = [
        {
            "version": index + 1,
            "deck_version": entry["deck_version"],
            "created_at": entry["created_at"],
            "note": entry["note"],
            "is_active": index == active_index,
        }
        for index, entry in enumerate(history)
    ]
    return {
        "deck_id": deck_id,
        "screen_key": screen_key,
        "active_version": items[active_index]["version"] if active_index is not None else None,
        "latest_version": items[-1]["version"],
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
    _: str = Depends(require_management_write),
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
                "POST /api/v1/groups",
                "PATCH /api/v1/groups/{group_id}",
                "DELETE /api/v1/groups/{group_id}",
                "POST /api/v1/groups/{group_id}/move",
                "PATCH /api/v1/decks/{deck_id}",
                "POST /api/v1/decks/{deck_id}/move",
                "POST /api/v1/decks/move",
                "PUT /api/v1/decks/{deck_id}/favorite",
                "PUT /api/v1/decks/favorites",
                "POST /api/v1/decks/{deck_id}/visit",
                "PATCH /api/v1/decks/{deck_id}/versions/{version_no}",
            ],
            "note": "Every management write accepts the configured Bearer token. The first-party browser may instead use a same-origin HttpOnly console session when enabled. Read operations, including library, versions, downloads and file manifests, need no auth.",
        },
        "workflow": [
            "1) POST /api/v1/decks to register a zip as a new deck = version 1 (multipart: file, name, idempotency_key)",
            "2) POST /api/v1/decks/{id}/versions to add a new version to the same deck (multipart: file, idempotency_key); the shared /v/{id}/ link keeps working and starts serving the new version",
            "3) GET /api/v1/decks/{id}/download?version=n to retrieve a version's source zip before editing it",
            "4) GET /api/v1/decks/{id}/versions to list every version and see which one is active",
            "5) GET /v/{id}/v{n}/ to render a specific ready version without changing the active shared link",
            "6) POST /api/v1/decks/{id}/versions/{n}/activate to roll the active version back to an earlier one",
            "7) GET /api/v1/library, create groups with an optional parent_id or move an intact group subtree, then POST /api/v1/decks/move to organize several decks atomically. Use group_id=null to remove deck membership.",
            "8) PUT a favorite value, POST a visit, or PATCH a version note to share those changes between the console and API agents. Notes may also be supplied with the original multipart upload.",
        ],
        "library_model": {
            "groups": "A flat array of real group records with stable IDs, parent_id (null at the top level), sibling-local sort_order, and directly owned ordered decks. Follow parent_id to build the tree or a full path; there is no fixed depth limit. Each deck occurs once. Existing group IDs, links and memberships survive the hierarchy migration.",
            "group_names": "Names are trimmed, 1..120 characters, and compared exactly (case-sensitive). Creating, renaming or moving into an identical sibling name is rejected with 409 duplicate_group_name, including at the top level. The same name under different parents is allowed. Pre-existing duplicate siblings are preserved; they may be reordered or renamed to their unchanged name, but no new conflict can be introduced.",
            "group_move": "POST /groups/{id}/move accepts {index:integer,parent_id?:string|null}. Omit parent_id to reorder within the current parent; explicit null moves to the top level. The index addresses destination siblings after removing the moving group, clamped to 0..N. Subgroups and deck memberships retain their IDs and links. Self/descendant moves return 409 group_cycle, missing parents return 404, and a failed mutation changes nothing.",
            "group_delete": "Only an empty group may be deleted. Any direct deck (including processing or failed decks) or child group causes 409 group_not_empty. Move those records first; deletion never ungroups or deletes decks and never deletes children.",
            "group_scope": "Each group's decks list is direct membership only. The console defaults to that scope and offers an explicit Include subgroups option; API clients can follow parent_id and combine those direct lists without duplicate memberships.",
            "ungrouped": "A separate section for decks with group_id=null, not a group record.",
            "favorite": "A shared server-owned boolean on each deck. PUT sets a value rather than toggling it, making retries safe.",
            "last_viewed_at": "A shared UTC timestamp or null. Only POST .../visit records a visit; ordinary GETs do not change it. Sort descending for recents.",
            "note": "Plain-text version metadata, up to 4000 characters. Editing it never changes immutable version files or the active pointer.",
            "bulk_move": "deck_ids is an ordered list of 1..1000 unique IDs. Validate every ID and target before writing; either the whole move commits or nothing changes. index is zero-based in the target AFTER removing selected IDs and clamps to the target end. Retrying the same payload preserves the resulting order if no concurrent write intervenes. There is no revision precondition; refresh and reconcile concurrent changes before retrying a write whose outcome is uncertain.",
        },
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
                    "note": "Optional version-1 note, up to 4000 characters. Defaults to an empty string. An idempotent replay does not replace the saved note.",
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
                "description": "Add a new version to an existing deck. multipart/form-data (file, idempotency_key, optional note). The deck name is not changed and any name field is ignored. 201 when a new version is created, 200 on a replay of the same idempotency_key, 404 if the deck does not exist. A rejected upload (400/500) leaves the active version untouched.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}/versions",
                "auth": False,
                "description": "List every version of a deck in ascending order, with active_version, latest_version and per-version status/has_thumb/is_active/note. 404 if the deck does not exist.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}/screens",
                "auth": False,
                "description": "List the screens of a version (default: active) as key/tag/viewer_path. A version with no screens.json manifest reports a single implicit screen. Add ?version=n for a specific version. 404 missing, 409 not ready.",
            },
            {
                "method": "GET",
                "path": "/api/v1/decks/{deck_id}/screens/{screen_key}/versions",
                "auth": False,
                "description": "A single screen's own change history, derived from the deck's version chain and relabeled 1..N - so two screens in the same deck can report different counts. Each entry includes the note of its backing deck_version. 404 if the screen never appears in a ready version.",
            },
            {
                "method": "POST",
                "path": "/api/v1/decks/{deck_id}/versions/{version_no}/activate",
                "auth": True,
                "description": "Roll the deck's active version back to an earlier, ready version. 200 on success, 404 if the deck or version is missing, 409 if that version is not ready. History is preserved.",
            },
            {
                "method": "GET",
                "path": "/api/v1/library",
                "auth": False,
                "description": "Read a flat groups array with parent_id, sibling-local sort_order and directly owned decks, plus a separate ungrouped section. Each deck includes group_id, sort_order, favorite, last_viewed_at, version counts and screen_count. Follow parent_id to build the tree or explicitly include descendants; decks are never copied into ancestor groups.",
            },
            {
                "method": "POST",
                "path": "/api/v1/groups",
                "auth": True,
                "description": "Create a group at the front of its sibling order. JSON: {name:string,parent_id?:string|null}; omitted/null parent means top level. Returns the new group with a stable id and parent_id (201). Names are trimmed, 1..120 characters. Exact sibling-name collisions return 409 duplicate_group_name, a missing parent returns 404, and invalid input returns 422.",
            },
            {
                "method": "PATCH",
                "path": "/api/v1/groups/{group_id}",
                "auth": True,
                "description": "Rename one group. JSON: {name:string}. Identity, parent, members and order stay unchanged. Returns the group (200); 404 missing, 409 duplicate_group_name for a new exact sibling-name conflict, 422 invalid name. An unchanged-name request remains valid for preserved legacy duplicates.",
            },
            {
                "method": "DELETE",
                "path": "/api/v1/groups/{group_id}",
                "auth": True,
                "description": "Delete an empty group only. A direct deck of any status or a child group blocks deletion with 409 group_not_empty. Move those records first. No deck, membership, child group or version is deleted or moved. 204 success, 404 missing.",
            },
            {
                "method": "POST",
                "path": "/api/v1/groups/{group_id}/move",
                "auth": True,
                "description": "Reorder a group or move its intact subtree. JSON: {index:integer,parent_id?:string|null}. Omitted parent preserves its current parent; explicit null moves to top level. Index addresses destination siblings without the moving group, clamped to 0..N. Returns the moved group (200); 404 missing group/parent, 409 group_cycle for self/descendant moves, 409 duplicate_group_name for an exact sibling-name conflict. Validation and both sibling orders change in one transaction.",
            },
            {
                "method": "PATCH",
                "path": "/api/v1/decks/{deck_id}",
                "auth": True,
                "description": "Rename a deck. JSON: {name:string}. Stable links, versions, membership and order stay unchanged. Returns the deck (200); 404 missing, 422 invalid name.",
            },
            {
                "method": "POST",
                "path": "/api/v1/decks/{deck_id}/move",
                "auth": True,
                "description": "Move or reorder one deck atomically. JSON: {group_id:string|null,index:integer}. Use null for ungrouped. Index addresses target siblings without the moving deck and is clamped to 0..N. Returns the deck (200). The existing single-deck API remains supported.",
            },
            {
                "method": "POST",
                "path": "/api/v1/decks/move",
                "auth": True,
                "description": "Move or reorder an entire selection in one transaction. JSON: {deck_ids:[string,...],group_id:string|null,index:nonnegative integer}. Caller order is preserved; index addresses the target after removing selected IDs. Returns the complete library (200). 404 missing deck/group, 422 empty/duplicate selection or invalid input. A failure changes nothing.",
            },
            {
                "method": "PUT",
                "path": "/api/v1/decks/{deck_id}/favorite",
                "auth": True,
                "description": "Set a shared favorite value. JSON: {favorite:boolean}. Returns the updated deck (200); 404 missing, 422 invalid value. Repeating the request is safe.",
            },
            {
                "method": "PUT",
                "path": "/api/v1/decks/favorites",
                "auth": True,
                "description": "Set favorites for 1..1000 unique decks atomically. JSON: {deck_ids:[string,...],favorite:boolean}. Returns the complete library (200); 404 missing deck, 422 invalid selection/value. Useful for importing browser pins without clearing favorites on other decks.",
            },
            {
                "method": "POST",
                "path": "/api/v1/decks/{deck_id}/visit",
                "auth": True,
                "description": "Record an explicit visit and return the updated deck with last_viewed_at (UTC). A repeated request records the newer visit time. GET requests never record visits. 200 success, 404 missing.",
            },
            {
                "method": "PATCH",
                "path": "/api/v1/decks/{deck_id}/versions/{version_no}",
                "auth": True,
                "description": "Update a version note. JSON: {note:string}, up to 4000 characters; empty string clears it. Returns the version entry (200). Does not modify stored content, content hashes, URLs or active_version. 404 missing deck/version, 422 invalid note.",
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
                "description": "A single deck, including its active version, latest_version, group_id, sort_order, favorite and last_viewed_at. 404 if it does not exist.",
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
                "path": "/v/{deck_id}/v{version_no}/",
                "auth": False,
                "description": "Read-only viewer for a specific ready version. Without the trailing slash you get a 307 redirect. Returns 404 if the deck or version is missing and 409 if the version is not ready.",
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
            "The deck name does not change when you upload a new version. name is a deck attribute; a version upload sends file, idempotency_key and an optional note, and any name field is ignored.",
            "/v/{id}/ and /thumbs/{id}.png always serve the deck's active version, so the shared links stay stable. Use the separate /v/{id}/v{n}/ path to view a specific ready version without changing the active pointer.",
            "A failed version upload leaves the active version untouched: the shared link keeps serving whatever was ready before.",
            "?version=n is read only. It works on /download and /files; the only way to change the active version is POST .../versions/{n}/activate.",
            "A zip with a root screens.json registers multiple screens instead of a single index.html; each entry needs key/tag/entry and the entry file must exist in the zip. Without screens.json, a deck behaves exactly as before (one implicit screen).",
            "Ungrouped is not a group ID. Send JSON null to remove group membership; group names are not unique identifiers.",
            "Groups use parent_id=null at the top level, while ungrouped decks use group_id=null. Omitting parent_id in a group move only reorders current siblings; send null explicitly to move a nested group to the top level.",
            "Deleting a nonempty group now returns 409 group_not_empty. Move its decks and child groups explicitly before deleting the empty group; deletion never changes deck membership.",
            "Use one bulk move for a selection instead of independent moves: validation and ordering updates commit together. The insertion index is measured after the selected decks have been removed from the target.",
            "Favorites and recency are now shared server metadata. A browser may import its old local pins once, but API writes and later console changes use the same durable values.",
        ],
    }


@public_router.get("/v/{deck_id}")
def view_root_redirect(deck_id: str):
    # Always end with a slash so relative asset paths keep working.
    return RedirectResponse(url=f"/v/{deck_id}/")


@public_router.get("/v/{deck_id}/v{version_no}")
def view_version_root_redirect(deck_id: str, version_no: int):
    # Version-scoped viewers follow the same trailing-slash rule as stable links.
    return RedirectResponse(url=f"/v/{deck_id}/v{version_no}/")


@public_router.get("/v/{deck_id}/v{version_no}/{asset_path:path}")
def view_version_asset(
    deck_id: str,
    version_no: int,
    asset_path: str = "",
    repo: DeckRepository = Depends(get_repo),
):
    target_version = _resolve_read_version(repo, deck_id, version_no)
    if not asset_path:
        return _view_root(repo, deck_id, target_version, f"/v/{deck_id}/v{version_no}/")
    target = storage.resolve_asset(settings, deck_id, target_version, asset_path)
    if target is None:
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


@public_router.get("/v/{deck_id}/{asset_path:path}")
def view_asset(
    deck_id: str,
    asset_path: str = "",
    repo: DeckRepository = Depends(get_repo),
):
    active = _active_version(repo, deck_id)
    if active is None:
        raise HTTPException(status_code=404, detail="not found")
    if not asset_path:
        return _view_root(repo, deck_id, active, f"/v/{deck_id}/")
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
