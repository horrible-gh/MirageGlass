"""README guidance.

Mangled deck names were a client-side problem (the console code page), not a
server one, so the fix landed in the documentation rather than in code. Since the
document is the only fix, it is locked here so it cannot quietly regress.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def readme() -> str:
    return (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")


def test_primary_example_is_requests_based(readme):
    assert "requests.post(" in readme


def test_curl_example_uses_an_ascii_name(readme):
    """The curl example must never carry a non-ASCII name again - that was the repro."""
    assert '-F "name=landing-draft"' in readme
    for line in readme.splitlines():
        if '-F "name=' in line:
            assert line.isascii(), line


def test_readme_warns_about_multipart_encoding(readme):
    assert "UTF-8" in readme
    assert "cp932" in readme


def test_readme_documents_env_loading_rule(readme):
    assert "load_dotenv" in readme
    assert "override=False" in readme


def test_readme_points_at_the_help_endpoint(readme):
    assert "/api/v1/help" in readme
    assert "## Looking up the usage guide" in readme
