"""
#10: Entra auth wired into the actual HTTP layer (app/main.py's
get_current_user), on top of tests/test_auth.py's pure token-validation
unit tests. Uses the same fake-JWKS-client approach: sign JWTs with a
local RSA key, never reach a real Entra endpoint.
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

TENANT = "33333333-3333-3333-3333-333333333333"
CLIENT_ID = "44444444-4444-4444-4444-444444444444"
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


@pytest.fixture
def entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    monkeypatch.setattr(auth, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(auth, "ENTRA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth, "ALLOWED_OIDS", set())
    monkeypatch.setattr(auth, "_jwks_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_pem)
    ))


def _token(**overrides):
    now = int(time.time())
    payload = {
        "iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now,
        "oid": overrides.pop("oid", "bbbbbbbb-0000-0000-0000-000000000002"),
        "scp": "access_as_user", "name": "Grace Hopper", "preferred_username": "grace@example.com",
    }
    payload.update(overrides)
    return jwt.encode(payload, _private_pem, algorithm="RS256")


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_config_endpoint_needs_no_auth_and_reflects_auth_mode(entra_mode):
    r = c.get("/api/config")
    assert r.status_code == 200
    assert r.json() == {"authMode": "entra", "tenantId": TENANT, "clientId": CLIENT_ID}


def test_config_endpoint_in_default_mode_has_no_tenant_info():
    r = c.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["authMode"] in ("none", "token")
    assert body["tenantId"] == "" and body["clientId"] == ""


def test_entra_mode_rejects_requests_with_no_bearer_token(entra_mode):
    r = c.get("/api/series")
    assert r.status_code == 401


def test_entra_mode_accepts_a_valid_token(entra_mode):
    r = c.get("/api/series", headers=_auth(_token()))
    assert r.status_code == 200


def test_entra_mode_rejects_an_invalid_token(entra_mode):
    r = c.get("/api/series", headers=_auth(_token(aud="wrong-client")))
    assert r.status_code == 401


def test_me_endpoint_reflects_the_bearer_tokens_identity(entra_mode):
    r = c.get("/api/me", headers=_auth(_token(oid="cccccccc-0000-0000-0000-000000000003")))
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "oid": "cccccccc-0000-0000-0000-000000000003",
        "email": "grace@example.com", "displayName": "Grace Hopper", "isPipeline": False,
    }


def test_me_endpoint_in_none_mode_is_the_synthetic_local_user():
    r = c.get("/api/me")
    assert r.status_code == 200
    assert r.json()["oid"] == "local"


def test_first_request_records_a_users_row(entra_mode):
    oid = "dddddddd-0000-0000-0000-000000000004"
    c.get("/api/me", headers=_auth(_token(oid=oid)))
    with main.db() as con:
        row = con.execute("SELECT * FROM users WHERE oid=?", (oid,)).fetchone()
    assert row is not None
    assert row["display_name"] == "Grace Hopper"
    assert row["first_seen"] == row["last_seen"]


def test_second_request_updates_last_seen_not_first_seen(entra_mode):
    oid = "eeeeeeee-0000-0000-0000-000000000005"
    c.get("/api/me", headers=_auth(_token(oid=oid)))
    with main.db() as con:
        first = con.execute("SELECT first_seen, last_seen FROM users WHERE oid=?", (oid,)).fetchone()
    time.sleep(0.01)
    c.get("/api/me", headers=_auth(_token(oid=oid)))
    with main.db() as con:
        second = con.execute("SELECT first_seen, last_seen FROM users WHERE oid=?", (oid,)).fetchone()
    assert second["first_seen"] == first["first_seen"]
    assert second["last_seen"] > first["last_seen"]


def test_allowed_oids_blocks_a_person_not_on_the_list(entra_mode, monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_OIDS", {"someone-else"})
    r = c.get("/api/series", headers=_auth(_token()))
    assert r.status_code == 403


def test_pipeline_token_is_authenticated_but_not_recorded_as_a_user(entra_mode):
    token = _token(scp=None, roles=["Pipeline.Read"], oid="pipeline-app-oid")
    r = c.get("/api/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["isPipeline"] is True
    with main.db() as con:
        row = con.execute("SELECT * FROM users WHERE oid=?", ("pipeline-app-oid",)).fetchone()
    assert row is None  # app-only tokens aren't people, don't clutter the users table


def test_none_and_token_modes_still_work_exactly_as_before():
    """Regression guard: adding AUTH_MODE must not change behaviour for
    every deployment that never sets it (the whole existing test suite is
    itself the bulk of this guarantee, but this pins it explicitly)."""
    assert auth.AUTH_MODE in ("none", "token")
    r = c.get("/api/series")
    assert r.status_code == 200


def test_health_endpoints_stay_unauthenticated_even_in_entra_mode(entra_mode):
    """#17: an uptime checker has no bearer token (or X-Token) to send -
    these two routes must never gain an auth dependency, in any AUTH_MODE,
    or external monitoring breaks silently."""
    assert c.get("/api/health").status_code in (200, 503)
    assert c.get("/api/health/backup").status_code in (200, 503)
