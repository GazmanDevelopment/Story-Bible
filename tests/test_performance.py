"""
#72: performance - the users row isn't rewritten on every request, series
are filtered in SQL, GET /bundle has an ETag (and a bodyless 304), the series
picker has a slim summary form, and big JSON responses are gzipped.
"""
import gzip
import json
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
TENANT = "77777777-7777-7777-7777-777777777777"
CLIENT_ID = "88888888-8888-8888-8888-888888888888"
ISSUER = f"https://login.microsoftonline.com/{TENANT}/v2.0"

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_pem = _key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
_public_pem = _key.public_key().public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
)


@pytest.fixture(autouse=True)
def entra_mode(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    monkeypatch.setattr(auth, "ENTRA_TENANT_ID", TENANT)
    monkeypatch.setattr(auth, "ENTRA_CLIENT_ID", CLIENT_ID)
    monkeypatch.setattr(auth, "ALLOWED_OIDS", set())
    monkeypatch.setattr(auth, "_jwks_client", SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=_public_pem)))
    main._seen_users.clear()


def _as(oid, name=None):
    now = int(time.time())
    token = jwt.encode({
        "iss": ISSUER, "aud": CLIENT_ID, "exp": now + 3600, "iat": now, "nbf": now,
        "oid": oid, "scp": "access_as_user", "name": name or oid, "preferred_username": f"{oid}@example.com",
    }, _private_pem, algorithm="RS256")
    return {"Authorization": f"Bearer {token}"}


def _last_seen(oid):
    with main.db() as con:
        row = con.execute("SELECT last_seen FROM users WHERE oid=?", (oid,)).fetchone()
    return row["last_seen"] if row else None


# --------------------------------------------- users row writes (F8)
def test_a_repeat_request_does_not_touch_the_users_row(monkeypatch):
    calls = []
    real = main.upsert_user
    monkeypatch.setattr(main, "upsert_user", lambda con, user: calls.append(user.oid) or real(con, user))
    for _ in range(10):
        assert c.get("/api/me", headers=_as("perf-a")).status_code == 200
    assert calls == ["perf-a"]  # once, on the first request


def test_plain_reads_no_longer_bump_last_seen():
    c.get("/api/me", headers=_as("perf-b"))
    first = _last_seen("perf-b")
    time.sleep(0.02)
    c.get("/api/series", headers=_as("perf-b"))
    assert _last_seen("perf-b") == first


def test_the_row_is_refreshed_once_the_interval_has_passed(monkeypatch):
    c.get("/api/me", headers=_as("perf-c"))
    first = _last_seen("perf-c")
    key = (main.DB_PATH, "perf-c")
    email, name, tid, stamp = main._seen_users[key]
    main._seen_users[key] = (email, name, tid, stamp - main.LAST_SEEN_REFRESH_SECONDS - 1)
    time.sleep(0.02)
    c.get("/api/me", headers=_as("perf-c"))
    assert _last_seen("perf-c") > first


def test_a_changed_display_name_is_written_immediately():
    c.get("/api/me", headers=_as("perf-d", name="Old Name"))
    c.get("/api/me", headers=_as("perf-d", name="New Name"))
    with main.db() as con:
        assert con.execute("SELECT display_name FROM users WHERE oid='perf-d'").fetchone()["display_name"] == "New Name"


def test_a_new_person_exists_straight_away_so_they_can_be_shared_with():
    """put_member refuses people who have never signed in; the cache must not
    delay that first insert."""
    owner = _as("perf-owner")
    sid = c.post("/api/series", json={"data": {"name": "share"}}, headers=owner).json()["id"]
    c.get("/api/me", headers=_as("perf-friend"))
    r = c.put(f"/api/series/{sid}/members/perf-friend", json={"role": "viewer"}, headers=owner)
    assert r.status_code == 200, r.text


def test_the_cache_is_per_database(monkeypatch):
    c.get("/api/me", headers=_as("perf-e"))
    assert (main.DB_PATH, "perf-e") in main._seen_users
    assert ("some-other.db", "perf-e") not in main._seen_users


