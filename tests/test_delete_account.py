"""
#86: delete my account and data (right to erasure), and GET /api/me/export.
Runs in AUTH_MODE=entra with locally signed tokens, like test_admin_users.py.
"""
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
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

TENANT = "77777777-7777-7777-7777-777777777777"
CLIENT_ID = "66666666-6666-6666-6666-666666666666"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"
ADMIN = "del-admin"

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
    monkeypatch.setattr(main, "_write_limiter", None)


def _bearer(**claims):
    now = int(time.time())
    payload = {"iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now}
    payload.update(claims)
    return {"Authorization": f"Bearer {jwt.encode(payload, _private_pem, algorithm='RS256')}"}


def _as(oid):
    return _bearer(oid=oid, scp="access_as_user", name=f"Name {oid}", preferred_username=f"{oid}@example.com")


def _delete(oid, confirm=None):
    return c.request("DELETE", "/api/me", json={"confirm": confirm if confirm is not None else f"{oid}@example.com"},
                     headers=_as(oid))


def _count(sql, *args):
    with main.db() as con:
        return con.execute(sql, args).fetchone()[0]


def _series(oid, name="S"):
    return c.post("/api/series", json={"data": {"name": name}}, headers=_as(oid)).json()["id"]


def _char(oid, sid, name="Ann"):
    r = c.post(f"/api/series/{sid}/characters", json={"data": {"name": name}}, headers=_as(oid))
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_deletes_account_owned_series_records_and_members():
    me, friend = "del-a1", "del-a1-friend"
    c.get("/api/me", headers=_as(friend))
    sid = _series(me)
    _char(me, sid)
    c.put(f"/api/series/{sid}/members/{friend}", json={"role": "editor"}, headers=_as(me))

    r = _delete(me)
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "series_deleted": 1, "shared_with": 1, "memberships_left": 0}
    assert _count("SELECT COUNT(*) FROM users WHERE oid=?", me) == 0
    assert _count("SELECT COUNT(*) FROM series WHERE id=?", sid) == 0
    assert _count("SELECT COUNT(*) FROM records WHERE series_id=?", sid) == 0
    assert _count("SELECT COUNT(*) FROM members WHERE series_id=?", sid) == 0
    assert c.get(f"/api/series/{sid}/bundle", headers=_as(friend)).status_code == 404


def test_tombstone_holds_only_oid_and_time():
    _series("del-t1")
    _delete("del-t1")
    with main.db() as con:
        row = con.execute("SELECT * FROM deleted_users WHERE oid='del-t1'").fetchone()
    assert set(row.keys()) == {"oid", "deleted_at"} and row["deleted_at"] > 0


def test_other_peoples_data_is_untouched():
    me, other = "del-b1", "del-b2"
    _series(me, "Mine")
    theirs = _series(other, "Theirs")
    cid = _char(other, theirs)
    _delete(me)
    assert c.get(f"/api/series/{theirs}/bundle", headers=_as(other)).status_code == 200
    assert _count("SELECT COUNT(*) FROM records WHERE id=?", cid) == 1
    assert _count("SELECT COUNT(*) FROM users WHERE oid=?", other) == 1


def test_leaves_memberships_and_replaces_name_on_others_series():
    me, owner = "del-c1", "del-c2"
    sid = _series(owner)
    c.get("/api/me", headers=_as(me))
    c.put(f"/api/series/{sid}/members/{me}", json={"role": "editor"}, headers=_as(owner))
    cid = _char(me, sid, "Written by me")
    before = c.get(f"/api/series/{sid}/bundle", headers=_as(owner)).json()
    assert next(x for x in before["characters"] if x["id"] == cid)["created_by"] == me

    r = _delete(me)
    assert r.json()["memberships_left"] == 1
    assert _count("SELECT COUNT(*) FROM members WHERE oid=?", me) == 0
    b = c.get(f"/api/series/{sid}/bundle", headers=_as(owner)).json()
    ch = next(x for x in b["characters"] if x["id"] == cid)
    assert ch["created_by"] == ch["updated_by"] == main.DELETED_USER_OID
    assert b["people"][main.DELETED_USER_OID] == "Deleted user"
    assert me not in str(b)


def test_blocked_row_survives_deletion_and_user_stays_blocked():
    """delete_account_data itself must leave blocked_users alone, so
    deleting and signing in again is no way round a block (#82)."""
    oid = "del-d2"
    c.get("/api/me", headers=_as(oid))
    with main.db(write=True) as con:
        con.execute("INSERT INTO blocked_users (oid, blocked_at, blocked_by) VALUES (?,?,?)", (oid, 1, ADMIN))
        main.delete_account_data(con, oid)
    assert _count("SELECT COUNT(*) FROM blocked_users WHERE oid=?", oid) == 1
    assert _count("SELECT COUNT(*) FROM users WHERE oid=?", oid) == 0
    main._seen_users.clear()
    assert c.get("/api/me", headers=_as(oid)).status_code == 403


def test_blocked_person_cannot_delete_and_sign_in_again():
    oid = "del-d1"
    c.get("/api/me", headers=_as(oid))
    assert c.put(f"/api/admin/users/{oid}/block", json={}, headers=_as(ADMIN)).status_code == 200
    assert _delete(oid).status_code == 403
    assert c.get("/api/me", headers=_as(oid)).status_code == 403


