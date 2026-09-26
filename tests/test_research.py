"""
#43: the Research page's backend surface - it rides entirely on the
existing generic /api/series/{id}/{kind} routes (research is just one
more entry in KINDS/KIND_MODELS, see app/main.py and app/models.py), so
what's actually new here is: the extra id-cleanup-on-delete wiring for
research's own reference fields, and body being HTML that gets sanitized
server-side rather than stored/escaped as plain text.
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
    return c.post("/api/series", json={"data": {"name": "Research test"}}).json()


def test_research_is_a_known_kind():
    s = _series()
    assert c.get(f"/api/series/{s['id']}/research").status_code == 200


def test_create_read_update_delete_roundtrip():
    s = _series()
    base = f"/api/series/{s['id']}"
    ch = c.post(f"{base}/chapters", json={"data": {"number": 1, "title": "Ch 1"}}).json()
    char = c.post(f"{base}/characters", json={"data": {"name": "A"}}).json()
    loc = c.post(f"{base}/locations", json={"data": {"name": "L"}}).json()
    ev = c.post(f"{base}/events", json={"data": {"title": "E"}}).json()

    r = c.post(f"{base}/research", json={"data": {
        "title": "Carriages", "body": "<p>Some <strong>notes</strong></p>",
        "date_entered": "2024-01-02", "chapter_id": ch["id"],
        "character_ids": [char["id"]], "location_ids": [loc["id"]], "event_ids": [ev["id"]],
    }}).json()
    assert r["title"] == "Carriages"
    assert r["body"] == "<p>Some <strong>notes</strong></p>"
    assert r["date_entered"] == "2024-01-02"
    assert r["chapter_id"] == ch["id"]
    assert r["character_ids"] == [char["id"]]
    assert r["location_ids"] == [loc["id"]]
    assert r["event_ids"] == [ev["id"]]

    got = c.get(f"{base}/research").json()
    assert len(got) == 1 and got[0]["id"] == r["id"]

    upd = c.put(f"{base}/research/{r['id']}",
                json={"data": {**r, "title": "Carriages (revised)"}, "version": r["version"]}).json()
    assert upd["title"] == "Carriages (revised)"

    assert c.delete(f"{base}/research/{r['id']}").status_code == 200
    assert c.get(f"{base}/research").json() == []


def test_series_isolation():
    s1, s2 = _series(), _series()
    c.post(f"/api/series/{s1['id']}/research", json={"data": {"title": "Only in s1"}})
    assert c.get(f"/api/series/{s2['id']}/research").json() == []


def test_deleting_a_linked_character_clears_the_reference_not_the_entry():
    s = _series()
    base = f"/api/series/{s['id']}"
    char = c.post(f"{base}/characters", json={"data": {"name": "A"}}).json()
    r = c.post(f"{base}/research", json={"data": {"title": "R", "character_ids": [char["id"]]}}).json()
    c.delete(f"{base}/characters/{char['id']}")
    got = c.get(f"{base}/research").json()[0]
    assert got["character_ids"] == []
    assert got["id"] == r["id"]  # the research entry itself survives


def test_deleting_a_linked_location_clears_the_reference():
    s = _series()
    base = f"/api/series/{s['id']}"
    loc = c.post(f"{base}/locations", json={"data": {"name": "L"}}).json()
    r = c.post(f"{base}/research", json={"data": {"title": "R", "location_ids": [loc["id"]]}}).json()
    c.delete(f"{base}/locations/{loc['id']}")
    got = c.get(f"{base}/research").json()[0]
    assert got["location_ids"] == []
    assert got["id"] == r["id"]  # the research entry itself survives


def test_deleting_a_linked_event_clears_the_reference():
    s = _series()
    base = f"/api/series/{s['id']}"
    ev = c.post(f"{base}/events", json={"data": {"title": "E"}}).json()
    r = c.post(f"{base}/research", json={"data": {"title": "R", "event_ids": [ev["id"]]}}).json()
    c.delete(f"{base}/events/{ev['id']}")
    got = c.get(f"{base}/research").json()[0]
    assert got["event_ids"] == []
    assert got["id"] == r["id"]


def test_deleting_a_linked_chapter_clears_the_reference():
    s = _series()
    base = f"/api/series/{s['id']}"
    ch = c.post(f"{base}/chapters", json={"data": {"number": 1, "title": "Ch 1"}}).json()
    r = c.post(f"{base}/research", json={"data": {"title": "R", "chapter_id": ch["id"]}}).json()
    c.delete(f"{base}/chapters/{ch['id']}")
    got = c.get(f"{base}/research").json()[0]
    assert got["chapter_id"] == ""
    assert got["id"] == r["id"]


def test_body_is_sanitized_on_write():
    s = _series()
    base = f"/api/series/{s['id']}"
    r = c.post(f"{base}/research", json={"data": {
        "title": "R",
        "body": '<p onclick="evil()">safe</p><script>alert(1)</script>'
                '<img src="javascript:alert(1)"><a href="javascript:alert(1)">link</a>',
    }}).json()
    assert "onclick" not in r["body"]
    assert "<script>" not in r["body"] and "alert(1)" not in r["body"]
    assert "<img" not in r["body"]  # unsafe src dropped the whole img
    assert 'href="javascript:alert(1)"' not in r["body"]
    assert "<p>safe</p>" in r["body"]


def test_bundle_and_import_include_research():
    s = _series()
    base = f"/api/series/{s['id']}"
    c.post(f"{base}/research", json={"data": {"title": "R", "body": "<p>hi</p>"}})
    bun = c.get(f"{base}/bundle").json()
    assert len(bun["research"]) == 1 and bun["research"][0]["title"] == "R"

    imp = c.post("/api/import", json=bun).json()
    b2 = c.get(f"/api/series/{imp['id']}/bundle").json()
    assert len(b2["research"]) == 1
    assert b2["research"][0]["id"] != bun["research"][0]["id"]  # id remapped like every other kind
