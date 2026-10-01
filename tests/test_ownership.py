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


def _as(oid, **overrides):
    return {"Authorization": f"Bearer {_token(oid, **overrides)}"}


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


# ------------------------------------------- claiming pre-auth series (#70)
def _insert_legacy(sid):
    with main.db() as con:
        con.execute("INSERT INTO series (id, data, updated, owner_oid) VALUES (?, '{}', 0, '')", (sid,))


def _owner_of(sid):
    with main.db() as con:
        return con.execute("SELECT owner_oid FROM series WHERE id=?", (sid,)).fetchone()["owner_oid"]


def test_first_sign_in_does_not_claim_ownerless_series(monkeypatch):
    """#70 regression: this used to hand every pre-auth series to whichever
    tenant user happened to sign in first."""
    monkeypatch.setattr(auth, "LEGACY_OWNER_OID", "")
    _insert_legacy("legacy1")
    c.get("/api/me", headers=_as("brand-new-oid-never-seen-before"))
    assert _owner_of("legacy1") == ""
    assert c.get("/api/series", headers=_as("brand-new-oid-never-seen-before")).json() == []


def test_only_the_configured_legacy_owner_claims_ownerless_series(monkeypatch):
    monkeypatch.setattr(auth, "LEGACY_OWNER_OID", "the-real-owner")
    _insert_legacy("legacy2")
    c.get("/api/me", headers=_as("some-stranger"))  # signs in first - gets nothing
    assert _owner_of("legacy2") == ""
    c.get("/api/me", headers=_as("the-real-owner"))
    assert _owner_of("legacy2") == "the-real-owner"


def test_legacy_owner_also_claims_series_that_appear_after_their_first_sign_in(monkeypatch):
    # The claim runs whenever the person's users row is refreshed - at most
    # every LAST_SEEN_REFRESH_SECONDS since #72; 0 = on every request here.
    monkeypatch.setattr(main, "LAST_SEEN_REFRESH_SECONDS", 0)
    monkeypatch.setattr(auth, "LEGACY_OWNER_OID", "the-real-owner-2")
    c.get("/api/me", headers=_as("the-real-owner-2"))
    _insert_legacy("legacy3")
    c.get("/api/me", headers=_as("the-real-owner-2"))
    assert _owner_of("legacy3") == "the-real-owner-2"


