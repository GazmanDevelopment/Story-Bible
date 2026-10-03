"""
#111: first-sign-in acknowledgement of the privacy policy and terms.
Runs in AUTH_MODE=entra with locally signed tokens, like test_delete_account.py.
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

TENANT = "88888888-8888-8888-8888-888888888888"
CLIENT_ID = "99999999-9999-9999-9999-999999999999"
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
    monkeypatch.setattr(auth, "ADMIN_OIDS", set())
    monkeypatch.setattr(auth, "_jwks_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_pem)
    ))
    monkeypatch.setattr(main, "_write_limiter", None)
    monkeypatch.setattr(main, "POLICY_VERSION", "1.0")


@pytest.fixture(autouse=True)
def policy_gate_open():
    """Overrides tests/conftest.py's fixture of the same name: this file tests the real gate."""
    main._policy_accepted.clear()


def _bearer(**claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now}
    payload.update(claims)
    return {"Authorization": f"Bearer {jwt.encode(payload, _private_pem, algorithm='RS256')}"}


def _as(oid):
    return _bearer(oid=oid, scp="access_as_user", name=f"Name {oid}", preferred_username=f"{oid}@example.com")


def _accept(oid, version="1.0"):
    return c.post("/api/me/accept-policy", json={"version": version}, headers=_as(oid))


def test_config_exposes_the_policy_version(monkeypatch):
    monkeypatch.setattr(main, "POLICY_VERSION", "2.3")
    assert c.get("/api/config").json()["policyVersion"] == "2.3"


def test_new_user_has_not_accepted_then_accepting_makes_them_current():
    me = c.get("/api/me", headers=_as("pol-a")).json()
    assert me["policyCurrent"] is False and me["policyVersion"] == "" and me["policyAcceptedAt"] == 0
    r = _accept("pol-a")
    assert r.status_code == 200
    assert r.json()["policyCurrent"] is True and r.json()["policyVersion"] == "1.0"
    me = c.get("/api/me", headers=_as("pol-a")).json()
    assert me["policyCurrent"] is True and me["policyVersion"] == "1.0"
    assert abs(me["policyAcceptedAt"] - time.time()) < 60


def test_wrong_version_is_rejected_and_nothing_is_stored():
    r = _accept("pol-b", version="0.9")
    assert r.status_code == 409
    me = c.get("/api/me", headers=_as("pol-b")).json()
    assert me["policyCurrent"] is False and me["policyVersion"] == ""


def test_version_bump_asks_again_immediately(monkeypatch):
    """Read from the DB, not the 5-minute _seen_users cache."""
    _accept("pol-c")
    assert c.get("/api/me", headers=_as("pol-c")).json()["policyCurrent"] is True
    monkeypatch.setattr(main, "POLICY_VERSION", "2.0")
    me = c.get("/api/me", headers=_as("pol-c")).json()
    assert me["policyCurrent"] is False and me["policyVersion"] == "1.0"
    assert _accept("pol-c", version="1.0").status_code == 409
    assert _accept("pol-c", version="2.0").status_code == 200
    assert c.get("/api/me", headers=_as("pol-c")).json()["policyCurrent"] is True


def test_requires_sign_in_and_refuses_the_pipeline():
    assert c.post("/api/me/accept-policy", json={"version": "1.0"}).status_code == 401
    pipeline = _bearer(oid="pol-pipe", roles=["Pipeline.Read"])
    assert c.post("/api/me/accept-policy", json={"version": "1.0"}, headers=pipeline).status_code == 403
    me = c.get("/api/me", headers=pipeline).json()
    assert me["isPipeline"] is True and me["policyCurrent"] is True


def test_not_available_without_sign_in_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "none")
    assert c.post("/api/me/accept-policy", json={"version": "1.0"}).status_code == 400
    me = c.get("/api/me").json()
    assert me["policyCurrent"] is True


