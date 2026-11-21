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
    assert all(isinstance(step, str) and step.strip() for step in help_doc["workflow"])
    assert all(isinstance(item, str) and item.strip() for item in help_doc["gotchas"])
    assert help_doc["workflow"] and help_doc["gotchas"] and help_doc["endpoints"]
    endpoints = [(item["method"], item["path"]) for item in help_doc["endpoints"]]
    assert len(endpoints) == len(set(endpoints)), "Each operation is documented once"


def test_help_auth_block_lists_the_protected_endpoints(app_client, help_doc):
    auth = help_doc["auth"]
    assert auth["scheme"] == "Bearer"
    paths = app_client.get("/openapi.json").json()["paths"]
    mutations = {
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        if path.startswith("/api/v1/")
        for method in operations
        if method in {"post", "put", "patch", "delete"}
    }
    assert set(auth["required_for"]) == mutations


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
    assert {"file", "name", "idempotency_key", "note"} <= set(entry["form_fields"])
    assert {"200", "201", "400", "401", "500"} <= set(entry["status_codes"])


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

    # Every API operation must be discoverable by an agent. Previously the help
    # page omitted the group organization routes despite their existence.
    documented = {(entry["method"], entry["path"]) for entry in help_doc["endpoints"]}
    api_operations = {
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith("/api/v1/")
        for method in operations
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert api_operations <= documented


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