def test_person_token_without_an_oid_is_rejected():
    token = _token("")
    assert c.get("/api/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ----------------------------------------- pipeline can't create data (#70)
def test_pipeline_cannot_create_a_series():
    r = c.post("/api/series", json={"data": {"name": "by pipeline"}}, headers=_pipeline_headers())
    assert r.status_code == 403


def test_pipeline_cannot_import():
    r = c.post("/api/import", json={"series": {"name": "x"}}, headers=_pipeline_headers())
    assert r.status_code == 403


def test_pipeline_cannot_file_feedback():
    r = c.post("/api/feedback", json={"kind": "issue", "title": "abc", "description": "d"}, headers=_pipeline_headers())
    assert r.status_code == 403


def test_pipeline_cannot_list_users():
    assert c.get("/api/users", headers=_pipeline_headers()).status_code == 403


def test_pipeline_cannot_touch_membership():
    sid = c.post("/api/series", json={"data": {"name": "members"}}, headers=_as(OWNER_OID)).json()["id"]
    assert c.delete(f"/api/series/{sid}/members/{EDITOR_OID}", headers=_pipeline_headers()).status_code == 403
    assert c.put(f"/api/series/{sid}/members/{EDITOR_OID}", json={"role": "viewer"}, headers=_pipeline_headers()).status_code == 403


def test_pipeline_cannot_act_as_owner_of_an_ownerless_series():
    """Regression for the class of bug where an identity with an empty oid
    compares equal to owner_oid == '' (pre-auth data)."""
    _insert_legacy("legacy-pipe")
    r = c.delete(f"/api/series/legacy-pipe/members/{EDITOR_OID}", headers=_pipeline_headers())
    assert r.status_code == 403
    assert c.delete("/api/series/legacy-pipe", headers=_pipeline_headers()).status_code == 403


def test_pipeline_can_still_read_series_it_could_before():
    sid = c.post("/api/series", json={"data": {"name": "readable"}}, headers=_as(OWNER_OID)).json()["id"]
    assert c.get(f"/api/series/{sid}/bundle", headers=_pipeline_headers()).status_code == 200


# --------------------------------------- import name leak (#70)
def test_import_does_not_reveal_other_peoples_series_names():
    c.post("/api/series", json={"data": {"name": "Secret Project"}}, headers=_as(OWNER_OID))
    r = c.post("/api/import", json={"series": {"name": "Secret Project"}}, headers=_as(OUTSIDER_OID)).json()
    assert r["name"] == "Secret Project"  # no "(imported)" suffix: the outsider can't see the owner's series


def test_import_still_suffixes_a_collision_with_your_own_series():
    c.post("/api/series", json={"data": {"name": "Mine"}}, headers=_as(OWNER_OID))
    r = c.post("/api/import", json={"series": {"name": "Mine"}}, headers=_as(OWNER_OID)).json()
    assert r["name"] == "Mine (imported)"


# ------------------------------------------------------------ users listing
def _users(oid):
    return {u["oid"] for u in c.get("/api/users", headers=_as(oid)).json()}


def test_list_users_includes_the_caller():
    oid = "listed-user-oid"
    c.get("/api/me", headers=_as(oid))
    assert oid in _users(oid)


def test_list_users_hides_strangers_with_no_shared_series():
    """#88: with open signup the users table holds strangers."""
    a, b = "lonely-a", "lonely-b"
    for oid in (a, b):
        c.get("/api/me", headers=_as(oid))
    c.post("/api/series", json={"data": {"name": "A's"}}, headers=_as(a))
    c.post("/api/series", json={"data": {"name": "B's"}}, headers=_as(b))
    assert b not in _users(a)
    assert a not in _users(b)


def test_list_users_shows_everyone_on_a_shared_series(series):
    # owner sees members; members see the owner AND each other
    assert {EDITOR_OID, VIEWER_OID} <= _users(OWNER_OID)
    assert {OWNER_OID, VIEWER_OID} <= _users(EDITOR_OID)
    assert {OWNER_OID, EDITOR_OID} <= _users(VIEWER_OID)


def test_list_users_after_sharing_they_see_each_other_and_after_leaving_they_dont():
    a, b = "contact-a", "contact-b"
    for oid in (a, b):
        c.get("/api/me", headers=_as(oid))
    sid = c.post("/api/series", json={"data": {"name": "Contacts"}}, headers=_as(a)).json()["id"]
    assert b not in _users(a)
    c.put(f"/api/series/{sid}/members/{b}", json={"role": "viewer"}, headers=_as(a))
    assert b in _users(a) and a in _users(b)
    c.delete(f"/api/series/{sid}/members/{b}", headers=_as(a))
    assert b not in _users(a) and a not in _users(b)


def test_list_users_does_not_leak_through_a_series_you_cant_access(series):
    assert OWNER_OID not in _users(OUTSIDER_OID)
    assert EDITOR_OID not in _users(OUTSIDER_OID)


def test_list_users_in_none_mode_still_lists_everyone(monkeypatch):
    c.get("/api/me", headers=_as("none-mode-someone"))
    monkeypatch.setattr(auth, "AUTH_MODE", "none")
    oids = {u["oid"] for u in c.get("/api/users").json()}
    assert "none-mode-someone" in oids


# ------------------------------------------------------------ user lookup (#88)
def test_lookup_finds_a_stranger_by_exact_email():
    target = "lookup-target"
    c.get("/api/me", headers=_as(target))
    r = c.get("/api/users/lookup", params={"email": f"{target}@example.com"}, headers=_as("lookup-caller"))
    assert r.status_code == 200
    assert r.json()["oid"] == target


def test_lookup_email_is_case_insensitive_and_trimmed():
    target = "lookup-case"
    c.get("/api/me", headers=_as(target))
    r = c.get("/api/users/lookup", params={"email": f"  {target.upper()}@Example.COM "}, headers=_as("lookup-caller"))
    assert r.status_code == 200 and r.json()["oid"] == target


def test_lookup_by_exact_oid():
    target = "lookup-by-oid"
    c.get("/api/me", headers=_as(target))
    r = c.get("/api/users/lookup", params={"oid": target}, headers=_as("lookup-caller"))
    assert r.status_code == 200 and r.json()["email"] == f"{target}@example.com"


@pytest.mark.parametrize("probe", ["lookup-par", "%", "_", "%@example.com", "lookup-par%", "lookup-par*", "@example.com"])
def test_lookup_does_not_enumerate_by_partial_or_wildcard(probe):
    c.get("/api/me", headers=_as("lookup-partial"))
    r = c.get("/api/users/lookup", params={"email": probe}, headers=_as("lookup-caller"))
    assert r.status_code == 404


def test_lookup_oid_is_exact_not_a_wildcard():
    c.get("/api/me", headers=_as("lookup-oid-x"))
    assert c.get("/api/users/lookup", params={"oid": "%"}, headers=_as("lookup-caller")).status_code == 404
    assert c.get("/api/users/lookup", params={"oid": "lookup-oid"}, headers=_as("lookup-caller")).status_code == 404


def test_lookup_needs_exactly_one_of_email_or_oid():
    h = _as("lookup-caller")
    assert c.get("/api/users/lookup", headers=h).status_code == 400
    assert c.get("/api/users/lookup", params={"email": "a@b.c", "oid": "x"}, headers=h).status_code == 400
    assert c.get("/api/users/lookup", params={"email": ""}, headers=h).status_code == 400
    assert c.get("/api/users/lookup", params={"email": "   "}, headers=h).status_code == 400


def test_lookup_blank_email_does_not_match_accounts_with_no_email():
    c.get("/api/me", headers=_as("lookup-no-email", preferred_username=""))
    r = c.get("/api/users/lookup", params={"email": "  "}, headers=_as("lookup-caller"))
    assert r.status_code == 400


def test_lookup_duplicate_email_is_409_and_oid_still_works():
    shared = "dupe@example.com"
    for oid in ("dupe-guest", "dupe-member"):
        c.get("/api/me", headers=_as(oid, preferred_username=shared))
    h = _as("lookup-caller")
    assert c.get("/api/users/lookup", params={"email": shared}, headers=h).status_code == 409
    assert c.get("/api/users/lookup", params={"oid": "dupe-guest"}, headers=h).json()["email"] == shared


def test_lookup_unknown_email_is_404():
    r = c.get("/api/users/lookup", params={"email": "nobody@nowhere.example"}, headers=_as("lookup-caller"))
    assert r.status_code == 404


def test_pipeline_cannot_lookup_users():
    r = c.get("/api/users/lookup", params={"email": "x@example.com"}, headers=_pipeline_headers())
    assert r.status_code == 403


def test_lookup_then_share_with_a_stranger():
    """The flow the picker uses: find by email, then PUT the member."""
    owner, stranger = "flow-owner", "flow-stranger"
    for oid in (owner, stranger):
        c.get("/api/me", headers=_as(oid))
    sid = c.post("/api/series", json={"data": {"name": "Flow"}}, headers=_as(owner)).json()["id"]
    found = c.get("/api/users/lookup", params={"email": f"{stranger}@example.com"}, headers=_as(owner)).json()
    r = c.put(f"/api/series/{sid}/members/{found['oid']}", json={"role": "editor"}, headers=_as(owner))
    assert r.status_code == 200
    assert stranger in _users(owner)


# --------------------------------------- #12's exact scenario, with real people
def test_two_real_people_editing_the_same_record_the_second_gets_409(series):
    ch = c.post(f"/api/series/{series['id']}/characters",
                json={"data": {"name": "A"}}, headers=_as(OWNER_OID)).json()
    # Both the owner and the editor load the record (same version)...
    # ...the owner saves first...
    c.put(f"/api/series/{series['id']}/characters/{ch['id']}",
          json={"data": {"name": "Owner's edit"}, "version": ch["version"]}, headers=_as(OWNER_OID))
    # ...then the editor's save, still holding the version they loaded, conflicts.
    r = c.put(f"/api/series/{series['id']}/characters/{ch['id']}",
              json={"data": {"name": "Editor's edit"}, "version": ch["version"]}, headers=_as(EDITOR_OID))
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["current"]["name"] == "Owner's edit"
    assert detail["updated_by"] == OWNER_OID  # the fake token's `name` claim, a real display name
