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
