"""Every published source file has to stay English.

The repository is public, so comments, docstrings, API strings and UI text are all
English. This test is the lock: it walks the tracked text files and fails on any
Hangul it finds. Without it, one localized comment slips back in and the next
person has to redo the whole sweep.

Runtime artifacts (storage/, .env) are gitignored and therefore out of scope.
"""

from __future__ import annotations

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Hangul syllables, compatibility jamo and conjoining jamo.
# Written as escapes on purpose, so this file stays pure ASCII and does not
# trip its own check.
HANGUL = re.compile("[\uac00-\ud7a3\u3130-\u318f\u1100-\u11ff]")

SUFFIXES = {".py", ".md", ".html", ".css", ".js", ".json", ".sql", ".txt", ".example"}
IGNORED_DIRS = {"storage", ".venv", ".git", "__pycache__", "node_modules"}
IGNORED_NAMES = {".env"}
FLOWGATE_DOC = re.compile(r"^[A-Z]{1,3}\d{4}\.md$")


def _tracked_text_files() -> list[Path]:
    found = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_ROOT)
        if IGNORED_DIRS & set(rel.parts):
            continue
        if len(rel.parts) == 1 and FLOWGATE_DOC.fullmatch(rel.name):
            continue
        if rel.name in IGNORED_NAMES:
            continue
        if path.suffix not in SUFFIXES and rel.name != ".gitignore":
            continue
        found.append(path)
    return found


def test_no_hangul_in_tracked_sources():
    offenders = []
    for path in _tracked_text_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if HANGUL.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, "Localized text found in published sources:\n" + "\n".join(offenders)


def test_the_scan_actually_reaches_the_sources():
    """A guard against the filter above silently matching nothing."""
    names = {p.name for p in _tracked_text_files()}
    assert {"README.md", "api.py", "index.html"} <= names
