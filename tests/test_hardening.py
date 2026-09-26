import io
import logging
import os
import sys
import tempfile

# mkstemp(), not mktemp(): the latter is a TOCTOU race between naming the
# file and creating it (CodeQL py/insecure-temporary-file, #28). Avoided
# entirely (rather than mkstemp()-then-discard) when some earlier-imported
# test module already set STORYBIBLE_DB, which is the common case.
if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import app.main as main
from fastapi.testclient import TestClient

c = TestClient(main.app)


def _capture_log():
    """Attach a throwaway handler directly to main.logger and return its
    buffer. Deterministic and independent of pytest's stdout/fd capturing
    (capsys/capfd don't play well with TestClient's background event loop
    thread here) - this tests what our code hands to the logger, which is
    what we actually care about; that a StreamHandler faithfully writes to
    whatever stream it's given is the stdlib's job, not ours to re-verify."""
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(message)s"))
    main.logger.addHandler(handler)
    return buf, handler


# --------------------------------------------------------------------- health
def test_health_ok_shape():
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "version" in body and "auth" in body
    assert "error" not in body

def test_health_reports_503_when_db_unreachable(monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", "/no/such/directory/db.sqlite")
    r = c.get("/api/health")
    assert r.status_code == 503
    body = r.json()
    assert body["ok"] is False
    assert "error" in body
    # version/auth still reported even when unhealthy - useful for monitoring
    assert "version" in body and "auth" in body

def test_data_dir_writable_helper(tmp_path):
    assert main._data_dir_writable(tmp_path) is True
    not_a_dir = tmp_path / "im_a_file"
    not_a_dir.write_text("x")
    assert main._data_dir_writable(not_a_dir) is False

def test_health_error_message_is_generic_not_the_raw_exception(monkeypatch):
    """/api/health has no auth - the failure reason (which can contain a
    filesystem path) is logged, not handed to whoever's asking."""
    monkeypatch.setattr(main, "DB_PATH", "/no/such/directory/db.sqlite")
    body = c.get("/api/health").json()
    assert body["error"] == "database unavailable"
    assert "/no/such/directory" not in body["error"]


# ---------------------------------------------------------------- validation
def test_record_defaults_filled_in():
    s = c.post("/api/series", json={"data": {"name": "Defaults"}}).json()
    ch = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "Only a name"}}).json()
    assert ch["name"] == "Only a name"
    assert ch["age"] == "" and ch["custom"] == {} and ch["backstory"] == ""

def test_loose_str_coerces_a_stray_number():
    """A hand-edited import (or an odd client) sending a number where a text
    field is expected shouldn't 400 - it gets coerced to text instead."""
    s = c.post("/api/series", json={"data": {"name": "Coerce"}}).json()
    ch = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "N", "age": 34}}).json()
    assert ch["age"] == "34"

def test_invalid_field_type_rejected_with_400():
    s = c.post("/api/series", json={"data": {"name": "Bad"}}).json()
    r = c.post(f"/api/series/{s['id']}/events", json={"data": {"title": "E", "off_y": "not-a-number"}})
    assert r.status_code == 400

def test_bad_id_list_element_rejected_with_400():
    s = c.post("/api/series", json={"data": {"name": "Bad2"}}).json()
    r = c.post(f"/api/series/{s['id']}/locations", json={"data": {"name": "L", "character_ids": [123]}})
    assert r.status_code == 400

def test_unknown_top_level_field_is_ignored_not_rejected():
    s = c.post("/api/series", json={"data": {"name": "Extra"}}).json()
    r = c.post(f"/api/series/{s['id']}/characters",
               json={"data": {"name": "N", "totally_unknown_field": "x"}})
    assert r.status_code == 200
    assert "totally_unknown_field" not in r.json()

def test_relationship_ids_rejected_not_coerced():
    """Unlike free-text fields, from/to are ids - a non-string value there
    is corruption, not a harmless type slip, and coercing it would just
    silently create a reference nothing can ever resolve."""
    s = c.post("/api/series", json={"data": {"name": "Rel"}}).json()
    r = c.post(f"/api/series/{s['id']}/relationships",
               json={"data": {"from": 123, "type": "knows", "to": 456}})
    assert r.status_code == 400


# ---------------------------------------------------------- import validation
def test_import_rejects_missing_series():
    assert c.post("/api/import", json={"characters": []}).status_code == 400

def test_import_rejects_non_dict_series():
    assert c.post("/api/import", json={"series": "not a dict"}).status_code == 400

def test_import_rejects_record_missing_id():
    r = c.post("/api/import", json={"series": {"name": "X"}, "characters": [{"name": "no id"}]})
    assert r.status_code == 400

def test_import_rejects_non_dict_record():
    r = c.post("/api/import", json={"series": {"name": "X"}, "characters": ["not a dict"]})
    assert r.status_code == 400

def test_import_rejects_non_list_kind():
    r = c.post("/api/import", json={"series": {"name": "X"}, "characters": {"id": "a"}})
    assert r.status_code == 400

def test_import_still_works_and_validates_records():
    bundle_in = {
        "series": {"name": "Good Import"},
        "characters": [{"id": "c1", "name": "A", "age": 40}],  # age as int, on purpose
        "chapters": [], "locations": [], "events": [], "relationships": [],
    }
    r = c.post("/api/import", json=bundle_in)
    assert r.status_code == 200
    sid = r.json()["id"]
    chars = c.get(f"/api/series/{sid}/characters").json()
    assert chars[0]["age"] == "40"  # coerced, not rejected

