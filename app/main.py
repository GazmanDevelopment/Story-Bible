"""
Story Bible - backend for the Word task-pane add-in.

A small FastAPI + SQLite service. Every record belongs to a Series.
Records are stored as JSON blobs so new *fields* can be added in the UI
without a schema change. Schema changes themselves (new tables/columns)
are handled by app/migrations.py and applied automatically on startup.

Env vars:
  STORYBIBLE_DB     path to the SQLite file   (default /data/storybible.db)
  STORYBIBLE_TOKEN  optional shared secret; when set every /api call must
                    send it in the X-Token header
  MAX_BODY_BYTES    request body size cap, in bytes (default 5 MiB)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__
from .migrations import migrate
from .models import KIND_MODELS, SeriesIn

DB_PATH = os.environ.get("STORYBIBLE_DB", "/data/storybible.db")
TOKEN = os.environ.get("STORYBIBLE_TOKEN", "").strip()
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 5 * 1024 * 1024))
STATIC_DIR = Path(__file__).parent / "static"

# Record kinds that hang off a series
KINDS = ("chapters", "characters", "locations", "events", "relationships")

# Logging: plain lines to stdout (never the request body or the token - see
# the hardening_middleware below, which only ever logs method/path/status/
# time). Explicit StreamHandler because logging.basicConfig() defaults to
# stderr, and we want this to show up in `docker logs`/TrueNAS app logs as
# stdout like everything else.
logger = logging.getLogger("storybible")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(logging.Formatter("%(message)s"))
logger.addHandler(_handler)
logger.propagate = False


# --------------------------------------------------------------------------- db
def init_db() -> None:
    """Create /data if needed and bring the database up to the latest
    schema. Runs once at import time, on its own connection (not through
    `db()`, since migrations manage their own transactions). Raises if the
    database is newer than this build understands - see app/migrations.py."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    try:
        con.execute("PRAGMA journal_mode = WAL")  # persisted in the file; no need to reset per connection
        con.isolation_level = None  # autocommit; migrate() drives its own transactions
        migrate(con)
    finally:
        con.close()


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")   # wait rather than fail when another save holds the write lock
    con.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; avoids an fsync on every write
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


# ------------------------------------------------------------------- validation
def validate_series(data: dict) -> dict:
    try:
        return SeriesIn(**data).model_dump()
    except ValidationError as e:
        raise HTTPException(400, str(e))


def validate_record(kind: str, data: dict) -> dict:
    try:
        return KIND_MODELS[kind](**data).model_dump(by_alias=True)
    except ValidationError as e:
        raise HTTPException(400, str(e))


# --------------------------------------------------------------------- health
def _db_ok() -> bool:
    """/api/health has no auth (Docker/monitoring hit it directly), so the
    *reason* for a failure is logged server-side, not put in the response -
    a stray exception can contain a filesystem path or similar detail an
    unauthenticated caller shouldn't get for free."""
    try:
        with db() as con:
            con.execute("SELECT 1")
        return True
    except Exception as e:
        logger.error("health check: database unreachable: %s", e)
        return False


def _data_dir_writable(data_dir: Path) -> bool:
    """Same reasoning as _db_ok(): log the detail, don't return it."""
    probe = data_dir / f".health-{uuid.uuid4().hex}"
    try:
        probe.write_text("")
        probe.unlink()
        return True
    except OSError as e:
        logger.error("health check: data directory not writable: %s", e)
        return False


def _sanitize_for_log(s: str) -> str:
    """A request path is attacker-controlled and logged verbatim elsewhere
    in this file - without this, a percent-encoded newline (`%0A`) in a URL
    would let an unauthenticated caller forge fake-looking log lines."""
    return s.replace("\r", "\\r").replace("\n", "\\n")


# ------------------------------------------------------------------------- auth
def check_token(x_token: str | None = Header(default=None)) -> None:
    if TOKEN and x_token != TOKEN:
        raise HTTPException(status_code=401, detail="Bad or missing X-Token")


# -------------------------------------------------------------------------- app
app = FastAPI(title="Story Bible", version=__version__)
init_db()


