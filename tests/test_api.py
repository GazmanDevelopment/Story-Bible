import os, tempfile
os.environ["STORYBIBLE_DB"] = tempfile.mktemp(suffix=".db")
from fastapi.testclient import TestClient
from app.main import app
c = TestClient(app)

def test_flow():
    s = c.post("/api/series", json={"data": {"name": "T"}}).json()
    base = f"/api/series/{s['id']}"
    a = c.post(f"{base}/characters", json={"data": {"name": "A"}}).json()
    b = c.post(f"{base}/characters", json={"data": {"name": "B"}}).json()
    l = c.post(f"{base}/locations", json={"data": {"name": "L", "character_ids": [a["id"], b["id"]]}}).json()
    c.post(f"{base}/relationships", json={"data": {"from": a["id"], "to": b["id"], "type": "married to"}})
    e = c.post(f"{base}/events", json={"data": {"title": "E", "character_ids": [a["id"]], "location_id": l["id"]}}).json()
    # series isolation
    s2 = c.post("/api/series", json={"data": {"name": "T2"}}).json()
    assert c.get(f"/api/series/{s2['id']}/characters").json() == []
    # delete cascades
    assert c.delete(f"{base}/characters/{a['id']}").status_code == 200
    bun = c.get(f"{base}/bundle").json()
    assert bun["relationships"] == []
    assert bun["events"][0]["character_ids"] == []
    assert bun["locations"][0]["character_ids"] == [b["id"]]
    c.delete(f"{base}/locations/{l['id']}")
    assert c.get(f"{base}/bundle").json()["events"][0]["location_id"] == ""
    # export -> import roundtrip remaps ids
    imp = c.post("/api/import", json=bun).json()
    b2 = c.get(f"/api/series/{imp['id']}/bundle").json()
    assert len(b2["characters"]) == 1 and b2["characters"][0]["id"] != b["id"]
    assert imp["name"] == "T (imported)"
    # bad kind
    assert c.get(f"{base}/widgets").status_code == 404
    c.delete(f"/api/series/{s['id']}")
    assert c.get(f"{base}/bundle").status_code == 404

def test_health():
    from app import __version__
    r = c.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "auth": False, "version": __version__}

def test_pragmas():
    """Per-request connections must have the hardening pragmas set, and
    the database itself must be in WAL mode (set once, at init)."""
    from app.main import db
    with db() as con:
        assert con.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert con.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
