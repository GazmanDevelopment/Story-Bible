"""
#82: the administrator's view of registered users, and blocking.
Runs in AUTH_MODE=entra with locally signed tokens, like test_ownership.py.
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

from app import auth, main

c = TestClient(main.app)

TENANT = "99999999-9999-9999-9999-999999999999"
CLIENT_ID = "88888888-8888-8888-8888-888888888888"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
ADMIN = "adm-admin-oid"
ADMIN_EMAIL = "admin@example.com"

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
    monkeypatch.setattr(auth, "ADMIN_OIDS", {ADMIN})
    monkeypatch.setattr(auth, "_jwks_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_pem)
    ))


def _bearer(**claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now}
    payload.update(claims)
    return {"Authorization": f"Bearer {jwt.encode(payload, _private_pem, algorithm='RS256')}"}


def _as(oid, email=None):
    return _bearer(oid=oid, scp="access_as_user", name=oid, preferred_username=email or f"{oid}@example.com")


def _users():
    r = c.get("/api/admin/users", headers=_as(ADMIN))
    assert r.status_code == 200, r.text
    return {u["oid"]: u for u in r.json()["users"]}


def _unblock_all():
    with main.db() as con:
        con.execute("DELETE FROM blocked_users")


@pytest.fixture(autouse=True)
def _clean_blocks():
    yield
    _unblock_all()


# ------------------------------------------------------------------ who may use it
def test_non_admin_gets_404_on_every_admin_route():
    h = _as("adm-plain")
    assert c.get("/api/admin/users", headers=h).status_code == 404
    assert c.put("/api/admin/users/x/block", headers=h).status_code == 404
    assert c.delete("/api/admin/users/x/block", headers=h).status_code == 404


def test_pipeline_cannot_use_admin_routes(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_OIDS", {"pipeline-oid"})
    h = _bearer(oid="pipeline-oid", roles=["Pipeline.Read"])
    assert c.get("/api/admin/users", headers=h).status_code == 403


def test_matching_the_admins_email_does_not_make_you_admin():
    """In open signup the email claim is controlled by the signer's own
    tenant, so only the oid counts."""
    c.get("/api/me", headers=_as(ADMIN, ADMIN_EMAIL))
    h = _as("adm-impostor", ADMIN_EMAIL)
    assert c.get("/api/admin/users", headers=h).status_code == 404
    assert c.get("/api/me", headers=h).json()["isAdmin"] is False


def test_admin_routes_are_off_outside_entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "none")
    assert c.get("/api/admin/users").status_code == 404
    assert c.get("/api/me").json()["isAdmin"] is False


def test_me_reports_is_admin():
    assert c.get("/api/me", headers=_as(ADMIN)).json()["isAdmin"] is True
    assert c.get("/api/me", headers=_as("adm-plain")).json()["isAdmin"] is False


def test_admin_oids_rejects_emails_and_wildcards():
    # A subprocess: ADMIN_OIDS is read once at import, and reloading app.auth
    # in-process would reset the state other tests have patched.
    import subprocess, sys
    for bad, ok in (("someone@example.com", False), ("*", False), ("a b", False), ("abc,def", True), ("", True)):
        r = subprocess.run([sys.executable, "-c", "import app.auth"], capture_output=True, text=True,
                           env={**os.environ, "ADMIN_OIDS": bad, "AUTH_MODE": "none", "STORYBIBLE_TOKEN": ""})
        assert (r.returncode == 0) is ok, (bad, r.stderr[-300:])


# ------------------------------------------------------------------ the listing
def test_listing_shows_counts_sizes_and_activity():
    owner, other = "adm-owner", "adm-other"
    c.get("/api/me", headers=_as(other))
    sid = c.post("/api/series", json={"data": {"name": "S1"}}, headers=_as(owner)).json()["id"]
    c.post("/api/series", json={"data": {"name": "S2"}}, headers=_as(owner))
    for n in range(3):
        c.post(f"/api/series/{sid}/characters", json={"data": {"name": f"C{n}"}}, headers=_as(owner))
    c.post(f"/api/series/{sid}/locations", json={"data": {"name": "L"}}, headers=_as(owner))
    c.put(f"/api/series/{sid}/members/{other}", json={"role": "viewer"}, headers=_as(owner))

    users = _users()
    o = users[owner]
    assert o["series_owned"] == 2 and o["series_shared"] == 0
    assert o["records"]["characters"] == 3 and o["records"]["locations"] == 1
    assert o["records"]["events"] == 0 and set(o["records"]) == set(main.KINDS)
    with main.db() as con:
        assert o["bytes_used"] == main.owner_bytes(con, owner)
    assert o["blocked"] is False and o["email"] == f"{owner}@example.com"
    assert o["first_seen"] <= o["last_seen"]
    assert users[other]["series_owned"] == 0 and users[other]["series_shared"] == 1


def test_listing_never_includes_story_content():
    owner = "adm-secret-owner"
    sid = c.post("/api/series", json={"data": {"name": "Hidden series name"}}, headers=_as(owner)).json()["id"]
    c.post(f"/api/series/{sid}/characters", json={"data": {"name": "Hidden Hero", "notes": "private plot"}},
           headers=_as(owner))
    text = c.get("/api/admin/users", headers=_as(ADMIN)).text
    assert "Hidden" not in text and "private plot" not in text


def test_listing_is_most_recently_active_first_and_limited():
    c.get("/api/me", headers=_as("adm-old"))
    c.get("/api/me", headers=_as("adm-new"))
    with main.db() as con:
        con.execute("UPDATE users SET last_seen=1 WHERE oid='adm-old'")
        con.execute("UPDATE users SET last_seen=?, email=email WHERE oid='adm-new'", (time.time() + 1000,))
    r = c.get("/api/admin/users?limit=1", headers=_as(ADMIN)).json()
    assert len(r["users"]) == 1 and r["users"][0]["oid"] == "adm-new" and r["total"] >= 2
    assert c.get("/api/admin/users?limit=0", headers=_as(ADMIN)).status_code == 422


# ------------------------------------------------------------------ blocking
def test_blocked_user_gets_403_and_unblocking_restores_access():
    victim = "adm-victim"
    assert c.get("/api/me", headers=_as(victim)).status_code == 200
    r = c.put(f"/api/admin/users/{victim}/block", json={"reason": "spam"}, headers=_as(ADMIN))
    assert r.status_code == 200 and r.json() == {"blocked": True}
    assert c.get("/api/me", headers=_as(victim)).status_code == 403
    assert c.get("/api/series", headers=_as(victim)).status_code == 403
    u = _users()[victim]
    assert u["blocked"] and u["blocked_reason"] == "spam" and u["blocked_by"] == ADMIN and u["blocked_at"]
    assert c.delete(f"/api/admin/users/{victim}/block", headers=_as(ADMIN)).json() == {"blocked": False}
    assert c.get("/api/me", headers=_as(victim)).status_code == 200


def test_block_takes_effect_despite_a_warm_last_seen_cache():
    victim = "adm-cached"
    c.get("/api/me", headers=_as(victim))
    assert (main.DB_PATH, victim) in main._seen_users  # the fast path would skip the DB
    c.put(f"/api/admin/users/{victim}/block", headers=_as(ADMIN))
    assert c.get("/api/me", headers=_as(victim)).status_code == 403


def test_block_without_a_body_is_allowed():
    c.get("/api/me", headers=_as("adm-nobody"))
    assert c.put("/api/admin/users/adm-nobody/block", headers=_as(ADMIN)).status_code == 200
    assert _users()["adm-nobody"]["blocked_reason"] == ""


def test_block_keeps_the_users_data_and_other_peoples_access():
    owner, friend = "adm-keep-owner", "adm-keep-friend"
    sid = c.post("/api/series", json={"data": {"name": "kept"}}, headers=_as(owner)).json()["id"]
    c.get("/api/me", headers=_as(friend))
    c.put(f"/api/series/{sid}/members/{friend}", json={"role": "viewer"}, headers=_as(owner))
    c.put(f"/api/admin/users/{owner}/block", headers=_as(ADMIN))
    assert _users()[owner]["series_owned"] == 1
    assert c.get(f"/api/series/{sid}/bundle", headers=_as(friend)).status_code == 200


def test_cannot_block_yourself_or_another_admin(monkeypatch):
    assert c.put(f"/api/admin/users/{ADMIN}/block", headers=_as(ADMIN)).status_code == 400
    monkeypatch.setattr(auth, "ADMIN_OIDS", {ADMIN, "adm-second"})
    c.get("/api/me", headers=_as("adm-second"))
    assert c.put("/api/admin/users/adm-second/block", headers=_as(ADMIN)).status_code == 400
    assert c.get("/api/me", headers=_as("adm-second")).status_code == 200


def test_block_unknown_user_404_and_unblock_is_idempotent():
    assert c.put("/api/admin/users/adm-nobody-here/block", headers=_as(ADMIN)).status_code == 404
    c.get("/api/me", headers=_as("adm-fine"))
    # unblocking someone who isn't blocked is a harmless no-op (a double click, a second admin)
    assert c.delete("/api/admin/users/adm-fine/block", headers=_as(ADMIN)).json() == {"blocked": False}


def test_reason_is_length_limited():
    c.get("/api/me", headers=_as("adm-long"))
    r = c.put("/api/admin/users/adm-long/block", json={"reason": "x" * 501}, headers=_as(ADMIN))
    assert r.status_code in (400, 422)


def test_block_survives_the_users_row_being_deleted():
    """Deleting an account (#86) must not be a way round a block."""
    victim = "adm-evader"
    c.get("/api/me", headers=_as(victim))
    c.put(f"/api/admin/users/{victim}/block", headers=_as(ADMIN))
    with main.db() as con:
        con.execute("DELETE FROM users WHERE oid=?", (victim,))
    main._seen_users.pop((main.DB_PATH, victim), None)
    assert c.get("/api/me", headers=_as(victim)).status_code == 403


def test_blocked_user_still_counts_toward_max_users(monkeypatch):
    c.get("/api/me", headers=_as("adm-counted"))
    c.put("/api/admin/users/adm-counted/block", headers=_as(ADMIN))
    with main.db() as con:
        n = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    monkeypatch.setattr(main, "MAX_USERS", n)
    assert c.get("/api/me", headers=_as("adm-brand-new")).status_code == 403
    assert c.get("/api/me", headers=_as("adm-counted")).status_code == 403


def test_blocking_is_rate_limited_like_other_writes(monkeypatch):
    from app import github_feedback as feedback
    monkeypatch.setattr(main, "_write_limiter", feedback._RateLimiter(1, 60))
    c.get("/api/me", headers=_as("adm-rl"))
    assert c.put("/api/admin/users/adm-rl/block", headers=_as(ADMIN)).status_code == 200
    assert c.delete("/api/admin/users/adm-rl/block", headers=_as(ADMIN)).status_code == 429
    monkeypatch.setattr(main, "_write_limiter", None)


def test_block_and_unblock_are_logged(caplog):
    import logging
    c.get("/api/me", headers=_as("adm-logged"))
    main.logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="storybible"):
            c.put("/api/admin/users/adm-logged/block", headers=_as(ADMIN))
            c.delete("/api/admin/users/adm-logged/block", headers=_as(ADMIN))
    finally:
        main.logger.propagate = False
    text = caplog.text
    assert f"admin {ADMIN} blocked user adm-logged" in text and "unblocked user adm-logged" in text


