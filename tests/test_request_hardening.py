"""
#69: request-handling hardening - the body cap is enforced while the body
streams (not after it has all been buffered), auth beats body parsing for
unauthenticated callers, and every response carries security headers.
"""
import asyncio
import os
import re
import tempfile
from pathlib import Path

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import pytest
from fastapi.testclient import TestClient

from app import auth, main

c = TestClient(main.app)
STATIC = Path(main.STATIC_DIR)


def _asgi_post(path, chunks, chunk_size=65536, headers=()):
    """Drive the app directly with a body of `chunks` chunks and no
    Content-Length (i.e. chunked transfer) and report how many chunks the
    server actually pulled before answering."""
    pulled = 0

    async def receive():
        nonlocal pulled
        pulled += 1
        return {"type": "http.request", "body": b"x" * chunk_size, "more_body": pulled < chunks}

    sent = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "method": "POST", "path": path, "query_string": b"", "http_version": "1.1",
        "scheme": "http", "server": ("test", 80), "client": ("c", 1), "root_path": "",
        "headers": [(b"content-type", b"application/json"), *headers],
    }
    asyncio.run(main.app(scope, receive, send))
    start = next(m for m in sent if m["type"] == "http.response.start")
    return pulled, start["status"], dict(start["headers"])


# ------------------------------------------------- body cap streams (#69)
def test_chunked_upload_stops_being_read_at_the_cap(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1024 * 1024)
    pulled, status, _ = _asgi_post("/api/series", chunks=2000)  # ~125 MiB offered
    assert status == 413
    assert pulled <= 1024 * 1024 // 65536 + 2  # the cap's worth of chunks, not 2000


def test_a_lying_content_length_does_not_get_past_the_cap(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1024 * 1024)
    pulled, status, _ = _asgi_post("/api/series", chunks=2000, headers=[(b"content-length", b"10")])
    assert status == 413
    assert pulled < 40


def test_an_oversized_content_length_is_refused_without_reading_anything(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1024)
    pulled, status, _ = _asgi_post("/api/series", chunks=5, headers=[(b"content-length", b"999999999")])
    assert status == 413
    assert pulled == 0


def test_import_streams_against_its_own_cap(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 1024)
    monkeypatch.setattr(main, "IMPORT_MAX_BODY_BYTES", 512 * 1024)
    pulled, status, _ = _asgi_post("/api/import", chunks=2000)
    assert status == 413
    assert 8 <= pulled <= 10  # 512 KiB / 64 KiB: past the small cap, stopped at the import one


def test_a_body_under_the_cap_is_unaffected():
    r = c.post("/api/series", json={"data": {"name": "fine"}})
    assert r.status_code == 200


def test_the_413_response_has_the_standard_headers(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 10)
    r = c.post("/api/series", json={"data": {"name": "longer than ten bytes"}})
    assert r.status_code == 413
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"


def test_the_oversize_request_is_logged_with_its_413():
    import io
    import logging
    buf, handler = io.StringIO(), None
    handler = logging.StreamHandler(buf)
    main.logger.addHandler(handler)
    try:
        _asgi_post("/api/series", chunks=2000)
    finally:
        main.logger.removeHandler(handler)
    assert "POST /api/series 413" in buf.getvalue()


# ------------------------------------- auth before body parsing (#69)
BAD_JSON = {"content": b"{not json", "headers": {"content-type": "application/json"}}


def test_unauthenticated_malformed_json_gets_401_not_422_in_token_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    monkeypatch.setattr(auth, "TOKEN", "s3cret")
    assert c.post("/api/series", **BAD_JSON).status_code == 401
    wrong = {**BAD_JSON, "headers": {**BAD_JSON["headers"], "X-Token": "nope"}}
    assert c.post("/api/series", **wrong).status_code == 401


