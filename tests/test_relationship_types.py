"""
#63: relationship_types is a per-series list of relationship-type
suggestions, editable the same way character_fields already was (see
app/models.py's SeriesIn). The relationship form's type field was, and
remains, a free-text input either way - this only customizes what
<datalist> suggests while typing, never what can actually be saved.
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


def test_relationship_types_defaults_to_empty_list():
    s = c.post("/api/series", json={"data": {"name": "T"}}).json()
    assert s["relationship_types"] == []


def test_relationship_types_round_trips_through_create():
    s = c.post("/api/series", json={
        "data": {"name": "T", "relationship_types": ["married to", "enemy of"]},
    }).json()
    assert s["relationship_types"] == ["married to", "enemy of"]
    fetched = c.get(f"/api/series/{s['id']}/bundle").json()["series"]
    assert fetched["relationship_types"] == ["married to", "enemy of"]


def test_relationship_types_round_trips_through_update():
    s = c.post("/api/series", json={"data": {"name": "T"}}).json()
    r = c.put(f"/api/series/{s['id']}", json={
        "data": {"relationship_types": ["enemy of", "rival of"]},
        "version": s["version"],
    })
    assert r.status_code == 200
    assert r.json()["relationship_types"] == ["enemy of", "rival of"]


def test_relationship_types_is_per_series_not_global():
    a = c.post("/api/series", json={"data": {"name": "A", "relationship_types": ["enemy of"]}}).json()
    b = c.post("/api/series", json={"data": {"name": "B"}}).json()
    assert a["relationship_types"] == ["enemy of"]
    assert b["relationship_types"] == []


def test_relationship_type_on_an_actual_relationship_record_stays_free_text():
    """The point of #63 - a type not on any suggestion list was always
    saveable, this just makes it easy to get suggested next time."""
    s = c.post("/api/series", json={"data": {"name": "T"}}).json()
    base = f"/api/series/{s['id']}"
    a = c.post(f"{base}/characters", json={"data": {"name": "A"}}).json()
    b = c.post(f"{base}/characters", json={"data": {"name": "B"}}).json()
    r = c.post(f"{base}/relationships", json={"data": {"from": a["id"], "to": b["id"], "type": "enemy of"}})
    assert r.status_code == 200
    assert r.json()["type"] == "enemy of"
