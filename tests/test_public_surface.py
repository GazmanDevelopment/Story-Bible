"""
#68 / #44: the only routes that may answer without credentials are listed
here, explicitly. Anything new that isn't in PUBLIC must depend on
get_current_user, and this file fails the build if it doesn't - so "only
health is public" is enforced rather than remembered.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app import auth, main

ROOT = Path(__file__).resolve().parent.parent

# /api/health: monitoring. /api/config: the pane needs the tenant/client id
# before it can sign in (public ids only). "/": the task pane's index.html
# (static shell, no data). Static assets are the "" mount, checked below.
PUBLIC = {"/api/health", "/api/config", "/"}

DOC_ROUTES = {"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


def _api_routes():
    return [r for r in main.app.routes if isinstance(r, APIRoute)]


def test_every_api_route_is_public_by_allowlist_or_requires_auth():
    unexpected = [
        f"{sorted(r.methods)} {r.path}"
        for r in _api_routes()
        if r.path not in PUBLIC and not _depends_on(r.dependant, main.get_current_user)
    ]
    assert not unexpected, f"routes reachable without auth but not in PUBLIC: {unexpected}"


def test_public_allowlist_has_no_stale_entries():
    paths = {r.path for r in _api_routes()}
    assert PUBLIC <= paths, f"PUBLIC lists routes that no longer exist: {PUBLIC - paths}"


def test_only_expected_non_api_routes_exist():
    """Anything that isn't an APIRoute (a raw Starlette Route or Mount) skips
    dependency injection entirely, so it can't be checked the way the routes
    above are - it has to be an explicit, known list."""
    others = {getattr(r, "path", "") for r in main.app.routes if not isinstance(r, APIRoute)}
    allowed = {""} | (DOC_ROUTES if main.API_DOCS_ENABLED else set())
    assert others <= allowed, f"unexpected non-API routes: {others - allowed}"


def _fill(path: str) -> str:
    import re
    return re.sub(r"\{[^}]+\}", "x", path)


def _protected_calls():
    for r in _api_routes():
        if r.path in PUBLIC:
            continue
        for method in sorted(r.methods - {"HEAD", "OPTIONS"}):
            yield method, _fill(r.path)


def _call(client, method, path):
    # A body that validates for every route that takes one, so a 401 can't be
    # confused with a 422 from body validation running first.
    return client.request(method, path, json={"data": {}, "role": "editor", "kind": "issue"})


def test_every_protected_route_rejects_a_missing_token_in_token_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    monkeypatch.setattr(auth, "TOKEN", "s3cret")
    c = TestClient(main.app)
    calls = list(_protected_calls())
    assert len(calls) > 10  # guard against the discovery silently finding nothing
    for method, path in calls:
        assert _call(c, method, path).status_code == 401, f"{method} {path} answered without a token"
        r = c.request(method, path, json={"data": {}}, headers={"X-Token": "wrong"})
        assert r.status_code == 401, f"{method} {path} accepted a wrong token"


def test_a_valid_token_gets_past_auth_on_every_protected_route(monkeypatch):
    """Positive control for the tests around it: if a dependency were wired
    to something that always 401s, the rejection tests would still pass."""
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    monkeypatch.setattr(auth, "TOKEN", "s3cret")
    c = TestClient(main.app)
    for method, path in _protected_calls():
        r = c.request(method, path, json={"data": {}, "role": "editor", "kind": "issue"}, headers={"X-Token": "s3cret"})
        assert r.status_code not in (401, 403), f"{method} {path} rejected a valid token ({r.status_code})"


def test_every_protected_route_rejects_a_missing_bearer_in_entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    c = TestClient(main.app)
    for method, path in _protected_calls():
        assert _call(c, method, path).status_code == 401, f"{method} {path} answered without a bearer token"
        r = c.request(method, path, json={"data": {}}, headers={"Authorization": "Bearer not.a.jwt"})
        assert r.status_code == 401, f"{method} {path} accepted a garbage bearer token"


def test_public_routes_answer_without_credentials_in_every_mode(monkeypatch):
    c = TestClient(main.app)
    for mode, token in (("token", "s3cret"), ("entra", "")):
        monkeypatch.setattr(auth, "AUTH_MODE", mode)
        monkeypatch.setattr(auth, "TOKEN", token)
        assert c.get("/api/health").status_code in (200, 503)
        assert c.get("/api/config").status_code == 200
        assert c.get("/").status_code == 200


# ------------------------------------------------- API docs are opt-in (#68)
_APP_ENV_KEYS = (
    "ENABLE_API_DOCS", "STORYBIBLE_TOKEN", "AUTH_MODE", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ALLOWED_OIDS",
    "LEGACY_OWNER_OID", "GITHUB_FEEDBACK_TOKEN", "BACKUP_DIR", "STORYBIBLE_DB",
)


def _probe_docs(tmp_path, enable: str | None) -> dict[str, int]:
    """The flag is read once at import, so check in a fresh interpreter with
    a clean app environment (nothing from the developer's shell or CI)."""
    import json
    env = {k: v for k, v in os.environ.items() if k not in _APP_ENV_KEYS}
    if enable is not None:
        env["ENABLE_API_DOCS"] = enable
    env.update(STORYBIBLE_DB=str(tmp_path / "probe.db"), PYTHONUTF8="1")
    code = (
        "import json\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "c = TestClient(app)\n"
        "print(json.dumps({p: c.get(p).status_code for p in "
        "['/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect', '/api/health', '/']}))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_docs_are_off_by_default(tmp_path):
    codes = _probe_docs(tmp_path, None)
    for p in DOC_ROUTES:
        assert codes[p] == 404, f"{p} answered ({codes[p]}) with ENABLE_API_DOCS unset"
    assert codes["/api/health"] == 200 and codes["/"] == 200


@pytest.mark.parametrize("value", ["", "false", "0", "no"])
def test_docs_stay_off_for_falsey_values(tmp_path, value):
    assert _probe_docs(tmp_path, value)["/docs"] == 404


def test_docs_can_be_switched_on_explicitly(tmp_path):
    codes = _probe_docs(tmp_path, "true")
    assert codes["/docs"] == 200 and codes["/redoc"] == 200 and codes["/openapi.json"] == 200


# ------------------------------------- health probe I/O is rate-limited (#68)
def test_health_does_not_hit_the_disk_on_every_request(monkeypatch, tmp_path):
    main._dir_probe_cache.clear()
    calls = []
    real = main._data_dir_writable
    monkeypatch.setattr(main, "_data_dir_writable", lambda d: calls.append(d) or real(d))
    c = TestClient(main.app)
    for _ in range(20):
        assert c.get("/api/health").status_code == 200
    assert len(calls) == 1


def test_health_reprobes_after_the_cache_expires(monkeypatch):
    main._dir_probe_cache.clear()
    calls = []
    real = main._data_dir_writable
    monkeypatch.setattr(main, "_data_dir_writable", lambda d: calls.append(d) or real(d))
    c = TestClient(main.app)
    c.get("/api/health")
    key = next(iter(main._dir_probe_cache))
    main._dir_probe_cache[key] -= main._DIR_PROBE_TTL_SECONDS + 1
    c.get("/api/health")
    assert len(calls) == 2


def test_a_failed_probe_is_not_cached_so_recovery_is_immediate(monkeypatch):
    main._dir_probe_cache.clear()
    results = iter([False, True])
    monkeypatch.setattr(main, "_data_dir_writable", lambda d: next(results))
    c = TestClient(main.app)
    assert c.get("/api/health").status_code == 503
    assert c.get("/api/health").status_code == 200  # no waiting out a TTL after one blip


def test_backup_marker_in_the_future_is_not_fresh(tmp_path, monkeypatch):
    from app import backup
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    import time
    (tmp_path / ".last_success").write_text(str(time.time() + 86400))
    assert backup.last_backup_age_seconds() < 0
    assert main._backup_ok() is False
