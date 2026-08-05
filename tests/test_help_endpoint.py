"""GET /api/v1/help.

This endpoint exists to be "parseable enough that an automation agent can read it
and call straight through". So the tests do not stop at a 200: they check (a) that
it needs no auth, (b) that every required key is present, and (c) that the document
does not drift from the real routes. (c) is the important one — a help document
that starts lying is worse than none at all.
"""

from __future__ import annotations

import pytest

TOP_LEVEL_KEYS = {
    "service",
    "version",
    "summary",
    "auth",
    "workflow",
    "endpoints",
    "examples",
    "gotchas",
}


@pytest.fixture(scope="module")
def help_doc(app_client):
    res = app_client.get("/api/v1/help")
    assert res.status_code == 200, res.text
    return res.json()


def test_help_requires_no_auth(app_client):
    """200 without a token. Reading the instructions must not require auth."""
    res = app_client.get("/api/v1/help")
    assert res.status_code == 200
    assert res.json()["service"] == "MirageGlass"


def test_help_top_level_shape(help_doc):
    assert TOP_LEVEL_KEYS <= set(help_doc)
    assert len(help_doc["workflow"]) == 6
    assert len(help_doc["gotchas"]) == 10
    assert len(help_doc["endpoints"]) == 16


def test_help_auth_block_lists_the_protected_endpoints(help_doc):
    auth = help_doc["auth"]
    assert auth["scheme"] == "Bearer"
    assert set(auth["required_for"]) == {
        "POST /api/v1/decks",
        "POST /api/v1/decks/{deck_id}/versions",
        "POST /api/v1/decks/{deck_id}/versions/{version_no}/activate",
        "DELETE /api/v1/decks/{deck_id}",
    }


def test_every_endpoint_entry_is_complete(help_doc):
    for entry in help_doc["endpoints"]:
        assert {"method", "path", "auth", "description"} <= set(entry), entry
        assert isinstance(entry["auth"], bool), entry
        assert entry["description"].strip(), entry


def test_create_deck_entry_documents_form_fields_and_status_codes(help_doc):
    entry = next(
        e
        for e in help_doc["endpoints"]
        if e["method"] == "POST" and e["path"] == "/api/v1/decks"
    )
    assert set(entry["form_fields"]) == {"file", "name", "idempotency_key"}
    assert set(entry["status_codes"]) == {"200", "201", "400", "401", "500"}


def test_auth_flags_match_the_auth_block(help_doc):
    """endpoints[].auth and auth.required_for must not contradict each other."""
    required = set(help_doc["auth"]["required_for"])
    for entry in help_doc["endpoints"]:
        label = f"{entry['method']} {entry['path']}"
        assert entry["auth"] is (label in required), label


def _normalize(path: str) -> str:
    return path.rstrip("/") or "/"


def test_documented_endpoints_exist_on_the_app(app_client, help_doc):
    """Every path and method help advertises has to actually exist.

    We do not walk app.routes — fastapi 0.139 wraps an included router in
    _IncludedRouter, which hides the path. The OpenAPI schema is the public
    contract, so we compare against that instead.
    """
    paths = app_client.get("/openapi.json").json()["paths"]
    known = {_normalize(p): {m.upper() for m in ops} for p, ops in paths.items()}

    for entry in help_doc["endpoints"]:
        path = _normalize(entry["path"])
        assert path in known, f"help advertises a path that does not exist: {entry['path']}"
        assert entry["method"] in known[path], f"method mismatch: {entry}"


def test_python_example_is_not_curl_based(help_doc):
    """curl caused the encoding incident, so the example has to be requests-based."""
    snippet = help_doc["examples"]["create_deck_python"]
    assert "requests.post" in snippet
    assert "idempotency_key" in snippet
    assert "Authorization" in snippet


def test_help_is_registered_in_openapi(app_client):
    res = app_client.get("/openapi.json")
    assert res.status_code == 200
    assert "/api/v1/help" in res.json()["paths"]
