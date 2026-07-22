"""Regression: registration used to die with a 500 because .env was never loaded.

As long as the README tells you to `cp .env.example .env`, the server has to read
that file for real. At the same time override=False means an already-exported
environment variable beats .env — the "inject only a token into a throwaway
instance" workflow must not break.

server.config calls load_dotenv() exactly once at import time, so these two
directions cannot be checked inside a single process. Each one spawns a fresh
Python process instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"

# The subprocess reads the configuration exactly the way the real server does and
# prints nothing but the token.
PROBE = "import json;from server.config import settings;print(json.dumps({'token': settings.upload_token}))"


@pytest.fixture(scope="module")
def env_token() -> str:
    """The MIRAGEGLASS_TOKEN value in .env.

    .env is gitignored, so a freshly checked out tree does not have one. If it is
    missing we create it from .env.example and remove it afterwards. **If one
    already exists we never touch it** — a test must not overwrite the token of a
    running instance.
    """
    from dotenv import dotenv_values

    created = False
    if not ENV_FILE.exists():
        ENV_FILE.write_bytes(ENV_EXAMPLE.read_bytes())
        created = True
    try:
        token = (dotenv_values(ENV_FILE) or {}).get("MIRAGEGLASS_TOKEN")
        if not token:
            pytest.skip("No MIRAGEGLASS_TOKEN in .env, so loading cannot be judged.")
        yield token
    finally:
        if created:
            ENV_FILE.unlink(missing_ok=True)


def _probe(extra_env: dict[str, str] | None = None) -> str:
    env = os.environ.copy()
    # A value exported by the parent test process would leak in and make the
    # assertion meaningless.
    env.pop("MIRAGEGLASS_TOKEN", None)
    env.pop("MIRAGEGLASS_STORAGE", None)
    env.update(extra_env or {})
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])["token"]


def test_env_file_is_loaded_when_nothing_is_exported(env_token):
    """Before the fix this was an empty string, which is why registration 500'd."""
    assert _probe() == env_token


def test_exported_variable_beats_env_file(env_token):
    """override=False. The existing "inject only a token" workflow has to keep working."""
    assert _probe({"MIRAGEGLASS_TOKEN": "exported-wins"}) == "exported-wins"


def test_dotenv_is_pinned_in_requirements():
    """The import is not wrapped in try/except, so a missing dependency kills start-up outright."""
    text = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "python-dotenv==0.21.0" in text


def test_config_does_not_override_the_process_environment():
    """If the load_dotenv call ever flips to override=True, one of the two tests above quietly stops meaning anything."""
    source = (PROJECT_ROOT / "server" / "config.py").read_text(encoding="utf-8")
    assert "override=False" in source