def test_import_rejects_duplicate_ids_across_kinds():
    """Two records sharing an id (even across different kinds) would both
    get remapped to the same new id and collide on records.id's primary
    key - must 400 up front instead of hitting that IntegrityError."""
    bundle_in = {
        "series": {"name": "Dup"},
        "characters": [{"id": "dup", "name": "A"}],
        "locations": [{"id": "dup", "name": "L"}],
        "chapters": [], "events": [], "relationships": [],
    }
    assert c.post("/api/import", json=bundle_in).status_code == 400


# --------------------------------------------------------------- body size cap
def test_body_too_large_rejected_with_413(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 10)
    r = c.post("/api/series", json={"data": {"name": "This is definitely more than ten bytes"}})
    assert r.status_code == 413


def test_import_gets_its_own_larger_body_cap(monkeypatch):
    """#43: a whole-series export/import bundle isn't a single record - it
    can carry several Research entries' embedded images and legitimately
    exceed the general per-record MAX_BODY_BYTES cap, so it's checked
    against IMPORT_MAX_BODY_BYTES instead."""
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 10)
    monkeypatch.setattr(main, "IMPORT_MAX_BODY_BYTES", 10_000)
    bundle_in = {
        "series": {"name": "T"}, "chapters": [], "characters": [], "locations": [],
        "events": [], "relationships": [], "research": [],
    }
    # under IMPORT_MAX_BODY_BYTES but (with headroom to spare) over the tiny
    # general MAX_BODY_BYTES set above - proves the import path isn't just
    # silently using the general cap.
    bundle_in["series"]["description"] = "x" * 5000
    r = c.post("/api/import", json=bundle_in)
    assert r.status_code == 200, r.text

    monkeypatch.setattr(main, "IMPORT_MAX_BODY_BYTES", 10)
    r = c.post("/api/import", json=bundle_in)
    assert r.status_code == 413


# ----------------------------------------------------------------- caching
def test_api_responses_are_not_cached():
    r = c.get("/api/health")
    assert r.headers.get("cache-control") == "no-store"

def test_task_pane_files_are_no_cache_not_no_store():
    """#4: Word/a browser must always revalidate (no-cache) rather than
    show a stale app.js after a deploy - but no-store (the /api/* policy)
    would needlessly throw away the cache StaticFiles' ETag support makes
    cheap to revalidate against."""
    for path in ("/", "/app.js", "/styles.css"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path

def test_static_file_revalidation_is_a_cheap_304():
    first = c.get("/app.js")
    etag = first.headers.get("etag")
    assert etag
    again = c.get("/app.js", headers={"If-None-Match": etag})
    assert again.status_code == 304


# ----------------------------------------------------- unhandled exceptions
def test_unhandled_exception_gets_a_controlled_response(monkeypatch):
    """A route that raises something other than HTTPException must still
    come back as a logged, no-store JSON 500 (unhandled_exception_handler),
    not Starlette's bare default response, and without leaking the
    exception's own text to the caller.

    Uses its own TestClient with raise_server_exceptions=False: the default
    `c` client (raise_server_exceptions=True, the default) deliberately
    re-raises an exception that reached ServerErrorMiddleware instead of
    returning its response, to make a genuine crash fail the test loudly -
    exactly what we don't want for this one, since we're intentionally
    causing that path and asserting on the response it produces."""
    def boom():
        raise RuntimeError("boom-detail: some internal detail that shouldn't reach the client")
    monkeypatch.setattr(main, "_db_ok", boom)
    quiet_client = TestClient(main.app, raise_server_exceptions=False)
    r = quiet_client.get("/api/health")
    assert r.status_code == 500
    assert r.json() == {"detail": "Internal server error"}
    assert "boom-detail" not in r.text
    assert r.headers.get("cache-control") == "no-store"


# ----------------------------------------------------------------- logging
def test_sanitize_for_log_strips_newlines():
    assert main._sanitize_for_log("/api/\ninjected\r\nFAKE 200") == "/api/\\ninjected\\r\\nFAKE 200"

def test_logger_writes_to_stdout():
    """The issue asks specifically for stdout, not stderr (logging's own
    basicConfig() defaults to stderr) - check the configured handler
    targets the right stream rather than capturing real process output."""
    assert any(
        isinstance(h, logging.StreamHandler) and h.stream is sys.stdout
        for h in main.logger.handlers
    )

def test_requests_are_logged():
    buf, handler = _capture_log()
    try:
        c.get("/api/health")
    finally:
        main.logger.removeHandler(handler)
    assert "GET /api/health 200" in buf.getvalue()

def test_logging_never_includes_body_or_token():
    buf, handler = _capture_log()
    try:
        secret_body_marker = "TOTALLY-UNIQUE-MARKER-98765"
        s = c.post("/api/series", json={"data": {"name": "LogTest"}}).json()
        c.post(
            f"/api/series/{s['id']}/characters",
            json={"data": {"name": "N", "backstory": secret_body_marker}},
            headers={"X-Token": "should-never-be-logged"},
        )
    finally:
        main.logger.removeHandler(handler)
    out = buf.getvalue()
    assert secret_body_marker not in out
    assert "should-never-be-logged" not in out
