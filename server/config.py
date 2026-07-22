"""MirageGlass settings plus sqloader 0.2.17 initialization.

database_init(config) is called once at startup. The spelling of the config key
``sqloder`` is taken verbatim from sqloader/init.py — it is not a typo on our
side; the library reads it as ``db_service.get('sqloder')``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqloader.init import database_init

BASE_DIR = Path(__file__).resolve().parent          # server/
PROJECT_ROOT = BASE_DIR.parent

SERVICE_SQLOADER = str(BASE_DIR / "sql" / "queries")
MIGRATION_PATH = str(BASE_DIR / "sql" / "migrations" / "sqlite")

# As long as the README tells you to `cp .env.example .env`, the server has to
# actually read that file. override=False keeps already-exported environment
# variables winning, so injecting only a token into a throwaway instance still
# works the way it used to.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Tuned through environment variables only. v0 has no config file."""

    upload_token: str
    storage_dir: Path
    db_path: Path
    max_zip_bytes: int
    max_entries: int
    max_total_bytes: int
    max_entry_bytes: int
    capture_width: int
    capture_height: int
    capture_timeout_ms: int

    @property
    def decks_dir(self) -> Path:
        return self.storage_dir / "decks"

    @property
    def tmp_dir(self) -> Path:
        return self.storage_dir / "tmp"

    @classmethod
    def from_env(cls) -> "Settings":
        storage_dir = Path(
            os.environ.get("MIRAGEGLASS_STORAGE", str(PROJECT_ROOT / "storage"))
        ).resolve()
        return cls(
            upload_token=os.environ.get("MIRAGEGLASS_TOKEN", ""),
            storage_dir=storage_dir,
            db_path=storage_dir / "mirageglass.db",
            max_zip_bytes=_env_int("MIRAGEGLASS_MAX_ZIP_BYTES", 200 * 1024 * 1024),
            max_entries=_env_int("MIRAGEGLASS_MAX_ENTRIES", 5000),
            max_total_bytes=_env_int("MIRAGEGLASS_MAX_TOTAL_BYTES", 500 * 1024 * 1024),
            max_entry_bytes=_env_int("MIRAGEGLASS_MAX_ENTRY_BYTES", 100 * 1024 * 1024),
            capture_width=_env_int("MIRAGEGLASS_CAPTURE_WIDTH", 1280),
            capture_height=_env_int("MIRAGEGLASS_CAPTURE_HEIGHT", 800),
            capture_timeout_ms=_env_int("MIRAGEGLASS_CAPTURE_TIMEOUT_MS", 20000),
        )

    def ensure_dirs(self) -> None:
        self.decks_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)


class Database:
    """Reuses the three instances returned by database_init() for the process lifetime."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db_instance = None
        self.sqloader = None
        self.migrator = None

    def build_config(self) -> dict:
        return {
            "type": "sqlite3",
            "sqlite3": {
                "db_name": str(self.settings.db_path),
            },
            "service": {
                # Mind the spelling: the key sqloader reads is 'sqloder'.
                "sqloder": SERVICE_SQLOADER,
            },
            "migration": {
                "migration_path": MIGRATION_PATH,
                "auto_migration": True,
            },
        }

    def init(self):
        self.settings.ensure_dirs()
        self.db_instance, self.sqloader, self.migrator = database_init(self.build_config())
        return self.db_instance, self.sqloader, self.migrator


settings = Settings.from_env()
database = Database(settings)
