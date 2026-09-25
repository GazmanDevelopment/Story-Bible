"""
Story Bible - backend for the Word task-pane add-in.

A small FastAPI + SQLite service. Every record belongs to a Series.
Records are stored as JSON blobs so new fields can be added in the UI
without database migrations.

Env vars:
  STORYBIBLE_DB     path to the SQLite file   (default /data/storybible.db)
  STORYBIBLE_TOKEN  optional shared secret; when set every /api call must
                    send it in the X-Token header
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

DB_PATH = os.environ.get("STORYBIBLE_DB", "/data/storybible.db")
TOKEN = os.environ.get("STORYBIBLE_TOKEN", "").strip()
STATIC_DIR = Path(__file__).parent / "static"

# Record kinds that hang off a series
KINDS = ("chapters", "characters", "locations", "events", "relationships")


# --------------------------------------------------------------------------- db
def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS series (
                   id TEXT PRIMARY KEY,
                   data TEXT NOT NULL,
                   updated REAL NOT NULL)"""
        )
        con.execute(
            """CREATE TABLE IF NOT EXISTS records (
                   id TEXT PRIMARY KEY,
                   series_id TEXT NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                   kind TEXT NOT NULL,
                   data TEXT NOT NULL,
                   updated REAL NOT NULL)"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS ix_rec ON records(series_id, kind)")


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def row_to_obj(row: sqlite3.Row) -> dict[str, Any]:
    obj = json.loads(row["data"])
    obj["id"] = row["id"]
    obj["updated"] = row["updated"]
    return obj


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def clean(data: dict) -> dict:
    """Strip server-managed keys before storing."""
    return {k: v for k, v in data.items() if k not in ("id", "updated", "series_id", "kind")}


# ------------------------------------------------------------------------- auth
def check_token(x_token: str | None = Header(default=None)) -> None:
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="Bad or missing X-Token")


# -------------------------------------------------------------------------- app
app = FastAPI(title="Story Bible", version="0.1.0")
init_db()


class Body(BaseModel):
    data: dict[str, Any]


def get_series_or_404(con, series_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Series not found")
    return row


def check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(404, f"Unknown kind '{kind}'")


# ---- health
@app.get("/api/health")
def health():
    return {"ok": True, "auth": bool(TOKEN)}


# ---- series
@app.get("/api/series", dependencies=[Depends(check_token)])
def list_series():
    with db() as con:
        rows = con.execute("SELECT * FROM series").fetchall()
    return sorted((row_to_obj(r) for r in rows), key=lambda s: s.get("name", "").lower())


@app.post("/api/series", dependencies=[Depends(check_token)])
def create_series(body: Body):
    sid = new_id()
    data = clean(body.data)
    data.setdefault("name", "Untitled series")
    now = time.time()
    with db() as con:
        con.execute("INSERT INTO series VALUES (?,?,?)", (sid, json.dumps(data), now))
    return {**data, "id": sid, "updated": now}


@app.put("/api/series/{series_id}", dependencies=[Depends(check_token)])
def update_series(series_id: str, body: Body):
    now = time.time()
    data = clean(body.data)
    with db() as con:
        get_series_or_404(con, series_id)
        con.execute("UPDATE series SET data=?, updated=? WHERE id=?", (json.dumps(data), now, series_id))
    return {**data, "id": series_id, "updated": now}


@app.delete("/api/series/{series_id}", dependencies=[Depends(check_token)])
def delete_series(series_id: str):
    with db() as con:
        get_series_or_404(con, series_id)
        con.execute("DELETE FROM series WHERE id=?", (series_id,))
    return {"deleted": series_id}


@app.get("/api/series/{series_id}/bundle", dependencies=[Depends(check_token)])
def bundle(series_id: str):
    """Everything for one series in a single call (also used as the export)."""
    with db() as con:
        s = row_to_obj(get_series_or_404(con, series_id))
        rows = con.execute("SELECT * FROM records WHERE series_id=?", (series_id,)).fetchall()
    out: dict[str, Any] = {"series": s, "exported": time.time()}
    for k in KINDS:
        out[k] = [row_to_obj(r) for r in rows if r["kind"] == k]
    return out


@app.post("/api/import", dependencies=[Depends(check_token)])
def import_bundle(bundle_in: dict[str, Any]):
    """Restore an exported bundle as a NEW series (ids are remapped)."""
    if "series" not in bundle_in:
        raise HTTPException(400, "Not a Story Bible export")
    idmap: dict[str, str] = {}
    sid = new_id()
    sdata = clean(bundle_in["series"])
    sdata.setdefault("name", "Imported")
    with db() as con:
        existing = {json.loads(r["data"]).get("name") for r in con.execute("SELECT data FROM series")}
    if sdata["name"] in existing:
        sdata["name"] += " (imported)"
    for k in KINDS:
        for rec in bundle_in.get(k, []):
            idmap[rec["id"]] = new_id()

    def remap(v):
        if isinstance(v, str):
            return idmap.get(v, v)
        if isinstance(v, list):
            return [remap(x) for x in v]
        if isinstance(v, dict):
            return {kk: remap(vv) for kk, vv in v.items()}
        return v

    now = time.time()
    with db() as con:
        con.execute("INSERT INTO series VALUES (?,?,?)", (sid, json.dumps(sdata), now))
        for k in KINDS:
            for rec in bundle_in.get(k, []):
                con.execute(
                    "INSERT INTO records VALUES (?,?,?,?,?)",
                    (idmap[rec["id"]], sid, k, json.dumps(remap(clean(rec))), now),
                )
    return {"id": sid, "name": sdata["name"]}


# ---- records (chapters / characters / locations / events / relationships)
@app.get("/api/series/{series_id}/{kind}", dependencies=[Depends(check_token)])
def list_records(series_id: str, kind: str):
    check_kind(kind)
    with db() as con:
        get_series_or_404(con, series_id)
        rows = con.execute(
            "SELECT * FROM records WHERE series_id=? AND kind=?", (series_id, kind)
        ).fetchall()
    return [row_to_obj(r) for r in rows]


@app.post("/api/series/{series_id}/{kind}", dependencies=[Depends(check_token)])
def create_record(series_id: str, kind: str, body: Body):
    check_kind(kind)
    rid, now, data = new_id(), time.time(), clean(body.data)
    with db() as con:
        get_series_or_404(con, series_id)
        con.execute(
            "INSERT INTO records VALUES (?,?,?,?,?)", (rid, series_id, kind, json.dumps(data), now)
        )
    return {**data, "id": rid, "updated": now}


@app.put("/api/series/{series_id}/{kind}/{rid}", dependencies=[Depends(check_token)])
def update_record(series_id: str, kind: str, rid: str, body: Body):
    check_kind(kind)
    now, data = time.time(), clean(body.data)
    with db() as con:
        cur = con.execute(
            "UPDATE records SET data=?, updated=? WHERE id=? AND series_id=? AND kind=?",
            (json.dumps(data), now, rid, series_id, kind),
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Record not found")
    return {**data, "id": rid, "updated": now}


@app.delete("/api/series/{series_id}/{kind}/{rid}", dependencies=[Depends(check_token)])
def delete_record(series_id: str, kind: str, rid: str):
    check_kind(kind)
    with db() as con:
        cur = con.execute(
            "DELETE FROM records WHERE id=? AND series_id=? AND kind=?", (rid, series_id, kind)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Record not found")
        # Tidy up anything that pointed at the deleted record
        rows = con.execute(
            "SELECT * FROM records WHERE series_id=? AND kind IN ('relationships','events','locations')",
            (series_id,),
        ).fetchall()
        for r in rows:
            d = json.loads(r["data"])
            if r["kind"] == "relationships" and rid in (d.get("from"), d.get("to")):
                con.execute("DELETE FROM records WHERE id=?", (r["id"],))
                continue
            changed = False
            for key in ("character_ids",):
                if rid in d.get(key, []):
                    d[key] = [x for x in d[key] if x != rid]
                    changed = True
            for key in ("location_id", "chapter_id"):
                if d.get(key) == rid:
                    d[key] = ""
                    changed = True
            if changed:
                con.execute("UPDATE records SET data=? WHERE id=?", (json.dumps(d), r["id"]))
    return {"deleted": rid}


# ---- static task pane
@app.get("/")
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