# ------------------------------------ series filtered in SQL (P2)
def test_list_series_shows_owned_and_shared_only():
    alice, bob, carol = _as("perf-alice"), _as("perf-bob"), _as("perf-carol")
    mine = c.post("/api/series", json={"data": {"name": "Alice's"}}, headers=alice).json()["id"]
    theirs = c.post("/api/series", json={"data": {"name": "Bob's"}}, headers=bob).json()["id"]
    c.get("/api/me", headers=alice)
    assert c.put(f"/api/series/{theirs}/members/perf-alice", json={"role": "viewer"}, headers=bob).status_code == 200
    visible = {s["id"] for s in c.get("/api/series", headers=alice).json()}
    assert {mine, theirs} <= visible
    carol_sees = {s["id"] for s in c.get("/api/series", headers=carol).json()}
    assert mine not in carol_sees and theirs not in carol_sees


def test_list_series_query_returns_only_visible_rows_from_sqlite():
    """The filter is in the SQL, not applied after loading everything."""
    c.post("/api/series", json={"data": {"name": "hidden from dan"}}, headers=_as("perf-erin"))
    dan = auth.CurrentUser(oid="perf-dan", email="", display_name="d")
    with main.db() as con:
        rows = main.accessible_series_rows(con, dan)
        total = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
    assert rows == [] and total > 0


def test_summary_is_slim_and_consistent_with_the_full_list():
    h = _as("perf-frank")
    c.post("/api/series", json={"data": {"name": "b series", "description": "d" * 100_000}}, headers=h)
    c.post("/api/series", json={"data": {"name": "A series", "description": "e" * 100_000}}, headers=h)
    full = c.get("/api/series", headers=h)
    summ = c.get("/api/series?summary=true", headers=h)
    assert summ.status_code == 200
    assert len(summ.content) < len(full.content) / 50
    assert [s["id"] for s in summ.json()] == [s["id"] for s in full.json()]
    assert [s["name"] for s in summ.json()] == ["A series", "b series"]  # still sorted case-insensitively
    assert set(summ.json()[0]) == {"id", "name", "updated", "version", "owner_oid", "created_by", "updated_by"}
    assert "description" not in summ.text


def test_the_default_list_is_unchanged_for_other_api_callers():
    h = _as("perf-gina")
    c.post("/api/series", json={"data": {"name": "full", "description": "kept"}}, headers=h)
    only = c.get("/api/series", headers=h).json()[0]
    assert only["description"] == "kept" and "character_fields" in only


# --------------------------------------------- bundle ETag (P1b)
def _series_with(h, n=3):
    sid = c.post("/api/series", json={"data": {"name": "etag"}}, headers=h).json()["id"]
    ids = [c.post(f"/api/series/{sid}/characters", json={"data": {"name": f"c{i}"}}, headers=h).json() for i in range(n)]
    return sid, ids


def test_bundle_sends_a_weak_etag_and_honours_if_none_match():
    h = _as("perf-h")
    sid, _ = _series_with(h)
    first = c.get(f"/api/series/{sid}/bundle", headers=h)
    etag = first.headers["etag"]
    assert etag.startswith('W/"') and first.status_code == 200
    again = c.get(f"/api/series/{sid}/bundle", headers={**h, "If-None-Match": etag})
    assert again.status_code == 304 and again.content == b"" and again.headers["etag"] == etag
    assert again.headers["cache-control"] == "no-store"
    assert again.headers["x-content-type-options"] == "nosniff"  # 304s get the security headers too


def test_the_etag_is_stable_when_nothing_changed():
    h = _as("perf-i")
    sid, _ = _series_with(h)
    assert c.get(f"/api/series/{sid}/bundle", headers=h).headers["etag"] == c.get(f"/api/series/{sid}/bundle", headers=h).headers["etag"]


def _etag(h, sid):
    return c.get(f"/api/series/{sid}/bundle", headers=h).headers["etag"]


def test_every_kind_of_change_changes_the_etag():
    h = _as("perf-j")
    sid, chars = _series_with(h)
    seen = {_etag(h, sid)}

    def changed():
        e = _etag(h, sid)
        assert e not in seen, "the bundle changed but the ETag did not"
        seen.add(e)

    r = c.put(f"/api/series/{sid}/characters/{chars[0]['id']}", json={"data": {"name": "edited"}, "version": chars[0]["version"]}, headers=h)
    assert r.status_code == 200; changed()
    c.post(f"/api/series/{sid}/locations", json={"data": {"name": "new"}}, headers=h); changed()
    cur = c.get(f"/api/series/{sid}/bundle", headers=h).json()["series"]
    c.put(f"/api/series/{sid}", json={"data": {"name": "renamed"}, "version": cur["version"]}, headers=h); changed()
    c.delete(f"/api/series/{sid}/characters/{chars[1]['id']}", headers=h); changed()


