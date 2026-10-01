"""
#89: abuse and capacity limits for open signup - MAX_USERS, the per-owner
storage ceiling, the per-person write rate limit and the feedback caps.
All run in AUTH_MODE=entra with locally signed tokens, like test_ownership.py.
"""
import os
import tempfile
import time
from types import SimpleNamespace

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth, github_feedback as feedback, main

c = TestClient(main.app)

TENANT = "77777777-7777-7777-7777-777777777777"
CLIENT_ID = "88888888-8888-8888-8888-888888888888"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_pem = _private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_public_pem = _private_key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PublicFormat.SubjectPublicKeyInfo,
)


@pytest.fixture(autouse=True)
def entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    monkeypatch.setattr(auth, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(auth, "ENTRA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth, "ALLOWED_OIDS", set())
    monkeypatch.setattr(auth, "_jwks_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_pem)
    ))


def _bearer(**claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now}
    payload.update(claims)
    return {"Authorization": f"Bearer {jwt.encode(payload, _private_pem, algorithm='RS256')}"}


def _as(oid):
    return _bearer(oid=oid, scp="access_as_user", name=oid, preferred_username=f"{oid}@example.com")


def _pipeline():
    return _bearer(oid="pipeline-oid", roles=["Pipeline.Read"])


def _user_count():
    with main.db() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def _used(oid):
    with main.db() as con:
        return main.owner_bytes(con, oid)


# ------------------------------------------------------------------ MAX_USERS
def test_max_users_refuses_a_new_account_once_reached(monkeypatch):
    c.get("/api/me", headers=_as("cap-seed"))  # a count of 0 would mean "unlimited"
    monkeypatch.setattr(main, "MAX_USERS", _user_count())
    main._seen_users.clear()
    r = c.get("/api/me", headers=_as("cap-newcomer"))
    assert r.status_code == 403
    assert "not accepting new accounts" in r.json()["detail"]
    with main.db() as con:
        assert not con.execute("SELECT 1 FROM users WHERE oid='cap-newcomer'").fetchone()


def test_max_users_lets_existing_accounts_keep_signing_in(monkeypatch):
    assert c.get("/api/me", headers=_as("cap-existing")).status_code == 200
    monkeypatch.setattr(main, "MAX_USERS", _user_count())  # exactly full
    main._seen_users.clear()                                # force the DB path, not the recently-seen cache
    assert c.get("/api/me", headers=_as("cap-existing")).status_code == 200


def test_max_users_admits_up_to_the_cap_then_stops(monkeypatch):
    monkeypatch.setattr(main, "MAX_USERS", _user_count() + 1)
    main._seen_users.clear()
    assert c.get("/api/me", headers=_as("cap-last-seat")).status_code == 200
    assert c.get("/api/me", headers=_as("cap-too-late")).status_code == 403


def test_max_users_zero_means_unlimited(monkeypatch):
    monkeypatch.setattr(main, "MAX_USERS", 0)
    assert c.get("/api/me", headers=_as("cap-unlimited")).status_code == 200


# ------------------------------------------------------------ storage ceiling
def _series(oid, name="s"):
    return c.post("/api/series", json={"data": {"name": name}}, headers=_as(oid)).json()["id"]


def _char(sid, oid, text, name="c"):
    return c.post(f"/api/series/{sid}/characters", json={"data": {"name": name, "notes": text}}, headers=_as(oid))


def test_storage_ceiling_blocks_growth_with_400(monkeypatch):
    owner = "store-owner"
    sid = _series(owner)
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", _used(owner) + 2000)
    assert _char(sid, owner, "x" * 500).status_code == 200
    r = _char(sid, owner, "y" * 3000)
    assert r.status_code == 400 and "Storage limit" in r.json()["detail"]


def test_storage_ceiling_is_per_owner_not_global(monkeypatch):
    a, b = "store-a", "store-b"
    sa, sb = _series(a), _series(b)
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", _used(a) + 1500)
    assert _char(sa, a, "x" * 4000).status_code == 400   # a is over
    assert _char(sb, b, "z" * 500).status_code == 200    # b's own usage is far below


def test_shared_editor_records_count_against_the_owner_not_the_editor(monkeypatch):
    owner, editor = "store-shared-owner", "store-shared-editor"
    c.get("/api/me", headers=_as(editor))
    sid = _series(owner)
    c.put(f"/api/series/{sid}/members/{editor}", json={"role": "editor"}, headers=_as(owner))
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", _used(owner) + 1000)
    assert _char(sid, editor, "x" * 3000).status_code == 400
    assert _used(editor) == 0
    assert c.post("/api/series", json={"data": {"name": "editor's own"}}, headers=_as(editor)).status_code == 200


def test_shrinking_edit_and_delete_still_work_when_over_the_ceiling(monkeypatch):
    owner = "store-over"
    sid = _series(owner)
    ch = _char(sid, owner, "x" * 2000).json()
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", 10)   # now far over
    shrink = c.put(f"/api/series/{sid}/characters/{ch['id']}",
                   json={"data": {"name": "c", "notes": "tiny"}, "version": ch["version"]}, headers=_as(owner))
    assert shrink.status_code == 200
    grow = c.put(f"/api/series/{sid}/characters/{ch['id']}",
                 json={"data": {"name": "c", "notes": "g" * 2000}, "version": shrink.json()["version"]},
                 headers=_as(owner))
    assert grow.status_code == 400
    assert c.delete(f"/api/series/{sid}/characters/{ch['id']}", headers=_as(owner)).status_code == 200


def test_storage_ceiling_applies_to_new_series_and_series_edits(monkeypatch):
    owner = "store-series"
    sid = _series(owner)
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", _used(owner) + 100)
    assert c.post("/api/series", json={"data": {"name": "n" * 500}}, headers=_as(owner)).status_code == 400
    cur = c.get("/api/series", headers=_as(owner)).json()[0]
    r = c.put(f"/api/series/{sid}", json={"data": {"name": "s", "description": "d" * 2000}, "version": cur["version"]},
              headers=_as(owner))
    assert r.status_code == 400


def test_storage_ceiling_applies_to_import(monkeypatch):
    owner = "store-import"
    _series(owner)
    used = _used(owner)
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", used + 500)
    bundle = {"series": {"name": "imp"},
              "characters": [{"id": f"c{i}", "name": "n", "notes": "x" * 400} for i in range(5)]}
    r = c.post("/api/import", json=bundle, headers=_as(owner))
    assert r.status_code == 400 and "Storage limit" in r.json()["detail"]
    assert _used(owner) == used          # nothing half-imported


def test_storage_ceiling_zero_means_off(monkeypatch):
    monkeypatch.setattr(main, "MAX_BYTES_PER_OWNER", 0)
    owner = "store-off"
    assert _char(_series(owner), owner, "x" * 5000).status_code == 200


# ------------------------------------------------------------- write rate limit
def _limit(monkeypatch, n):
    monkeypatch.setattr(main, "_write_limiter", feedback._RateLimiter(n, 60))


def test_write_rate_limit_returns_429_with_retry_after(monkeypatch):
    _limit(monkeypatch, 3)
    h = _as("rate-a")
    sid = c.post("/api/series", json={"data": {"name": "r"}}, headers=h).json()["id"]
    assert c.post(f"/api/series/{sid}/characters", json={"data": {"name": "1"}}, headers=h).status_code == 200
    assert c.post(f"/api/series/{sid}/characters", json={"data": {"name": "2"}}, headers=h).status_code == 200
    r = c.post(f"/api/series/{sid}/characters", json={"data": {"name": "3"}}, headers=h)
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"


def test_write_rate_limit_is_per_person(monkeypatch):
    _limit(monkeypatch, 1)
    assert c.post("/api/series", json={"data": {"name": "p1"}}, headers=_as("rate-p1")).status_code == 200
    assert c.post("/api/series", json={"data": {"name": "p1b"}}, headers=_as("rate-p1")).status_code == 429
    assert c.post("/api/series", json={"data": {"name": "p2"}}, headers=_as("rate-p2")).status_code == 200


def test_write_rate_limit_does_not_touch_reads(monkeypatch):
    _limit(monkeypatch, 1)
    h = _as("rate-reader")
    for _ in range(5):
        assert c.get("/api/series", headers=h).status_code == 200
        assert c.get("/api/me", headers=h).status_code == 200


def test_write_rate_limit_covers_every_write_route(monkeypatch):
    _limit(monkeypatch, 1)
    h = _as("rate-routes")
    assert c.delete("/api/series/nope", headers=h).status_code == 404          # takes the one slot
    for r in (c.put("/api/series/nope", json={"data": {"name": "x"}, "version": 1}, headers=h),
              c.delete("/api/series/nope", headers=h),
              c.post("/api/import", json={"series": {"name": "x"}}, headers=h),
              c.put("/api/series/nope/members/x", json={"role": "viewer"}, headers=h),
              c.delete("/api/series/nope/members/x", headers=h),
              c.post("/api/series/nope/characters", json={"data": {"name": "x"}}, headers=h),
              c.put("/api/series/nope/characters/x", json={"data": {"name": "x"}, "version": 1}, headers=h),
              c.delete("/api/series/nope/characters/x", headers=h)):
        assert r.status_code == 429, f"{r.request.method} {r.request.url}"


def test_write_rate_limit_is_off_outside_entra(monkeypatch):
    _limit(monkeypatch, 1)
    monkeypatch.setattr(auth, "AUTH_MODE", "none")
    for i in range(4):
        assert c.post("/api/series", json={"data": {"name": f"n{i}"}}).status_code == 200


def test_write_rate_limit_none_disables_it(monkeypatch):
    monkeypatch.setattr(main, "_write_limiter", None)
    for i in range(4):
        assert c.post("/api/series", json={"data": {"name": f"d{i}"}}, headers=_as("rate-off")).status_code == 200


def test_write_rate_limit_does_not_apply_to_the_pipeline(monkeypatch):
    _limit(monkeypatch, 1)
    for _ in range(3):  # still 403 (read-only), never 429
        assert c.post("/api/series", json={"data": {"name": "x"}}, headers=_pipeline()).status_code == 403


def test_defaults():
    assert main.WRITE_RATE_LIMIT_PER_MINUTE == 120
    assert main.MAX_USERS == 0
    assert main.MAX_BYTES_PER_OWNER == 500 * 1024 * 1024


# ------------------------------------------------------------ feedback caps
def test_global_feedback_cap_has_headroom_for_many_people():
    assert feedback.RATE_LIMIT_GLOBAL_MAX_CALLS >= 10 * feedback.RATE_LIMIT_MAX_CALLS


def test_one_abuser_cannot_exhaust_the_global_feedback_cap():
    lim = feedback._RateLimiter(5, 3600, 60)
    assert sum(1 for _ in range(100) if lim.reserve("abuser")) == 5
    assert lim.reserve("someone-else") is not None


def test_global_full_reports_which_cap_was_hit():
    lim = feedback._RateLimiter(5, 3600, 2)
    lim.reserve("a")
    lim.reserve("b")
    assert lim.reserve("c") is None and lim.global_full()
    per = feedback._RateLimiter(1, 3600, 10)
    per.reserve("a")
    assert per.reserve("a") is None and not per.global_full()
