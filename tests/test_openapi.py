"""
#45: Swagger UI (/docs), ReDoc (/redoc) and the underlying /openapi.json -
FastAPI generates these for free from the route definitions in app/main.py,
so what's actually worth pinning down here is that adding tags/response
models to those routes didn't break schema generation (a bad response_model
raises at schema-build time, which TestClient would surface as a 500 on
these routes) and that the grouping actually landed.
"""
import os
import tempfile

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)


def test_swagger_ui_is_served():
    r = c.get("/docs")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_redoc_is_served():
    r = c.get("/redoc")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


def test_openapi_schema_is_well_formed():
    r = c.get("/openapi.json")
    assert r.status_code == 200
    schema = r.json()
    assert schema["info"]["title"] == "Story Bible"
    assert schema["info"]["description"]  # non-empty, not just a title


def test_every_declared_tag_is_used_by_at_least_one_operation():
    """Catches a renamed/typo'd tags=[...] on a route silently creating an
    orphaned group in the sidebar, or a tag in TAGS_METADATA nothing uses."""
    schema = c.get("/openapi.json").json()
    declared = {t["name"] for t in schema["tags"]}
    used = {
        tag
        for path in schema["paths"].values()
        for op in path.values()
        for tag in op.get("tags", [])
    }
    assert declared == used


def test_api_root_is_excluded_from_the_docs_but_reachable():
    """"/" serves the task pane's index.html, not an API operation - it
    shouldn't clutter the Swagger/ReDoc route list, but must still work."""
    schema = c.get("/openapi.json").json()
    assert "/" not in schema["paths"]
    assert c.get("/").status_code == 200


def test_health_endpoint_has_a_typed_response_schema():
    """Spot-check one of the routes that got a real response_model (#45) -
    the other small typed endpoints (config/me/users/members) follow the
    same shape, so this stands in for all of them without pinning every
    field name in this test file too."""
    schema = c.get("/openapi.json").json()
    get_op = schema["paths"]["/api/health"]["get"]
    ok_schema = get_op["responses"]["200"]["content"]["application/json"]["schema"]
    # A $ref (or, once resolved, a real object schema) - not the bare
    # "any JSON" shape every un-typed route still correctly gets.
    assert "$ref" in ok_schema or ok_schema.get("type") == "object"
