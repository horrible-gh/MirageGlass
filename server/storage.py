"""Safe zip extraction and static path resolution.

Principle: validation and extraction finish inside storage/tmp, and only what
passes is moved to storage/decks/{id}/src. A failed upload leaves nothing
behind in the final location.
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Optional


class ZipRejected(Exception):
    """Input error to be returned as a 400."""


def _reject(msg: str) -> None:
    raise ZipRejected(msg)


def _open_zip(zip_path: Path) -> zipfile.ZipFile:
    """A non-zip upload is an input error, not a server error (400)."""
    try:
        return zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:
        raise ZipRejected("Not a zip file, or the archive is corrupted.")


def inspect_zip(zip_path: Path, settings) -> None:
    """Screen out zip bombs, path traversal and a missing entry point before extracting."""
    if zip_path.stat().st_size > settings.max_zip_bytes:
        _reject("Zip size limit exceeded.")

    with _open_zip(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > settings.max_entries:
            _reject("Too many entries in the zip.")

        total = 0
        has_index = False
        for info in infos:
            name = info.filename.replace("\\", "/")

            if name.startswith("/") or ".." in Path(name).parts:
                _reject(f"Disallowed path in the zip: {info.filename}")
            # Symlink (top 4 bits of the external unix permission bits are 0xA)
            if (info.external_attr >> 28) == 0xA:
                _reject(f"Symbolic links are not allowed: {info.filename}")

            if info.is_dir():
                continue

            if info.file_size > settings.max_entry_bytes:
                _reject(f"Single file size limit exceeded: {info.filename}")
            total += info.file_size
            if total > settings.max_total_bytes:
                _reject("Total extracted size limit exceeded.")

            if name == "index.html":
                has_index = True

        if not has_index:
            _reject("No index.html at the zip root.")


def extract_zip(zip_path: Path, dest: Path) -> None:
    """Only pass zips that cleared inspect_zip. Paths are checked once more after extraction."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_root = dest.resolve()

    with _open_zip(zip_path) as zf:
        for info in zf.infolist():
            target = (dest / info.filename).resolve()
            if dest_root not in target.parents and target != dest_root:
                _reject(f"Extraction path escapes the storage directory: {info.filename}")
            zf.extract(info, dest)


def deck_dir(settings, deck_id: str) -> Path:
    return settings.decks_dir / deck_id


def deck_src_dir(settings, deck_id: str) -> Path:
    return deck_dir(settings, deck_id) / "src"


def thumb_path(settings, deck_id: str) -> Path:
    return deck_dir(settings, deck_id) / "thumb.png"


def resolve_asset(settings, deck_id: str, rel_path: str) -> Optional[Path]:
    """Path resolution for static serving. Returns None if it escapes src."""
    src_root = deck_src_dir(settings, deck_id).resolve()
    if not src_root.is_dir():
        return None

    candidate = (src_root / (rel_path or "index.html")).resolve()
    if candidate != src_root and src_root not in candidate.parents:
        return None
    if candidate.is_dir():
        candidate = candidate / "index.html"
    if not candidate.is_file():
        return None
    return candidate


def publish(tmp_src: Path, final_dir: Path) -> None:
    """Move a fully validated tmp directory to its final location."""
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.move(str(tmp_src), str(final_dir))


def remove_deck(settings, deck_id: str) -> None:
    target = deck_dir(settings, deck_id)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
