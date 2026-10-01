"""
#90 (WP3 of #85): SIGNUP_MODE=open - tokens from any Entra tenant and personal
Microsoft accounts. Same fake-JWKS approach as tests/test_auth.py: sign JWTs
with a local RSA key and never reach a real Entra endpoint.
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

HOME = "55555555-5555-5555-5555-555555555555"
CLIENT_ID = "66666666-6666-6666-6666-666666666666"
FOREIGN_A = "aaaaaaaa-1111-1111-1111-aaaaaaaaaaaa"
FOREIGN_B = "bbbbbbbb-2222-2222-2222-bbbbbbbbbbbb"
PERSONAL = "9188040d-6c67-4c5b-b112-36a304b66dad"  # Microsoft's tenant for personal accounts

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


def _iss(tid):
    return f"https://login.microsoftonline.com/{tid}/v2.0"


@pytest.fixture(autouse=True)
def open_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    monkeypatch.setattr(auth, "SIGNUP_MODE", "open")
    monkeypatch.setattr(auth, "ENTRA_TENANT_ID", HOME)
    monkeypatch.setattr(auth, "ENTRA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth, "ALLOWED_OIDS", set())
    monkeypatch.setattr(auth, "BLOCKED_TENANTS", set())
    monkeypatch.setattr(auth, "LEGACY_OWNER_OID", "")
    monkeypatch.setattr(auth, "_jwks_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_pem)
    ))
    main._seen_users.clear()


def _token(tenant=FOREIGN_A, **overrides):
    now = int(time.time())
    payload = {
        "iss": _iss(tenant), "tid": tenant, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now,
        "oid": "10000000-0000-0000-0000-000000000001",
        "scp": "access_as_user", "name": "Ada", "preferred_username": "ada@foreign.example",
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    return jwt.encode(payload, _private_pem, algorithm="RS256")


def _h(tenant=FOREIGN_A, **overrides):
    return {"Authorization": f"Bearer {_token(tenant, **overrides)}"}


def _status(token):
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(token)
    return exc.value.status_code


# ------------------------------------------------------------ token validation
def test_foreign_tenant_token_accepted_in_open_mode():
    user = auth.resolve_entra_user(_token(FOREIGN_A))
    assert user.tid == FOREIGN_A and user.oid.startswith("10000000")


def test_personal_account_token_accepted_in_open_mode():
    assert auth.resolve_entra_user(_token(PERSONAL)).tid == PERSONAL


def test_home_tenant_token_still_accepted_in_open_mode():
    assert auth.resolve_entra_user(_token(HOME)).tid == HOME


@pytest.mark.parametrize("tid", [FOREIGN_A, PERSONAL])
def test_foreign_and_personal_tokens_rejected_in_allowlist_mode(monkeypatch, tid):
    monkeypatch.setattr(auth, "SIGNUP_MODE", "allowlist")
    assert _status(_token(tid)) == 401


def test_allowlist_mode_still_accepts_the_home_tenant(monkeypatch):
    monkeypatch.setattr(auth, "SIGNUP_MODE", "allowlist")
    assert auth.resolve_entra_user(_token(HOME)).tid == HOME


def test_issuer_not_matching_the_tokens_tid_is_401():
    assert _status(_token(FOREIGN_A, iss=_iss(FOREIGN_B))) == 401


def test_v1_issuer_is_401_in_open_mode():
    assert _status(_token(FOREIGN_A, iss=f"https://sts.windows.net/{FOREIGN_A}/")) == 401


def test_missing_or_malformed_tid_is_401():
    assert _status(_token(FOREIGN_A, tid=None)) == 401
    assert _status(_token(FOREIGN_A, tid="not-a-guid", iss=_iss("not-a-guid"))) == 401


def test_other_checks_still_apply_in_open_mode():
    assert _status(_token(aud="someone-elses-app")) == 401
    assert _status(_token(oid=None)) == 401
    assert _status(_token(scp="other")) == 403
    good = _token()
    assert _status(good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")) == 401
    now = int(time.time())
    assert _status(_token(exp=now - 3600, iat=now - 7200, nbf=now - 7200)) == 401


def test_blocked_tenant_is_403_and_others_are_unaffected(monkeypatch):
    monkeypatch.setattr(auth, "BLOCKED_TENANTS", {FOREIGN_A})
    assert _status(_token(FOREIGN_A)) == 403
    assert auth.resolve_entra_user(_token(FOREIGN_B)).tid == FOREIGN_B


def test_foreign_tenant_cannot_use_the_pipeline_role():
    """App roles are assigned per tenant, so a foreign admin could grant
    Pipeline.Read to their own principal."""
    assert _status(_token(FOREIGN_A, scp=None, roles=["Pipeline.Read"])) == 403
    assert auth.resolve_entra_user(_token(HOME, scp=None, roles=["Pipeline.Read"])).is_pipeline


def test_allowed_oids_is_optional_in_open_mode_but_enforced_when_set(monkeypatch):
    assert auth.resolve_entra_user(_token()).oid
    monkeypatch.setattr(auth, "ALLOWED_OIDS", {"someone-else"})
    assert _status(_token()) == 403


def test_jwks_url_and_issuer_are_mode_aware(monkeypatch):
    assert auth.jwks_url() == "https://login.microsoftonline.com/common/discovery/v2.0/keys"
    assert auth.issuer(FOREIGN_A) == _iss(FOREIGN_A)
    monkeypatch.setattr(auth, "SIGNUP_MODE", "allowlist")
    assert auth.jwks_url() == f"https://login.microsoftonline.com/{HOME}/discovery/v2.0/keys"
    assert auth.issuer(FOREIGN_A) == _iss(HOME)  # a token's own tid is never trusted here


# ------------------------------------------------------------- over HTTP
def _sid(headers, name="s"):
    return c.post("/api/series", json={"data": {"name": name}}, headers=headers).json()["id"]


def test_tid_is_recorded_and_refreshed_on_sign_in():
    oid = "20000000-0000-0000-0000-000000000002"
    assert c.get("/api/me", headers=_h(FOREIGN_A, oid=oid)).status_code == 200
    with main.db() as con:
        assert con.execute("SELECT tid FROM users WHERE oid=?", (oid,)).fetchone()["tid"] == FOREIGN_A
        con.execute("UPDATE users SET tid='' WHERE oid=?", (oid,))  # a row from before #90
    main._seen_users.clear()
    c.get("/api/me", headers=_h(FOREIGN_A, oid=oid))
    with main.db() as con:
        assert con.execute("SELECT tid FROM users WHERE oid=?", (oid,)).fetchone()["tid"] == FOREIGN_A


def test_two_foreign_tenant_users_cannot_see_each_others_series():
    a = _h(FOREIGN_A, oid="30000000-0000-0000-0000-00000000000a")
    b = _h(FOREIGN_B, oid="30000000-0000-0000-0000-00000000000b")
    sid = _sid(a, "A's private bible")
    assert c.get(f"/api/series/{sid}/bundle", headers=a).status_code == 200
    assert c.get(f"/api/series/{sid}/bundle", headers=b).status_code in (403, 404)
    assert sid not in [s["id"] for s in c.get("/api/series", headers=b).json()]
    assert c.delete(f"/api/series/{sid}", headers=b).status_code in (403, 404)


def _insert_legacy(sid):
    with main.db() as con:
        con.execute("INSERT INTO series (id, data, updated, owner_oid) VALUES (?,?,?,'')", (sid, '{"name":"old"}', 0))


def _owner(sid):
    with main.db() as con:
        return con.execute("SELECT owner_oid FROM series WHERE id=?", (sid,)).fetchone()["owner_oid"]


def test_a_stranger_cannot_claim_legacy_series(monkeypatch):
    owner = "40000000-0000-0000-0000-0000000000a0"
    monkeypatch.setattr(auth, "LEGACY_OWNER_OID", owner)
    _insert_legacy("legacy-open-1")
    c.get("/api/me", headers=_h(FOREIGN_A, oid="40000000-0000-0000-0000-0000000000b1"))
    assert _owner("legacy-open-1") == ""


def test_a_foreign_tenant_principal_with_the_legacy_oid_cannot_claim_either(monkeypatch):
    owner = "40000000-0000-0000-0000-0000000000a2"
    monkeypatch.setattr(auth, "LEGACY_OWNER_OID", owner)
    _insert_legacy("legacy-open-2")
    c.get("/api/me", headers=_h(FOREIGN_A, oid=owner))
    assert _owner("legacy-open-2") == ""
    with main.db() as con:
        con.execute("DELETE FROM users WHERE oid=?", (owner,))  # the genuine person's own first sign-in
    main._seen_users.clear()
    c.get("/api/me", headers=_h(HOME, oid=owner))
    assert _owner("legacy-open-2") == owner


def test_foreign_tenant_pipeline_token_is_refused_over_http():
    assert c.get("/api/series", headers=_h(FOREIGN_A, scp=None, roles=["Pipeline.Read"])).status_code == 403


def test_blocked_tenant_is_403_over_http(monkeypatch):
    monkeypatch.setattr(auth, "BLOCKED_TENANTS", {FOREIGN_B})
    assert c.get("/api/me", headers=_h(FOREIGN_B, oid="50000000-0000-0000-0000-000000000005")).status_code == 403


def test_admin_must_be_signed_in_from_the_home_tenant(monkeypatch):
    admin_oid = "60000000-0000-0000-0000-0000000000ad"
    monkeypatch.setattr(auth, "ADMIN_OIDS", {admin_oid})
    assert c.get("/api/admin/users", headers=_h(HOME, oid=admin_oid)).status_code == 200
    assert c.get("/api/admin/users", headers=_h(FOREIGN_A, oid=admin_oid)).status_code in (403, 404)


def test_same_oid_from_a_different_tenant_cannot_take_over_an_account():
    oid = "70000000-0000-0000-0000-000000000007"
    assert c.get("/api/me", headers=_h(FOREIGN_A, oid=oid, name="Real Person")).status_code == 200
    main._seen_users.clear()
    assert c.get("/api/me", headers=_h(FOREIGN_B, oid=oid, name="Impostor")).status_code == 403
    with main.db() as con:
        row = con.execute("SELECT display_name, tid FROM users WHERE oid=?", (oid,)).fetchone()
    assert (row["display_name"], row["tid"]) == ("Real Person", FOREIGN_A)