def test_blocked_people_are_listed_first_so_they_can_always_be_unblocked():
    c.get("/api/me", headers=_as("adm-sinks"))
    c.put("/api/admin/users/adm-sinks/block", headers=_as(ADMIN))
    for n in range(3):
        c.get("/api/me", headers=_as(f"adm-busy{n}"))
    with main.db() as con:  # everyone else is far more recently active
        con.execute("UPDATE users SET last_seen=? WHERE oid LIKE 'adm-busy%'", (time.time() + 5000,))
        con.execute("UPDATE users SET last_seen=1 WHERE oid='adm-sinks'")
    r = c.get("/api/admin/users?limit=1", headers=_as(ADMIN)).json()
    assert r["users"][0]["oid"] == "adm-sinks" and r["users"][0]["blocked"]


def test_an_in_flight_request_cannot_recache_a_person_after_they_are_blocked(monkeypatch):
    """The request already passed the blocked_users check when the block
    landed; it must not leave a cache entry that skips the check for 5 minutes."""
    victim = "adm-racer"
    c.get("/api/me", headers=_as(victim))
    main._seen_users.pop((main.DB_PATH, victim), None)
    real_upsert = main.upsert_user

    def upsert_then_block(con, user):
        out = real_upsert(con, user)
        if user.oid == victim:  # an admin's block lands after the blocked_users check above
            main._block_epoch += 1
        return out

    monkeypatch.setattr(main, "upsert_user", upsert_then_block)
    c.get("/api/me", headers=_as(victim))
    monkeypatch.setattr(main, "upsert_user", real_upsert)
    assert (main.DB_PATH, victim) not in main._seen_users


def test_admin_oids_match_case_insensitively(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_OIDS", {"abcdef01-0000-0000-0000-000000000000"})
    assert c.get("/api/admin/users", headers=_as("ABCDEF01-0000-0000-0000-000000000000")).status_code == 200