def test_authenticated_malformed_json_is_still_a_422(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    monkeypatch.setattr(auth, "TOKEN", "s3cret")
    ok = {**BAD_JSON, "headers": {**BAD_JSON["headers"], "X-Token": "s3cret"}}
    assert c.post("/api/series", **ok).status_code == 422


def test_unauthenticated_malformed_json_gets_401_in_entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    assert c.post("/api/series", **BAD_JSON).status_code == 401
    bearer = {**BAD_JSON, "headers": {**BAD_JSON["headers"], "Authorization": "Bearer not.a.jwt"}}
    assert c.post("/api/series", **bearer).status_code == 401


def test_malformed_json_with_no_auth_configured_is_still_a_422():
    assert c.post("/api/series", **BAD_JSON).status_code == 422


def test_other_validation_errors_are_untouched(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    monkeypatch.setattr(auth, "TOKEN", "s3cret")
    r = c.post("/api/series", json={"not": "the shape"}, headers={"X-Token": "s3cret"})
    assert r.status_code == 422


# ---------------------------------------------------- security headers (#69)
def _assert_secure(r):
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "camera=()" in r.headers["permissions-policy"]
    assert r.headers["content-security-policy"] == main.CONTENT_SECURITY_POLICY


@pytest.mark.parametrize("path", ["/", "/app.js", "/styles.css", "/auth-dialog.html", "/api/health", "/api/config", "/nope"])
def test_security_headers_on_every_kind_of_response(path):
    _assert_secure(c.get(path))


def test_security_headers_on_auth_failures(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    monkeypatch.setattr(auth, "TOKEN", "s3cret")
    r = c.get("/api/series")
    assert r.status_code == 401
    _assert_secure(r)


def test_security_headers_on_the_unhandled_exception_response(monkeypatch):
    def boom():
        raise RuntimeError("boom")
    monkeypatch.setattr(main, "_db_ok", boom)
    r = TestClient(main.app, raise_server_exceptions=False).get("/api/health")
    assert r.status_code == 500
    _assert_secure(r)
    assert r.headers["cache-control"] == "no-store"


def test_csp_forbids_the_dangerous_things():
    csp = main.DEFAULT_CSP
    assert "'unsafe-eval'" not in csp
    script_src = next(d for d in csp.split("; ") if d.startswith("script-src"))
    assert "'unsafe-inline'" not in script_src  # inline scripts are the XSS foothold
    assert "object-src 'none'" in csp and "base-uri 'self'" in csp
    assert csp.startswith("default-src 'self'")


def test_csp_can_be_overridden_or_turned_off(monkeypatch):
    monkeypatch.setattr(main, "CONTENT_SECURITY_POLICY", "default-src 'none'")
    assert c.get("/").headers["content-security-policy"] == "default-src 'none'"
    monkeypatch.setattr(main, "CONTENT_SECURITY_POLICY", "off")
    r = c.get("/")
    assert "content-security-policy" not in r.headers
    assert r.headers["x-content-type-options"] == "nosniff"  # the other headers stay


# ---- the pane's own files have to keep working under the CSP we ship
@pytest.mark.parametrize("page", ["index.html", "auth-dialog.html"])
def test_pages_load_scripts_only_from_origins_the_csp_allows(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    script_src = next(d for d in main.DEFAULT_CSP.split("; ") if d.startswith("script-src"))
    allowed_hosts = re.findall(r"https://[\w.-]+", script_src)
    for tag in re.findall(r"<script\b[^>]*>", html):
        m = re.search(r'src="([^"]+)"', tag)
        assert m, f"{page} has an inline <script> ({tag}) - the CSP would block it"
        src = m.group(1)
        assert not src.startswith("http") or any(src.startswith(h) for h in allowed_hosts), f"{page}: {src} not allowed by the CSP"


@pytest.mark.parametrize("page", ["index.html", "auth-dialog.html"])
def test_pages_have_no_inline_event_handlers(page):
    html = (STATIC / page).read_text(encoding="utf-8")
    assert not re.search(r"\son[a-z]+\s*=", html), f"{page} has an inline on*= handler, blocked by the CSP"


def test_the_auth_dialog_script_is_served():
    r = c.get("/auth-dialog.js")
    assert r.status_code == 200
    assert "handleRedirectPromise" in r.text


def test_the_app_never_builds_inline_handlers():
    """app.js renders HTML strings; an onclick= in one would be silently
    dead under the CSP (all interaction goes through data-act instead)."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"""\son(click|change|input|submit|load|error)\s*=""", js)


# ------------------------------------------------------ review follow-ups
def test_a_blank_csp_setting_means_the_default_not_no_csp(monkeypatch, tmp_path):
    import subprocess
    import sys
    env = {k: v for k, v in os.environ.items() if k not in ("CONTENT_SECURITY_POLICY", "STORYBIBLE_TOKEN", "AUTH_MODE")}
    env.update(CONTENT_SECURITY_POLICY="   ", STORYBIBLE_DB=str(tmp_path / "x.db"), PYTHONUTF8="1")
    out = subprocess.run(
        [sys.executable, "-c", "from app import main; print(main.CONTENT_SECURITY_POLICY == main.DEFAULT_CSP)"],
        cwd=Path(__file__).resolve().parent.parent, env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "True", out.stderr


def test_swagger_and_redoc_pages_are_exempt_from_the_csp():
    """They need an inline script and jsdelivr assets; the suite runs with
    ENABLE_API_DOCS=true (tests/conftest.py), so they exist here."""
    for path in ("/docs", "/redoc"):
        r = c.get(path)
        assert r.status_code == 200
        assert "content-security-policy" not in r.headers
        assert r.headers["x-content-type-options"] == "nosniff"


def test_a_response_the_app_already_started_is_not_followed_by_a_second_start():
    """If the cap trips after the app has begun replying there is nothing
    sane to add - in particular no second http.response.start."""
    async def app_that_replies_first(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await receive()  # trips the cap after the response has started
        await send({"type": "http.response.body", "body": b"ok"})

    mw = main.HardeningMiddleware(app_that_replies_first)
    sent = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b"x" * 100, "more_body": False}

    scope = {"type": "http", "method": "POST", "path": "/api/series", "headers": [], "query_string": b""}
    original = main.MAX_BODY_BYTES
    main.MAX_BODY_BYTES = 10
    try:
        asyncio.run(mw(scope, receive, send))
    finally:
        main.MAX_BODY_BYTES = original
    assert [m["type"] for m in sent].count("http.response.start") == 1