def test_a_cascade_from_a_delete_changes_the_etag():
    h = _as("perf-k")
    sid, chars = _series_with(h, 1)
    loc = c.post(f"/api/series/{sid}/locations", json={"data": {"name": "L", "character_ids": [chars[0]["id"]]}}, headers=h).json()
    before = _etag(h, sid)
    c.delete(f"/api/series/{sid}/characters/{chars[0]['id']}", headers=h)
    after = c.get(f"/api/series/{sid}/bundle", headers=h)
    assert after.headers["etag"] != before
    assert after.json()["locations"][0]["character_ids"] == []


def test_a_renamed_person_changes_the_etag_because_people_is_in_the_body():
    h = _as("perf-l", name="Before")
    sid, _ = _series_with(h)
    before = _etag(h, sid)
    renamed = _as("perf-l", name="After")  # the same person, signing in with a new display name
    assert _etag(renamed, sid) != before


def test_the_etag_differs_between_series():
    h = _as("perf-m")
    a, _ = _series_with(h)
    b, _ = _series_with(h)
    assert _etag(h, a) != _etag(h, b)


def test_multiple_and_weak_prefixed_validators_match():
    h = _as("perf-n")
    sid, _ = _series_with(h)
    etag = _etag(h, sid)
    for header in (etag, etag.removeprefix("W/"), f'"nope", {etag}', f"W/\"nope\" , {etag} "):
        assert c.get(f"/api/series/{sid}/bundle", headers={**h, "If-None-Match": header}).status_code == 304, header
    for header in ('"nope"', "", "garbage"):
        assert c.get(f"/api/series/{sid}/bundle", headers={**h, "If-None-Match": header}).status_code == 200, header
    # "*" means "if it exists at all" (RFC 9110) - it does, and access was already checked
    assert c.get(f"/api/series/{sid}/bundle", headers={**h, "If-None-Match": "*"}).status_code == 304


def test_a_valid_etag_does_not_bypass_access_control():
    owner, stranger = _as("perf-o"), _as("perf-p")
    sid, _ = _series_with(owner)
    etag = _etag(owner, sid)
    r = c.get(f"/api/series/{sid}/bundle", headers={**stranger, "If-None-Match": etag})
    assert r.status_code == 404  # existence still hidden; the ETag doesn't leak that it is unchanged


def test_export_bundle_still_returns_the_dict_the_backup_needs():
    h = _as("perf-q")
    sid, _ = _series_with(h)
    data = main.export_bundle(sid, auth.SYSTEM_USER)
    assert data["series"]["name"] == "etag" and len(data["characters"]) == 3 and "people" in data


# --------------------------------------------------------------- gzip (P2)
def _big_bundle(h):
    sid = c.post("/api/series", json={"data": {"name": "big"}}, headers=h).json()["id"]
    for i in range(40):
        c.post(f"/api/series/{sid}/characters", json={"data": {"name": f"c{i}", "backstory": "lorem ipsum " * 400}}, headers=h)
    return sid


def test_large_json_is_gzipped_when_the_client_accepts_it():
    h = _as("perf-r")
    sid = _big_bundle(h)
    plain = c.get(f"/api/series/{sid}/bundle", headers={**h, "Accept-Encoding": "identity"})
    zipped = c.get(f"/api/series/{sid}/bundle", headers={**h, "Accept-Encoding": "gzip"})
    assert "content-encoding" not in plain.headers
    assert zipped.headers["content-encoding"] == "gzip"
    assert zipped.num_bytes_downloaded < plain.num_bytes_downloaded / 10
    assert zipped.json()["characters"][0]["name"] == plain.json()["characters"][0]["name"]
    assert "accept-encoding" in zipped.headers["vary"].lower()


def test_small_responses_are_not_compressed():
    assert "content-encoding" not in c.get("/api/health", headers={"Accept-Encoding": "gzip"}).headers


def test_static_assets_are_gzipped_too():
    r = c.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"