@app.middleware("http")
async def hardening_middleware(request: Request, call_next):
    """Three concerns in one pass, kept together so the ordering between
    them can't drift apart:
    - reject oversized request bodies with 413, before the route runs
    - log every request to stdout: method, path, status, time taken -
      never the body or the X-Token header
    - Cache-Control: /api/* gets no-store; everything else (the task pane's
      index.html and static assets) gets no-cache, so Word/a browser always
      revalidates instead of running a stale app.js after a deploy (#4).
      StaticFiles already sets ETag/Last-Modified and honours conditional
      GETs, so in practice that revalidation is a cheap 304 most of the time.

    An unhandled exception (not an HTTPException - one of those is already
    a normal Response by the time it gets here) still propagates out of
    `call_next` past this point: Starlette installs Exception/500 handlers
    on ServerErrorMiddleware, which wraps *outside* this middleware, not
    inside it - precisely so a broken user middleware can't hide a crash.
    unhandled_exception_handler() below is the one actually producing that
    response, so it does its own logging and sets its own Cache-Control
    rather than relying on the tail of this function, which it never
    reaches for that path.
    """
    start = time.perf_counter()
    safe_path = _sanitize_for_log(request.url.path)

    # Reads (and Starlette caches) the whole body ourselves, so the cap is
    # enforced on what was actually sent rather than a Content-Length header
    # a client could omit (chunked transfer) or simply lie about.
    if len(await request.body()) > MAX_BODY_BYTES:
        response = JSONResponse({"detail": "Request body too large"}, status_code=413)
    else:
        response = await call_next(request)

    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("%s %s %s %.1fms", request.method, safe_path, response.status_code, elapsed_ms)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Without this, an unhandled exception (e.g. a raw sqlite3 error) gets
    Starlette's bare default 500 response - unlogged, with no Cache-Control
    set at all. This runs in ServerErrorMiddleware, outside
    hardening_middleware (see its docstring), so it has to do both itself."""
    logger.exception("%s %s 500 (unhandled)", request.method, _sanitize_for_log(request.url.path))
    response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    response.headers["Cache-Control"] = "no-store" if request.url.path.startswith("/api/") else "no-cache"
    return response


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
    body: dict[str, Any] = {"ok": True, "auth": bool(TOKEN), "version": __version__}
    if not _db_ok():
        body.update(ok=False, error="database unavailable")
        return JSONResponse(body, status_code=503)
    if not _data_dir_writable(Path(DB_PATH).parent):
        body.update(ok=False, error="data directory not writable")
        return JSONResponse(body, status_code=503)
    return body


# ---- series
@app.get("/api/series", dependencies=[Depends(check_token)])
def list_series():
    with db() as con:
        rows = con.execute("SELECT * FROM series").fetchall()
    return sorted((row_to_obj(r) for r in rows), key=lambda s: s.get("name", "").lower())


@app.post("/api/series", dependencies=[Depends(check_token)])
def create_series(body: Body):
    sid = new_id()
    data = validate_series(clean(body.data))
    now = time.time()
    with db() as con:
        con.execute("INSERT INTO series VALUES (?,?,?)", (sid, json.dumps(data), now))
    return {**data, "id": sid, "updated": now}


@app.put("/api/series/{series_id}", dependencies=[Depends(check_token)])
def update_series(series_id: str, body: Body):
    now = time.time()
    data = validate_series(clean(body.data))
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
    if not isinstance(bundle_in.get("series"), dict):
        raise HTTPException(400, "Not a Story Bible export")
    for k in KINDS:
        recs = bundle_in.get(k, [])
        if not isinstance(recs, list) or not all(isinstance(r, dict) and r.get("id") for r in recs):
            raise HTTPException(400, f"'{k}' must be a list of records, each with an id")
    all_ids = [rec["id"] for k in KINDS for rec in bundle_in.get(k, [])]
    if len(all_ids) != len(set(all_ids)):
        # Two records sharing an id would collide in idmap below and both
        # get remapped to the same new id, tripping the records.id primary
        # key on insert (a 500) instead of a clean 400 here.
        raise HTTPException(400, "Duplicate record id in import bundle")

    idmap: dict[str, str] = {}
    sid = new_id()
    raw_sdata = clean(bundle_in["series"])
    name_was_given = "name" in raw_sdata
    sdata = validate_series(raw_sdata)
    if not name_was_given:
        sdata["name"] = "Imported"
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
                data = validate_record(k, remap(clean(rec)))
                con.execute(
                    "INSERT INTO records VALUES (?,?,?,?,?)",
                    (idmap[rec["id"]], sid, k, json.dumps(data), now),
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
    rid, now = new_id(), time.time()
    data = validate_record(kind, clean(body.data))
    with db() as con:
        get_series_or_404(con, series_id)
        con.execute(
            "INSERT INTO records VALUES (?,?,?,?,?)", (rid, series_id, kind, json.dumps(data), now)
        )
    return {**data, "id": rid, "updated": now}


@app.put("/api/series/{series_id}/{kind}/{rid}", dependencies=[Depends(check_token)])
def update_record(series_id: str, kind: str, rid: str, body: Body):
    check_kind(kind)
    now = time.time()
    data = validate_record(kind, clean(body.data))
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
