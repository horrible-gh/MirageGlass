"""MirageGlass v0 entry point.

Run with: uvicorn server.main:app --reload --port 8100
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import storage
from .api import api_router, public_router
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


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    page: Path = WEB_DIR / "index.html"
    return FileResponse(page)
