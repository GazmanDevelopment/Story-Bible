"""
#12: optimistic concurrency (version + 409/428) and audit fields
(created_by/updated_by, the bundle's people map).
"""
import os
import tempfile

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)


def _series():
    return c.post("/api/series", json={"data": {"name": "Concurrency test"}}).json()


def test_new_series_starts_at_version_1_with_audit_fields():
    s = _series()
    assert s["version"] == 1
    assert s["created_by"] == s["updated_by"] == "local"  # AUTH_MODE=none's synthetic user


def test_new_record_starts_at_version_1():
    s = _series()
    r = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}}).json()
    assert r["version"] == 1
    assert r["created_by"] == r["updated_by"] == "local"


def test_update_without_version_is_428():
    s = _series()
    r = c.put(f"/api/series/{s['id']}", json={"data": {"name": "Renamed"}})
    assert r.status_code == 428


def test_record_update_without_version_is_428():
    s = _series()
    ch = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}}).json()
    r = c.put(f"/api/series/{s['id']}/characters/{ch['id']}", json={"data": {"name": "B"}})
    assert r.status_code == 428


def test_update_with_correct_version_succeeds_and_bumps_it():
    s = _series()
    r = c.put(f"/api/series/{s['id']}", json={"data": {"name": "Renamed"}, "version": s["version"]}).json()
    assert r["version"] == 2
    assert r["name"] == "Renamed"


def test_record_update_with_correct_version_succeeds_and_bumps_it():
    s = _series()
    ch = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}}).json()
    r = c.put(f"/api/series/{s['id']}/characters/{ch['id']}",
              json={"data": {"name": "B"}, "version": ch["version"]}).json()
    assert r["version"] == 2
    assert r["name"] == "B"


def test_stale_version_is_409_series():
    s = _series()
    # First writer updates successfully, bumping to version 2.
    c.put(f"/api/series/{s['id']}", json={"data": {"name": "First"}, "version": s["version"]})
    # Second writer still has the stale version 1.
    r = c.put(f"/api/series/{s['id']}", json={"data": {"name": "Second"}, "version": s["version"]})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["error"] == "conflict"
    assert body["current"]["name"] == "First"
    assert body["current"]["version"] == 2
    assert body["updated_by"]  # a display name, not blank


def test_stale_version_is_409_record():
    s = _series()
    ch = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}}).json()
    c.put(f"/api/series/{s['id']}/characters/{ch['id']}", json={"data": {"name": "First"}, "version": ch["version"]})
    r = c.put(f"/api/series/{s['id']}/characters/{ch['id']}",
              json={"data": {"name": "Second"}, "version": ch["version"]})
    assert r.status_code == 409
    body = r.json()["detail"]
    assert body["current"]["name"] == "First"


def test_update_nonexistent_record_is_404_not_409():
    s = _series()
    r = c.put(f"/api/series/{s['id']}/characters/does-not-exist", json={"data": {"name": "X"}, "version": 1})
    assert r.status_code == 404


def test_created_by_never_changes_across_updates():
    s = _series()
    ch = c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}}).json()
    upd = c.put(f"/api/series/{s['id']}/characters/{ch['id']}",
                json={"data": {"name": "B"}, "version": ch["version"]}).json()
    assert upd["created_by"] == ch["created_by"]


def test_client_cannot_forge_version_via_data_field():
    """clean() strips version/created_by/updated_by from `data` - only the
    top-level `version` field (compared server-side) has any effect."""
    s = _series()
    ch = c.post(f"/api/series/{s['id']}/characters",
                json={"data": {"name": "A", "version": 999, "created_by": "someone-else"}}).json()
    assert ch["version"] == 1
    assert ch["created_by"] == "local"


def test_cascade_delete_cleanup_bumps_the_touched_records_version():
    s = _series()
    base = f"/api/series/{s['id']}"
    char = c.post(f"{base}/characters", json={"data": {"name": "A"}}).json()
    loc = c.post(f"{base}/locations", json={"data": {"name": "L", "character_ids": [char["id"]]}}).json()
    assert loc["version"] == 1
    c.delete(f"{base}/characters/{char['id']}")
    after = c.get(f"{base}/locations").json()[0]
    assert after["character_ids"] == []
    assert after["version"] == 2  # bumped by the cascade cleanup, not just the delete
    # a stale form (still holding version 1) trying to save now correctly conflicts
    stale = c.put(f"{base}/locations/{loc['id']}", json={"data": {"name": "L2"}, "version": 1})
    assert stale.status_code == 409


def test_bundle_includes_a_people_map():
    s = _series()
    c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}})
    bun = c.get(f"/api/series/{s['id']}/bundle").json()
    assert "people" in bun
    assert bun["people"]["local"] == "Local"  # AUTH_MODE=none's synthetic display name


def test_import_sets_version_1_and_audit_fields():
    s = _series()
    c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "A"}})
    bun = c.get(f"/api/series/{s['id']}/bundle").json()
    imp = c.post("/api/import", json=bun).json()
    b2 = c.get(f"/api/series/{imp['id']}/bundle").json()
    assert b2["series"]["version"] == 1
    assert b2["characters"][0]["version"] == 1
    assert b2["characters"][0]["created_by"] == "local"
