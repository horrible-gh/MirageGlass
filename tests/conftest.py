"""Shared test setup.

server.config runs load_dotenv() and Settings.from_env() exactly once, at import
time. That means the storage and token environment variables have to be set
**before the server package is imported** — which is why every server import in
this file lives inside a fixture. Do not hoist them to the top of the module.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

TEST_TOKEN = "test-token-0009"

# 1x1 transparent PNG, used by the fake that stands in for thumbnail capture.
_PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c63000100000500010d0a2db40000000049454e44ae"
    "426082"
)


@pytest.fixture(scope="session")
def app_client(tmp_path_factory):
    """A TestClient started against isolated storage with the token exported.

    Thumbnail capture is swapped for a fake: the tests must not depend on whether
    Playwright/Chromium happens to be installed, and capture is unrelated to what
    these tests actually cover (help, .env, README).
    """
    storage_dir = tmp_path_factory.mktemp("mg-storage")
    os.environ["MIRAGEGLASS_STORAGE"] = str(storage_dir)
    os.environ["MIRAGEGLASS_TOKEN"] = TEST_TOKEN

    from fastapi.testclient import TestClient

    from server import thumbnail
    from server.main import app

    def _fake_capture(index_html: Path, out_png: Path, settings) -> bool:
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_png.write_bytes(_PNG_1PX)
        return True

    real_capture = thumbnail.capture
    thumbnail.capture = _fake_capture
    try:
        # It has to run inside the with block so the lifespan fires and
        # database.init() (i.e. the migrations) is applied.
        with TestClient(app) as client:
            yield client
    finally:
        thumbnail.capture = real_capture


@pytest.fixture
def auth_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


def make_zip(entries: dict[str, str]) -> bytes:
    """{"index.html": "<html>...", ...} -> zip bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


@pytest.fixture
def deck_zip() -> bytes:
    return make_zip(
        {
            "index.html": (
                "<!doctype html><html><head><meta charset='utf-8'>"
                "<link rel='stylesheet' href='assets/style.css'></head>"
                "<body><h1>deck</h1></body></html>"
            ),
            "assets/style.css": "body{margin:0}",
        }
    )
