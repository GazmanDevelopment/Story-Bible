"""
#10: Entra token validation, with a fake-token fixture rather than reaching
the real Entra JWKS endpoint - sign JWTs with a local RSA key and point the
validator's JWKS client at a fake object exposing the one method PyJWKClient
itself exposes (`get_signing_key_from_jwt(token).key`), per the issue's own
suggested approach.
"""
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app import auth

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT_ID = "22222222-2222-2222-2222-222222222222"
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
def entra_config(monkeypatch):
    """Point the validator at our fake tenant/client and a fake JWKS client
    backed by the local keypair above, instead of a real Entra endpoint."""
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
        "oid": "aaaaaaaa-0000-0000-0000-000000000001",
        "scp": "access_as_user", "name": "Ada Lovelace", "preferred_username": "ada@example.com",
    }
    payload.update(overrides)
    return jwt.encode(payload, _private_pem, algorithm="RS256")


def test_valid_person_token_resolves_to_a_current_user():
    user = auth.resolve_entra_user(_token())
    assert user.oid == "aaaaaaaa-0000-0000-0000-000000000001"
    assert user.display_name == "Ada Lovelace"
    assert user.email == "ada@example.com"
    assert user.is_pipeline is False


def test_valid_pipeline_token_resolves_to_a_pipeline_user():
    user = auth.resolve_entra_user(_token(scp=None, roles=["Pipeline.Read"]))
    assert user.is_pipeline is True


def test_wrong_audience_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(aud="some-other-client-id"))
    assert exc.value.status_code == 401


def test_wrong_issuer_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(iss="https://login.microsoftonline.com/other-tenant/v2.0"))
    assert exc.value.status_code == 401


def test_v1_issuer_rejected():
    """#9's note: a v1 token (unmigrated app manifest) has iss sts.windows.net/... - must fail closed, not be silently accepted as some other tenant would."""
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(iss=f"https://sts.windows.net/{TENANT}/"))
    assert exc.value.status_code == 401


def test_expired_token_rejected():
    now = int(time.time())
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(exp=now - 3600, iat=now - 7200, nbf=now - 7200))
    assert exc.value.status_code == 401


def test_not_yet_valid_token_rejected():
    now = int(time.time())
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(nbf=now + 3600, iat=now))
    assert exc.value.status_code == 401


def test_leeway_tolerates_a_slightly_expired_token():
    """~60s leeway (#10's spec) for clock skew between this server and Entra."""
    now = int(time.time())
    user = auth.resolve_entra_user(_token(exp=now - 30, iat=now - 3600, nbf=now - 3600))
    assert user.oid


def test_missing_scope_and_role_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(scp=""))
    assert exc.value.status_code == 403


def test_wrong_scope_value_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token(scp="some_other_scope"))
    assert exc.value.status_code == 403


def test_tampered_signature_rejected():
    good = _token()
    tampered = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(tampered)
    assert exc.value.status_code == 401


def test_oid_not_on_allowlist_rejected(monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_OIDS", {"some-other-oid"})
    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_entra_user(_token())
    assert exc.value.status_code == 403


def test_oid_on_allowlist_accepted(monkeypatch):
    oid = "aaaaaaaa-0000-0000-0000-000000000001"
    monkeypatch.setattr(auth, "ALLOWED_OIDS", {oid})
    user = auth.resolve_entra_user(_token())
    assert user.oid == oid


def test_empty_allowlist_means_no_extra_restriction():
    # ALLOWED_OIDS is defence in depth on top of Entra's own "assignment
    # required" gate, not the primary one - unset means "don't add a check".
    user = auth.resolve_entra_user(_token())
    assert user.oid


def test_pipeline_token_is_not_subject_to_allowed_oids(monkeypatch):
    monkeypatch.setattr(auth, "ALLOWED_OIDS", {"some-person-oid"})
    user = auth.resolve_entra_user(_token(scp=None, roles=["Pipeline.Read"]))
    assert user.is_pipeline is True


def test_never_logs_the_raw_token_on_failure(caplog):
    token = _token(aud="wrong")
    with pytest.raises(auth.AuthError):
        auth.resolve_entra_user(token)
    assert token not in caplog.text


# --------------------------------------------------------------- bearer_token
def test_bearer_token_extracts_the_token():
    assert auth.bearer_token("Bearer abc.def.ghi") == "abc.def.ghi"


def test_bearer_token_is_case_insensitive_on_the_scheme():
    assert auth.bearer_token("bearer abc.def.ghi") == "abc.def.ghi"


def test_bearer_token_missing_header_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.bearer_token(None)
    assert exc.value.status_code == 401


def test_bearer_token_wrong_scheme_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.bearer_token("Basic abc.def.ghi")
    assert exc.value.status_code == 401


def test_bearer_token_empty_token_rejected():
    with pytest.raises(auth.AuthError) as exc:
        auth.bearer_token("Bearer ")
    assert exc.value.status_code == 401


# --------------------------------------------------------------- AUTH_MODE
def test_auth_mode_defaults_to_none_without_a_token():
    import importlib
    import os
    old = os.environ.pop("AUTH_MODE", None)
    old_token = os.environ.pop("STORYBIBLE_TOKEN", None)
    try:
        mod = importlib.reload(auth)
        assert mod.AUTH_MODE == "none"
    finally:
        importlib.reload(auth)  # restore real module state for later tests
        if old is not None:
            os.environ["AUTH_MODE"] = old
        if old_token is not None:
            os.environ["STORYBIBLE_TOKEN"] = old_token


def test_auth_mode_defaults_to_token_when_storybible_token_set():
    import importlib
    import os
    old = os.environ.pop("AUTH_MODE", None)
    os.environ["STORYBIBLE_TOKEN"] = "secret"
    try:
        mod = importlib.reload(auth)
        assert mod.AUTH_MODE == "token"
    finally:
        del os.environ["STORYBIBLE_TOKEN"]
        importlib.reload(auth)
        if old is not None:
            os.environ["AUTH_MODE"] = old


def test_invalid_auth_mode_raises():
    import importlib
    import os
    os.environ["AUTH_MODE"] = "bogus"
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(auth)
    finally:
        del os.environ["AUTH_MODE"]
        importlib.reload(auth)


def test_entra_mode_without_tenant_or_client_id_raises():
    import importlib
    import os
    os.environ["AUTH_MODE"] = "entra"
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(auth)
    finally:
        del os.environ["AUTH_MODE"]
        importlib.reload(auth)


def test_token_mode_without_a_token_raises():
    """A blank STORYBIBLE_TOKEN under AUTH_MODE=token would otherwise make
    `if auth.TOKEN and x_token != auth.TOKEN` in main.py's get_current_user
    vacuously false - silently accepting every request as authenticated
    while /api/health and /api/config both report auth as "on"."""
    import importlib
    import os
    old_token = os.environ.pop("STORYBIBLE_TOKEN", None)
    os.environ["AUTH_MODE"] = "token"
    try:
        with pytest.raises(RuntimeError):
            importlib.reload(auth)
    finally:
        del os.environ["AUTH_MODE"]
        if old_token is not None:
            os.environ["STORYBIBLE_TOKEN"] = old_token
        importlib.reload(auth)