def test_can_sign_up_again_fresh():
    oid = "del-e1"
    _char(oid, _series(oid))
    assert _delete(oid).status_code == 200
    r = c.get("/api/series", headers=_as(oid))
    assert r.status_code == 200 and r.json() == []
    assert _count("SELECT COUNT(*) FROM users WHERE oid=?", oid) == 1
    assert _count("SELECT COUNT(*) FROM deleted_users WHERE oid=?", oid) == 1


def test_seen_cache_entry_is_evicted():
    oid = "del-f1"
    c.get("/api/me", headers=_as(oid))
    assert (main.DB_PATH, oid) in main._seen_users
    _delete(oid)
    assert (main.DB_PATH, oid) not in main._seen_users


def test_confirmation_is_required():
    oid = "del-g1"
    sid = _series(oid)
    for bad in ("", "someone-else@example.com", "   "):
        assert _delete(oid, bad).status_code == 400
    assert c.request("DELETE", "/api/me", headers=_as(oid)).status_code == 422  # no body at all
    assert _count("SELECT COUNT(*) FROM series WHERE id=?", sid) == 1
    assert _delete(oid, "  DEL-G1@Example.com ").status_code == 200  # case-insensitive email


def test_the_word_delete_also_confirms():
    oid = "del-g2"
    c.get("/api/me", headers=_as(oid))
    assert _delete(oid, "DELETE").status_code == 200


def test_requires_sign_in_and_refuses_the_pipeline():
    assert c.request("DELETE", "/api/me", json={"confirm": "x"}).status_code == 401
    h = _bearer(oid="del-pipe", roles=["Pipeline.Read"])
    assert c.request("DELETE", "/api/me", json={"confirm": "DELETE"}, headers=h).status_code == 403
    assert c.get("/api/me/export", headers=h).status_code == 403


def test_refused_outside_entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "none")
    sid = c.post("/api/series", json={"data": {"name": "Shared bible"}}).json()["id"]
    assert c.request("DELETE", "/api/me", json={"confirm": "DELETE"}).status_code == 400
    assert _count("SELECT COUNT(*) FROM series WHERE id=?", sid) == 1


def test_write_rate_limit_applies(monkeypatch):
    from app import github_feedback as feedback
    oid = "del-rl"
    c.get("/api/me", headers=_as(oid))
    monkeypatch.setattr(main, "_write_limiter", feedback._RateLimiter(1, 60))
    _series(oid)  # uses the one allowed write
    assert _delete(oid).status_code == 429


# ---------------------------------------------------------------- export
def test_export_contains_only_the_callers_data():
    me, other = "del-x1", "del-x2"
    mine, theirs = _series(me, "Mine"), _series(other, "Theirs")
    _char(me, mine, "Mine Char")
    _char(other, theirs, "Their Char")
    c.put(f"/api/series/{theirs}/members/{me}", json={"role": "viewer"}, headers=_as(other))

    r = c.get("/api/me/export", headers=_as(me))
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    d = r.json()
    assert d["account"]["oid"] == me and d["account"]["email"] == f"{me}@example.com"
    assert d["memberships"] == [{"series_id": theirs, "role": "viewer"}]
    assert [s["series"]["id"] for s in d["series"]] == [mine]
    assert d["series"][0]["characters"][0]["name"] == "Mine Char"
    assert "Their Char" not in r.text
    assert c.get("/api/me/export").status_code == 401


# ---------------------------------------------------------------- restore
def test_apply_tombstones_reapplies_deletions_to_a_restored_database(tmp_path):
    """A backup taken before the deletion has the user's data; the script
    erases it again from the tombstones carried over from the live DB."""
    oid = "del-r1"
    sid = _series(oid)
    _char(oid, sid)
    backup = tmp_path / "restored.db"
    with main.db() as con:  # the "backup": taken while the user still exists
        dest = sqlite3.connect(backup)
        con.backup(dest)
        dest.close()
    _delete(oid)
    with main.db() as live:  # carry the live tombstones across, as RESTORE.md does
        rows = [tuple(r) for r in live.execute("SELECT oid, deleted_at FROM deleted_users WHERE oid=?", (oid,))]
    con = sqlite3.connect(backup)
    assert con.execute("SELECT COUNT(*) FROM series WHERE id=?", (sid,)).fetchone()[0] == 1
    con.executemany("INSERT OR REPLACE INTO deleted_users VALUES (?,?)", rows)
    con.commit()
    con.close()

    script = Path(__file__).resolve().parent.parent / "scripts" / "apply_tombstones.py"
    out = subprocess.run([sys.executable, str(script), str(backup)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    con = sqlite3.connect(backup)
    assert con.execute("SELECT COUNT(*) FROM series WHERE id=?", (sid,)).fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM users WHERE oid=?", (oid,)).fetchone()[0] == 0
    con.close()


def test_apply_tombstones_spares_someone_who_signed_up_again(tmp_path):
    """Deleted, then signed up again, then a backup was taken: that backup's
    account is the new one and must survive the re-application."""
    oid = "del-r2"
    _delete(oid)
    time.sleep(0.01)
    sid = _series(oid)  # signs up again
    backup = tmp_path / "restored.db"
    with main.db() as con:
        dest = sqlite3.connect(backup)
        con.backup(dest)
        dest.close()
    script = Path(__file__).resolve().parent.parent / "scripts" / "apply_tombstones.py"
    out = subprocess.run([sys.executable, str(script), str(backup)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    con = sqlite3.connect(backup)
    assert con.execute("SELECT COUNT(*) FROM series WHERE id=?", (sid,)).fetchone()[0] == 1
    con.close()