def test_export_contains_the_accepted_version_and_time():
    c.get("/api/me", headers=_as("pol-d"))
    d = c.get("/api/me/export", headers=_as("pol-d")).json()["account"]
    assert d["policy_version"] == "" and d["policy_accepted_at"] == 0
    _accept("pol-d")
    d = c.get("/api/me/export", headers=_as("pol-d")).json()["account"]
    assert d["policy_version"] == "1.0" and d["policy_accepted_at"] > 0


# ---------------------------------------------------- server-side gate (#130)
def _create_series(oid, name="Gate test"):
    return c.post("/api/series", json={"data": {"name": name}}, headers=_as(oid))


def test_data_routes_refuse_a_person_who_has_not_accepted():
    h = _as("gate-a")
    r = _create_series("gate-a")
    assert r.status_code == 403
    assert r.json()["detail"] == {"error": "policy_not_accepted", "policyVersion": "1.0"}
    for method, path, kwargs in [
        ("get", "/api/series", {}),
        ("post", "/api/import", {"json": {"series": {"name": "x"}}}),
        ("get", "/api/users", {}),
        ("get", "/api/users/lookup?email=a@example.com", {}),
        ("post", "/api/feedback", {"json": {"kind": "issue", "title": "abc", "description": "d"}}),
        ("get", "/api/series/nope/bundle", {}),
        ("get", "/api/series/nope/characters", {}),
        ("put", "/api/series/nope/members/x", {"json": {"role": "viewer"}}),
    ]:
        r = getattr(c, method)(path, headers=h, **kwargs)
        assert r.status_code == 403 and r.json()["detail"]["error"] == "policy_not_accepted", (method, path, r.text)


def test_accepting_opens_the_gate_and_a_version_bump_closes_it_again(monkeypatch):
    assert _create_series("gate-b").status_code == 403
    _accept("gate-b")
    sid = _create_series("gate-b").json()["id"]
    assert c.get(f"/api/series/{sid}/bundle", headers=_as("gate-b")).status_code == 200
    monkeypatch.setattr(main, "POLICY_VERSION", "2.0")
    assert c.get("/api/series", headers=_as("gate-b")).status_code == 403   # accepted 1.0, needs 2.0
    assert _accept("gate-b", version="2.0").status_code == 200
    assert c.get("/api/series", headers=_as("gate-b")).status_code == 200


def test_me_accept_export_and_delete_work_without_accepting():
    """Someone must be able to accept, see their own data and erase it before agreeing to anything."""
    h = _as("gate-c")
    assert c.get("/api/me", headers=h).status_code == 200
    assert c.get("/api/me/export", headers=h).status_code == 200
    assert c.request("DELETE", "/api/me", json={"confirm": "DELETE"}, headers=h).status_code == 200
    assert _accept("gate-c").status_code == 200   # signs up again, then accepts


def test_deleting_the_account_clears_the_acceptance():
    h = _as("gate-d")
    _accept("gate-d")
    assert c.get("/api/series", headers=h).status_code == 200
    assert c.request("DELETE", "/api/me", json={"confirm": "DELETE"}, headers=h).status_code == 200
    # Signing in again makes a fresh account with nothing accepted.
    assert c.get("/api/series", headers=h).status_code == 403


def test_admin_routes_need_acceptance_too(monkeypatch):
    monkeypatch.setattr(auth, "ADMIN_OIDS", {"gate-admin"})
    assert c.get("/api/admin/users", headers=_as("gate-admin")).status_code == 403
    _accept("gate-admin")
    assert c.get("/api/admin/users", headers=_as("gate-admin")).status_code == 200


def test_pipeline_and_sign_in_free_modes_are_not_gated(monkeypatch):
    pipeline = _bearer(oid="gate-pipe", roles=["Pipeline.Read"])
    assert c.get("/api/series", headers=pipeline).status_code == 200
    monkeypatch.setattr(auth, "AUTH_MODE", "none")
    assert c.get("/api/series").status_code == 200


def test_unauthenticated_is_still_401_not_403():
    assert c.get("/api/series").status_code == 401
