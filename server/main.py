"""MirageGlass v0 entry point.

Run with: uvicorn server.main:app --reload --port 8100
"""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import storage
from .api import api_router, public_router
from .auth import issue_console_session
from .config import PROJECT_ROOT, database, settings

logging.basicConfig(level=logging.INFO)

WEB_DIR = PROJECT_ROOT / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Migrations are applied once here, via auto_migration=True.
    database.init()
    # Relocate any pre-version deck files (decks/{id}/src) into the versioned
    # layout (decks/{id}/versions/1/) alongside the DB migration. Idempotent.
    moved = storage.migrate_legacy_layout(settings)
    if moved:
        logging.getLogger(__name__).info("Migrated %s legacy deck(s) to version 1.", moved)
    yield


app = FastAPI(title="MirageGlass", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)
app.include_router(public_router)
app.mount("/static", StaticFiles(directory=WEB_DIR), name="console-assets")

# The SPA shell and moving "active" URLs must never be served stale. Versioned
# viewer URLs are immutable: the upload flow never changes a ready version's
# extracted source, so they can be cached safely and efficiently.
_VERSIONED_VIEWER = re.compile(r"^/v/[^/]+/v[1-9][0-9]*/")


@app.middleware("http")
async def cache_policy(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith(("/static/", "/api/v1/", "/v/", "/thumbs/")):
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable"
            if request.method == "GET" and response.status_code == 200
            and _VERSIONED_VIEWER.match(path)
            else "no-store"
        )
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index(request: Request):
    page: Path = WEB_DIR / "index.html"
    response = FileResponse(page)
    issue_console_session(request, response)
    return response