def test_compressed_responses_still_carry_the_security_headers():
    h = _as("perf-s")
    sid = _big_bundle(h)
    r = c.get(f"/api/series/{sid}/bundle", headers={**h, "Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in r.headers


def test_a_304_is_not_given_a_gzip_body():
    h = _as("perf-t")
    sid = _big_bundle(h)
    etag = _etag(h, sid)
    r = c.get(f"/api/series/{sid}/bundle", headers={**h, "If-None-Match": etag, "Accept-Encoding": "gzip"})
    assert r.status_code == 304 and r.num_bytes_downloaded == 0


# ------------------------------------------------ access indexes (P2)
def _index_names(con):
    return {r[1] for r in con.execute("SELECT type, name FROM sqlite_master WHERE type='index'")}


def test_the_access_query_is_answered_from_indexes():
    with main.db() as con:
        plan = " ".join(str(tuple(r)) for r in con.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM series WHERE owner_oid=? OR id IN "
            "(SELECT series_id FROM members WHERE oid=?)", ("a", "b")))
    assert "ix_series_owner" in plan and "ix_members_oid" in plan, plan


def test_the_indexes_migration_upgrades_an_existing_database(tmp_path, monkeypatch):
    import sqlite3

    from app import migrations
    con = sqlite3.connect(tmp_path / "old.db")
    con.isolation_level = None
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 4)   # a database as it was before #72
    migrations.migrate(con)
    assert "ix_series_owner" not in _index_names(con)
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", len(migrations.MIGRATIONS))
    migrations.migrate(con)
    assert {"ix_series_owner", "ix_members_oid"} <= _index_names(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == len(migrations.MIGRATIONS)
    migrations.migrate(con)  # and re-running is a no-op
    con.close()


def test_image_assets_are_not_recompressed():
    r = c.get("/assets/icon-128.png", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and "content-encoding" not in r.headers


def test_error_responses_from_the_outer_middleware_keep_their_headers_with_gzip_in_the_chain(monkeypatch):
    monkeypatch.setattr(main, "MAX_BODY_BYTES", 10)
    r = c.post("/api/series", json={"data": {"name": "longer than ten bytes"}}, headers={**_as("perf-u"), "Accept-Encoding": "gzip"})
    assert r.status_code == 413
    assert r.headers["cache-control"] == "no-store" and r.headers["x-content-type-options"] == "nosniff"


def test_import_finds_name_collisions_only_among_visible_series_using_sql():
    owner, other = _as("perf-v"), _as("perf-w")
    c.post("/api/series", json={"data": {"name": "Collide"}}, headers=owner)
    c.post("/api/series", json={"data": {"name": "Only Theirs"}}, headers=other)
    assert c.post("/api/import", json={"series": {"name": "Collide"}}, headers=owner).json()["name"] == "Collide (imported)"
    assert c.post("/api/import", json={"series": {"name": "Only Theirs"}}, headers=owner).json()["name"] == "Only Theirs"


def test_an_unrelated_user_signing_in_or_renaming_leaves_the_etag_alone():
    """#132: the validator covers only the people in the series, so another
    account appearing (or being renamed) must not force a re-download."""
    h = _as("perf-n", name="Author")
    sid, _ = _series_with(h)
    etag = _etag(h, sid)
    _as("perf-n-stranger", name="Before")
    c.get("/api/series", headers=_as("perf-n-stranger", name="Before"))  # signs the stranger in
    c.get("/api/series", headers=_as("perf-n-stranger", name="After"))   # ...and renames them
    assert _etag(h, sid) == etag
    again = c.get(f"/api/series/{sid}/bundle", headers={**h, "If-None-Match": etag})
    assert again.status_code == 304


def test_renaming_a_record_editor_changes_the_etag():
    h = _as("perf-o", name="Owner")
    sid, _ = _series_with(h)
    editor = _as("perf-o-editor", name="Ed")
    c.get("/api/series", headers=editor)  # sign the editor in so they can be added
    c.put(f"/api/series/{sid}/members/perf-o-editor", json={"role": "editor"}, headers=h)
    loc = c.post(f"/api/series/{sid}/locations", json={"data": {"name": "L"}}, headers=editor)
    assert loc.status_code == 200, loc.text
    before = _etag(h, sid)
    c.get("/api/series", headers=_as("perf-o-editor", name="Edward"))
    assert _etag(h, sid) != before
