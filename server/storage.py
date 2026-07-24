"""Safe zip extraction and versioned static path resolution.

Principle: validation and extraction finish inside storage/tmp, and only what
passes is moved to storage/decks/{id}/versions/{n}. A failed upload leaves
nothing behind in the final location. Each deck keeps one directory per version,
and the deck's active version decides which one the viewer, thumbnail and
downloads serve.
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


def versions_dir(settings, deck_id: str) -> Path:
    return deck_dir(settings, deck_id) / "versions"


def version_dir(settings, deck_id: str, version_no: int) -> Path:
    return versions_dir(settings, deck_id) / str(version_no)


def version_src_dir(settings, deck_id: str, version_no: int) -> Path:
    return version_dir(settings, deck_id, version_no) / "src"


def version_thumb_path(settings, deck_id: str, version_no: int) -> Path:
    return version_dir(settings, deck_id, version_no) / "thumb.png"


def resolve_asset(settings, deck_id: str, version_no: int, rel_path: str) -> Optional[Path]:
    """Path resolution for static serving. Returns None if it escapes the version src."""
    src_root = version_src_dir(settings, deck_id, version_no).resolve()
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


def iter_version_files(settings, deck_id: str, version_no: int) -> Optional[list[tuple[str, int]]]:
    """Return safe source-relative file paths and sizes for one version, in stable order."""
    src_root = version_src_dir(settings, deck_id, version_no).resolve()
    if not src_root.is_dir():
        return None

    files = []
    try:
        for candidate in src_root.rglob("*"):
            resolved = candidate.resolve()
            if resolved != src_root and src_root not in resolved.parents:
                continue
            if not resolved.is_file():
                continue
            files.append((candidate.relative_to(src_root).as_posix(), resolved.stat().st_size))
    except (FileNotFoundError, PermissionError):
        return None

    if not src_root.is_dir():
        return None
    return sorted(files, key=lambda item: item[0])


def build_version_archive(settings, deck_id: str, version_no: int, out_path: Path) -> bool:
    """Write a source-rooted zip for one version to disk without buffering it in memory."""
    files = iter_version_files(settings, deck_id, version_no)
    if files is None:
        return False

    src_root = version_src_dir(settings, deck_id, version_no).resolve()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel_path, _ in files:
            source = (src_root / rel_path).resolve()
            if source != src_root and src_root not in source.parents:
                continue
            zf.write(source, arcname=rel_path)
    return True


def publish(tmp_src: Path, final_dir: Path) -> None:
    """Move a fully validated tmp directory to its final location."""
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    if final_dir.exists():
        shutil.rmtree(final_dir)
    shutil.move(str(tmp_src), str(final_dir))


def remove_version(settings, deck_id: str, version_no: int) -> None:
    target = version_dir(settings, deck_id, version_no)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def remove_deck(settings, deck_id: str) -> None:
    target = deck_dir(settings, deck_id)
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)


def migrate_legacy_layout(settings) -> int:
    """Relocate pre-version decks (decks/{id}/src) into decks/{id}/versions/1/.

    The v0 layout stored one deck as decks/{id}/src (+ thumb.png). The version
    model serves from decks/{id}/versions/{n}/, so each legacy deck becomes its
    version 1. This runs at start-up alongside the DB migration and is idempotent:
    a deck that already has a versions/ directory is left untouched.
    """
    root = settings.decks_dir
    if not root.is_dir():
        return 0

    moved = 0
    for deck_path in root.iterdir():
        if not deck_path.is_dir():
            continue
        legacy_src = deck_path / "src"
        versions_path = deck_path / "versions"
        if versions_path.exists() or not legacy_src.is_dir():
            continue

        v1 = versions_path / "1"
        v1.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy_src), str(v1 / "src"))
        legacy_thumb = deck_path / "thumb.png"
        if legacy_thumb.is_file():
            shutil.move(str(legacy_thumb), str(v1 / "thumb.png"))
        moved += 1
    return moved
