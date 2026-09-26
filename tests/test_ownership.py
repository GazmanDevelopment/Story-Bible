"""
#11: series ownership, sharing and permission checks. Only enforced in
AUTH_MODE=entra (see require_access() in app/main.py) - none/token modes
keep today's single-shared-bible behaviour, which the rest of the test
suite already covers.
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

TENANT = "55555555-5555-5555-5555-555555555555"
CLIENT_ID = "66666666-6666-6666-6666-666666666666"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
OWNER_OID = "owner-oid-1"
EDITOR_OID = "editor-oid-2"
VIEWER_OID = "viewer-oid-3"
OUTSIDER_OID = "outsider-oid-4"

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


def _token(oid, **overrides):
    now = int(time.time())
    payload = {
        "iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now,
        "oid": oid, "scp": "access_as_user", "name": oid, "preferred_username": f"{oid}@example.com",
    }
    payload.update(overrides)
    return jwt.encode(payload, _private_pem, algorithm="RS256")


def _as(oid):
    return {"Authorization": f"Bearer {_token(oid)}"}


def _pipeline_headers():
    token = _token("pipeline-oid", scp=None, roles=["Pipeline.Read"])
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def series():
    """A series owned by OWNER_OID, shared with EDITOR_OID (editor) and
    VIEWER_OID (viewer); all three have signed in at least once."""
    for oid in (OWNER_OID, EDITOR_OID, VIEWER_OID):
        c.get("/api/me", headers=_as(oid))  # records them in `users`
    s = c.post("/api/series", json={"data": {"name": "Owned series"}}, headers=_as(OWNER_OID)).json()
    c.put(f"/api/series/{s['id']}/members/{EDITOR_OID}", json={"role": "editor"}, headers=_as(OWNER_OID))
    c.put(f"/api/series/{s['id']}/members/{VIEWER_OID}", json={"role": "viewer"}, headers=_as(OWNER_OID))
    return s


# --------------------------------------------------------------------- reads
def test_owner_can_read(series):
    assert c.get(f"/api/series/{series['id']}/bundle", headers=_as(OWNER_OID)).status_code == 200


def test_editor_can_read(series):
    assert c.get(f"/api/series/{series['id']}/bundle", headers=_as(EDITOR_OID)).status_code == 200


def test_viewer_can_read(series):
    assert c.get(f"/api/series/{series['id']}/bundle", headers=_as(VIEWER_OID)).status_code == 200


def test_outsider_gets_404_not_403():
    """Existence is hidden from a non-member, not just access denied."""
    c.get("/api/me", headers=_as(OUTSIDER_OID))
    s = c.post("/api/series", json={"data": {"name": "Private"}}, headers=_as(OWNER_OID)).json()
    r = c.get(f"/api/series/{s['id']}/bundle", headers=_as(OUTSIDER_OID))
    assert r.status_code == 404


def test_list_series_only_shows_accessible_ones(series):
    c.get("/api/me", headers=_as(OUTSIDER_OID))
    private = c.post("/api/series", json={"data": {"name": "Someone else's"}}, headers=_as(OUTSIDER_OID)).json()
    ids = {s["id"] for s in c.get("/api/series", headers=_as(OWNER_OID)).json()}
    assert series["id"] in ids
    assert private["id"] not in ids


# -------------------------------------------------------------------- writes
def test_owner_can_write(series):
    r = c.post(f"/api/series/{series['id']}/characters", json={"data": {"name": "A"}}, headers=_as(OWNER_OID))
    assert r.status_code == 200


def test_editor_can_write(series):
    r = c.post(f"/api/series/{series['id']}/characters", json={"data": {"name": "A"}}, headers=_as(EDITOR_OID))
    assert r.status_code == 200


def test_viewer_cannot_write(series):
    r = c.post(f"/api/series/{series['id']}/characters", json={"data": {"name": "A"}}, headers=_as(VIEWER_OID))
    assert r.status_code == 403


def test_viewer_cannot_update_series_settings(series):
    r = c.put(f"/api/series/{series['id']}", json={"data": {"name": "Renamed"}}, headers=_as(VIEWER_OID))
    assert r.status_code == 403


def test_outsider_writing_gets_404():
    c.get("/api/me", headers=_as(OUTSIDER_OID))
    s = c.post("/api/series", json={"data": {"name": "Private2"}}, headers=_as(OWNER_OID)).json()
    r = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}}, headers=_as(OUTSIDER_OID))
    assert r.status_code == 404


# ------------------------------------------------------- delete / ownership
def test_editor_cannot_delete_series(series):
    assert c.delete(f"/api/series/{series['id']}", headers=_as(EDITOR_OID)).status_code == 403


def test_viewer_cannot_delete_series(series):
    assert c.delete(f"/api/series/{series['id']}", headers=_as(VIEWER_OID)).status_code == 403


def test_owner_can_delete_series(series):
    assert c.delete(f"/api/series/{series['id']}", headers=_as(OWNER_OID)).status_code == 200


# --------------------------------------------------------------- sharing API
def test_editor_cannot_change_sharing(series):
    r = c.put(f"/api/series/{series['id']}/members/{VIEWER_OID}", json={"role": "editor"}, headers=_as(EDITOR_OID))
    assert r.status_code == 403


def test_owner_can_change_a_members_role(series):
    r = c.put(f"/api/series/{series['id']}/members/{VIEWER_OID}", json={"role": "editor"}, headers=_as(OWNER_OID))
    assert r.status_code == 200
    r = c.post(f"/api/series/{series['id']}/characters", json={"data": {"name": "A"}}, headers=_as(VIEWER_OID))
    assert r.status_code == 200  # promoted to editor, can now write


def test_invalid_role_rejected(series):
    r = c.put(f"/api/series/{series['id']}/members/{VIEWER_OID}", json={"role": "admin"}, headers=_as(OWNER_OID))
    assert r.status_code == 400


def test_cannot_add_unknown_user_as_member(series):
    r = c.put(f"/api/series/{series['id']}/members/never-signed-in", json={"role": "editor"}, headers=_as(OWNER_OID))
    assert r.status_code == 400


def test_list_members_visible_to_any_member(series):
    r = c.get(f"/api/series/{series['id']}/members", headers=_as(VIEWER_OID))
    assert r.status_code == 200
    body = r.json()
    assert body["owner_oid"] == OWNER_OID
    oids = {m["oid"] for m in body["members"]}
    assert oids == {EDITOR_OID, VIEWER_OID}


def test_owner_can_remove_a_member(series):
    r = c.delete(f"/api/series/{series['id']}/members/{VIEWER_OID}", headers=_as(OWNER_OID))
    assert r.status_code == 200
    assert c.get(f"/api/series/{series['id']}/bundle", headers=_as(VIEWER_OID)).status_code == 404


def test_member_can_remove_themselves():
    c.get("/api/me", headers=_as(EDITOR_OID))
    s = c.post("/api/series", json={"data": {"name": "Leave-test"}}, headers=_as(OWNER_OID)).json()
    c.put(f"/api/series/{s['id']}/members/{EDITOR_OID}", json={"role": "editor"}, headers=_as(OWNER_OID))
    r = c.delete(f"/api/series/{s['id']}/members/{EDITOR_OID}", headers=_as(EDITOR_OID))
    assert r.status_code == 200


def test_member_cannot_remove_someone_else(series):
    r = c.delete(f"/api/series/{series['id']}/members/{VIEWER_OID}", headers=_as(EDITOR_OID))
    assert r.status_code == 403


# ------------------------------------------------------- pipeline (read-only)
def test_pipeline_can_read_any_series(series):
    assert c.get(f"/api/series/{series['id']}/bundle", headers=_pipeline_headers()).status_code == 200


def test_pipeline_cannot_write(series):
    r = c.post(f"/api/series/{series['id']}/characters", json={"data": {"name": "A"}}, headers=_pipeline_headers())
    assert r.status_code == 403


def test_pipeline_sees_every_series_in_list(series):
    ids = {s["id"] for s in c.get("/api/series", headers=_pipeline_headers()).json()}
    assert series["id"] in ids


# ----------------------------------------------------- creation sets owner
def test_create_series_sets_caller_as_owner():
    c.get("/api/me", headers=_as(OWNER_OID))
    s = c.post("/api/series", json={"data": {"name": "New"}}, headers=_as(OWNER_OID)).json()
    assert s["owner_oid"] == OWNER_OID


def test_client_cannot_spoof_owner_oid_via_data():
    c.get("/api/me", headers=_as(OWNER_OID))
    s = c.post("/api/series", json={"data": {"name": "New2", "owner_oid": "someone-else"}}, headers=_as(OWNER_OID)).json()
    assert s["owner_oid"] == OWNER_OID


def test_import_sets_caller_as_owner(series):
    bun = c.get(f"/api/series/{series['id']}/bundle", headers=_as(OWNER_OID)).json()
    imp = c.post("/api/import", json=bun, headers=_as(EDITOR_OID)).json()
    r = c.get(f"/api/series/{imp['id']}/bundle", headers=_as(EDITOR_OID)).json()
    assert r["series"]["owner_oid"] == EDITOR_OID


# --------------------------------------------------- first-sign-in claiming
def test_first_sign_in_claims_ownerless_series():
    """#11's migration behaviour: a series created before auth existed
    (owner_oid == '') is claimed by whoever signs in for the very first
    time - simulated here by inserting one directly, bypassing the API."""
    with main.db() as con:
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid) VALUES ('legacy1', '{}', 0, '')"
        )
    fresh_oid = "brand-new-oid-never-seen-before"
    c.get("/api/me", headers=_as(fresh_oid))
    with main.db() as con:
        row = con.execute("SELECT owner_oid FROM series WHERE id='legacy1'").fetchone()
    assert row["owner_oid"] == fresh_oid


def test_second_sign_in_does_not_reclaim_other_ownerless_series():
    with main.db() as con:
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid) VALUES ('legacy2', '{}', 0, '')"
        )
    oid = "already-known-oid"
    c.get("/api/me", headers=_as(oid))  # first sign-in: claims legacy2
    with main.db() as con:
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid) VALUES ('legacy3', '{}', 0, '')"
        )
    c.get("/api/me", headers=_as(oid))  # second sign-in: should NOT claim legacy3
    with main.db() as con:
        row = con.execute("SELECT owner_oid FROM series WHERE id='legacy3'").fetchone()
    assert row["owner_oid"] == ""


# ------------------------------------------------------------ users listing
def test_list_users_returns_everyone_whos_signed_in():
    oid = "listed-user-oid"
    c.get("/api/me", headers=_as(oid))
    oids = {u["oid"] for u in c.get("/api/users", headers=_as(oid)).json()}
    assert oid in oids
