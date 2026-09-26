"""
Story Bible - backend for the Word task-pane add-in.

A small FastAPI + SQLite service. Every record belongs to a Series.
Records are stored as JSON blobs so new *fields* can be added in the UI
without a schema change. Schema changes themselves (new tables/columns)
are handled by app/migrations.py and applied automatically on startup.

Env vars:
  STORYBIBLE_DB     path to the SQLite file   (default /data/storybible.db)
  MAX_BODY_BYTES    request body size cap, in bytes (default 5 MiB)
  IMPORT_MAX_BODY_BYTES  body size cap for /api/import specifically, in
                    bytes (default 20 MiB) - a whole-series bundle in one
                    request, unlike every other route's single record, and
                    research entries (#43) can each carry embedded images.
                    Kept well short of a memory-exhaustion-friendly size
                    rather than matched to some large hypothetical import,
                    since STORYBIBLE_TOKEN is optional and this whole body
                    is buffered before any auth check runs (see PLAN.md's
                    "LAN/VPN only" guidance for the actual threat model)

Auth (#10, see app/auth.py for the rest of this): AUTH_MODE is "none",
"token" or "entra" (default: "token" if STORYBIBLE_TOKEN is set, else
"none" - so an existing deployment that only ever set STORYBIBLE_TOKEN
keeps behaving exactly as before with no other change required).
  STORYBIBLE_TOKEN  AUTH_MODE=token's shared secret; every /api call must
                    send it in the X-Token header
  ENTRA_TENANT_ID   required for AUTH_MODE=entra
  ENTRA_CLIENT_ID   required for AUTH_MODE=entra
  ALLOWED_OIDS      optional comma-separated allowlist of Entra object ids,
                    defence in depth on top of Entra's own "assignment
                    required" - unset means don't add this extra check

Nightly backups (VACUUM INTO + a JSON export per series) run in-process -
see app/backup.py for BACKUP_DIR/BACKUP_KEEP_DAYS/BACKUP_HOUR and the
manual `python -m app.backup` entry point.

"Log Issue"/"Log Suggestion" in the pane file a GitHub issue directly -
see app/github_feedback.py for GITHUB_FEEDBACK_TOKEN (#22).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__
from . import auth
from . import backup as backup_mod
from . import github_feedback as feedback_mod
from .migrations import migrate
from .models import KIND_MODELS, SeriesIn

DB_PATH = os.environ.get("STORYBIBLE_DB", "/data/storybible.db")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 5 * 1024 * 1024))
IMPORT_MAX_BODY_BYTES = int(os.environ.get("IMPORT_MAX_BODY_BYTES", 20 * 1024 * 1024))
STATIC_DIR = Path(__file__).parent / "static"

# Record kinds that hang off a series
KINDS = ("chapters", "characters", "locations", "events", "relationships", "research")

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
    obj["version"] = row["version"]
    obj["created_by"] = row["created_by"]
    obj["updated_by"] = row["updated_by"]
    if "owner_oid" in row.keys():  # series rows only (#11)
        obj["owner_oid"] = row["owner_oid"]
    return obj


def new_id() -> str:
    return uuid.uuid4().hex[:12]


SERVER_MANAGED_KEYS = ("id", "updated", "series_id", "kind", "owner_oid", "version", "created_by", "updated_by")


def clean(data: dict) -> dict:
    """Strip server-managed keys before storing - a client echoing back an
    object the server sent it (e.g. {**prev, **edits}) must not be able to
    self-promote via a crafted owner_oid, or roll back version/audit
    fields (#11, #12)."""
    return {k: v for k, v in data.items() if k not in SERVER_MANAGED_KEYS}


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
def upsert_user(con: sqlite3.Connection, user: auth.CurrentUser) -> bool:
    """Record/refresh a signed-in person in the `users` table (#10).
    Returns True the first time this oid is ever seen - the caller uses
    that to claim any ownerless series for them (#11)."""
    now = time.time()
    row = con.execute("SELECT oid FROM users WHERE oid=?", (user.oid,)).fetchone()
    if row:
        con.execute(
            "UPDATE users SET email=?, display_name=?, last_seen=? WHERE oid=?",
            (user.email, user.display_name, now, user.oid),
        )
        return False
    con.execute(
        "INSERT INTO users (oid, email, display_name, first_seen, last_seen) VALUES (?,?,?,?,?)",
        (user.oid, user.email, user.display_name, now, now),
    )
    return True


def get_current_user(
    authorization: str | None = Header(default=None),
    x_token: str | None = Header(default=None),
) -> auth.CurrentUser:
    """The one auth dependency every /api/* route (other than /api/health*
    and /api/config) takes. Behaviour depends entirely on AUTH_MODE, so
    every existing none/token deployment (and every test written before
    #10 landed) keeps working unchanged - only AUTH_MODE=entra does real
    per-person validation."""
    if auth.AUTH_MODE == "none":
        return auth.LOCAL_USER
    if auth.AUTH_MODE == "token":
        if auth.TOKEN and x_token != auth.TOKEN:
            raise HTTPException(status_code=401, detail="Bad or missing X-Token")
        return auth.SHARED_USER
    # entra
    try:
        token = auth.bearer_token(authorization)
        user = auth.resolve_entra_user(token)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    if not user.is_pipeline:
        # A second connection beyond the one the route handler itself opens
        # right after - each `with db() as con:` block is already its own
        # connection throughout this file (no pooling), so this doubles
        # SQLite connection setup for every entra-mode request. Not worth
        # threading a shared, request-scoped connection through every route
        # to save on local-file connect() calls this app's actual scale
        # (a single writer, PLAN.md) makes negligible - see #12's PR
        # discussion if usage ever grows enough to matter.
        with db() as con:
            is_new = upsert_user(con, user)
            if is_new:
                # #11: the first person to ever sign in claims any series
                # left over from before auth existed (owner_oid == '').
                con.execute("UPDATE series SET owner_oid=? WHERE owner_oid=''", (user.oid,))
    return user


# -------------------------------------------------------------------------- app
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Starts the nightly backup scheduler (#5) alongside the server, and
    cancels it cleanly on shutdown. Not triggered by TestClient(app) used
    without a `with` block - i.e. not during this repo's existing tests -
    since that's how the ASGI lifespan protocol works; only a real server
    (uvicorn) or `with TestClient(app) as c:` sends the startup/shutdown
    messages that invoke this."""
    task = asyncio.create_task(backup_mod.scheduler())
    yield
    task.cancel()
    # If the scheduler is mid-backup, it's inside asyncio.to_thread(), which
    # cancellation can't interrupt - only the *next* `await` in that thread
    # would raise, and there isn't one until the thread returns. Bounding
    # our own wait keeps a slow backup from blocking shutdown indefinitely;
    # it doesn't force the worker thread to stop (Python still joins
    # non-daemon executor threads at process exit either way), just caps
    # what this function itself waits for.
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.CancelledError, TimeoutError):
        pass


# #45: grouping/descriptions for the Swagger UI (/docs) and ReDoc (/redoc)
# FastAPI already serves for free. This tag list is metadata only, but the
# response_model=... added below on the small fixed-shape endpoints (health,
# config, me, users, members) is a real behaviour change, not just docs: it
# makes FastAPI filter the returned dict down to that model's declared
# fields. Every current field was checked against what each handler
# actually returns, so nothing is dropped today - but adding a new field to
# one of those handlers later without updating its matching *Out model
# would silently vanish from the response instead of erroring. Deliberately
# NOT applied to series/record/bundle responses - those stay untyped, per
# this module's docstring on why records are intentionally loose JSON.
TAGS_METADATA = [
    {"name": "health", "description": "Liveness/readiness checks for monitoring - no auth required."},
    {"name": "auth", "description": "Sign-in config and the caller's own identity."},
    {"name": "series", "description": "A series is the top-level container everything else hangs off. "
        "Records are stored as free-form JSON (see module docstring), so request/response bodies here "
        "are intentionally loosely typed rather than validated on the way out."},
    {"name": "sharing", "description": "Owner/editor/viewer membership on a series (#11)."},
    {"name": "records", "description": "Chapters, characters, locations, events, relationships and "
        "research entries within a series."},
    {"name": "feedback", "description": "Files a GitHub issue from the task pane's Log Issue/Log "
        "Suggestion buttons."},
]

app = FastAPI(
    title="Story Bible",
    version=__version__,
    description="Backend for the Story Bible Word task-pane add-in - characters, places, "
        "relationships and a timeline for a per-series story bible. See PLAN.md in the repo "
        "for the full data model and the AUTH_MODE options used by `Authorize` above.",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)
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
    # /api/import gets its own, larger cap: it's a whole series bundle in
    # one request rather than a single record, and research entries (#43)
    # can each carry embedded images - a handful of those alone can put a
    # perfectly normal export over the general per-record cap.
    limit = IMPORT_MAX_BODY_BYTES if request.url.path == "/api/import" else MAX_BODY_BYTES
    if len(await request.body()) > limit:
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
    version: int | None = None  # required on PUT, ignored on POST (#12)


def get_series_or_404(con, series_id: str) -> sqlite3.Row:
    row = con.execute("SELECT * FROM series WHERE id=?", (series_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Series not found")
    return row


def check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise HTTPException(404, f"Unknown kind '{kind}'")


# ------------------------------------------------------------ ownership (#11)
def member_role(con, series_id: str, oid: str) -> str | None:
    row = con.execute("SELECT role FROM members WHERE series_id=? AND oid=?", (series_id, oid)).fetchone()
    return row["role"] if row else None


def require_access(con, user: auth.CurrentUser, series_id: str, need: str) -> sqlite3.Row:
    """owner/editor/viewer can read, owner/editor can write, only the owner
    can delete a series or change its sharing (`need` is "read", "write"
    or "owner"). A series the caller can't access at all returns 404
    (existence hidden); one they can see but can't act on this way
    returns 403.

    A no-op outside AUTH_MODE=entra (returns get_series_or_404 unchanged):
    none/token deployments have no real per-user identity to check
    ownership against, so they keep today's single-shared-bible access.
    """
    row = get_series_or_404(con, series_id)
    if auth.AUTH_MODE != "entra":
        return row
    if user.is_pipeline:
        if need != "read":
            raise HTTPException(403, "The review pipeline is read-only")
        return row
    role = "owner" if row["owner_oid"] == user.oid else member_role(con, series_id, user.oid)
    if role is None:
        raise HTTPException(404, "Series not found")
    if need == "read":
        return row
    if need == "write" and role in ("owner", "editor"):
        return row
    if need == "owner" and role == "owner":
        return row
    raise HTTPException(403, "Not allowed")


def accessible_series_ids(con, user: auth.CurrentUser) -> set[str] | None:
    """None means "don't filter" - every non-entra caller, and the
    read-only review pipeline, which can see every series."""
    if auth.AUTH_MODE != "entra" or user.is_pipeline:
        return None
    owned = {r["id"] for r in con.execute("SELECT id FROM series WHERE owner_oid=?", (user.oid,))}
    member = {r["series_id"] for r in con.execute("SELECT series_id FROM members WHERE oid=?", (user.oid,))}
    return owned | member


# ------------------------------------------------------- concurrency (#12)
# The two non-entra identities don't get a `users` row (see app/auth.py) -
# resolved here instead of on every request in the common no-auth case.
_SYNTHETIC_DISPLAY_NAMES = {auth.LOCAL_USER.oid: auth.LOCAL_USER.display_name, auth.SHARED_USER.oid: auth.SHARED_USER.display_name}


def display_name_for(con, oid: str) -> str:
    if not oid:
        return ""
    row = con.execute("SELECT display_name FROM users WHERE oid=?", (oid,)).fetchone()
    if row and row["display_name"]:
        return row["display_name"]
    return _SYNTHETIC_DISPLAY_NAMES.get(oid, oid)


def conflict(con, current: dict[str, Any]) -> HTTPException:
    """A PUT lost the optimistic-concurrency race - #12 wants the current
    record and who changed it back with the 409, so the caller can show
    "changed by X" without a second round trip."""
    return HTTPException(
        status_code=409,
        detail={"error": "conflict", "current": current, "updated_by": display_name_for(con, current.get("updated_by", ""))},
    )


def people_map(con, *records: dict[str, Any]) -> dict[str, str]:
    """oid -> display name, for every created_by/updated_by among the
    given records - the bundle's `people` map (#12), so the pane can show
    "edited by X" without a request per record."""
    oids = {r.get(k) for r in records for k in ("created_by", "updated_by") if r.get(k)}
    return {oid: display_name_for(con, oid) for oid in oids}


# ---- health
class HealthResponse(BaseModel):
    ok: bool
    auth: bool
    version: str


@app.get("/api/health", tags=["health"], response_model=HealthResponse,
         responses={503: {"description": "Database unreachable, or the data directory isn't writable"}})
def health():
    body: dict[str, Any] = {"ok": True, "auth": auth.AUTH_MODE != "none", "version": __version__}
    if not _db_ok():
        body.update(ok=False, error="database unavailable")
        return JSONResponse(body, status_code=503)
    if not _data_dir_writable(Path(DB_PATH).parent):
        body.update(ok=False, error="data directory not writable")
        return JSONResponse(body, status_code=503)
    return body


MAX_BACKUP_AGE_SECONDS = 36 * 3600


class HealthBackupResponse(BaseModel):
    ok: bool
    age_seconds: float


@app.get("/api/health/backup", tags=["health"], response_model=HealthBackupResponse,
         responses={503: {"description": "No successful backup yet, or the most recent one is stale"}})
def health_backup():
    """For monitoring (#17), not the pane - unauthenticated like /api/health,
    since a monitoring tool won't have STORYBIBLE_TOKEN either."""
    age = backup_mod.last_backup_age_seconds()
    if age is None:
        return JSONResponse({"ok": False, "error": "no successful backup yet"}, status_code=503)
    if age > MAX_BACKUP_AGE_SECONDS:
        return JSONResponse({"ok": False, "age_seconds": age, "error": "backup is stale"}, status_code=503)
    return {"ok": True, "age_seconds": age}


# ---- auth (#10)
class ConfigOut(BaseModel):
    authMode: str
    tenantId: str
    clientId: str


@app.get("/api/config", tags=["auth"], response_model=ConfigOut)
def get_config():
    """Public (no auth) - the pane needs this before it has any way to
    authenticate, to know *how* to sign in (#13). Only ever the tenant/
    client id, both already public in the app's own manifest/redirect URIs
    - nothing here is a secret."""
    return {
        "authMode": auth.AUTH_MODE,
        "tenantId": auth.ENTRA_TENANT_ID,
        "clientId": auth.ENTRA_CLIENT_ID,
    }


class MeOut(BaseModel):
    oid: str
    email: str
    displayName: str
    isPipeline: bool


@app.get("/api/me", tags=["auth"], response_model=MeOut)
def get_me(user: auth.CurrentUser = Depends(get_current_user)):
    return {"oid": user.oid, "email": user.email, "displayName": user.display_name, "isPipeline": user.is_pipeline}


class UserOut(BaseModel):
    oid: str
    email: str
    display_name: str


@app.get("/api/users", tags=["auth"], response_model=list[UserOut])
def list_users(user: auth.CurrentUser = Depends(get_current_user)):
    """People who have signed in at least once (#11) - for picking who to
    share a series with. Not scoped to any one series; being listed here
    only means "known to this server", the same low bar as showing up in
    anyone's Entra directory."""
    with db() as con:
        rows = con.execute("SELECT oid, email, display_name FROM users ORDER BY display_name").fetchall()
    return [dict(r) for r in rows]


# ---- series
@app.get("/api/series", tags=["series"])
def list_series(user: auth.CurrentUser = Depends(get_current_user)):
    with db() as con:
        ids = accessible_series_ids(con, user)
        rows = con.execute("SELECT * FROM series").fetchall()
    if ids is not None:
        rows = [r for r in rows if r["id"] in ids]
    return sorted((row_to_obj(r) for r in rows), key=lambda s: s.get("name", "").lower())


@app.post("/api/series", tags=["series"])
def create_series(body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    sid = new_id()
    data = validate_series(clean(body.data))
    now = time.time()
    with db() as con:
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid, version, created_by, updated_by) "
            "VALUES (?,?,?,?,1,?,?)",
            (sid, json.dumps(data), now, user.oid, user.oid, user.oid),
        )
    return {**data, "id": sid, "updated": now, "owner_oid": user.oid, "version": 1,
            "created_by": user.oid, "updated_by": user.oid}


@app.put("/api/series/{series_id}", tags=["series"])
def update_series(series_id: str, body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    now = time.time()
    data = validate_series(clean(body.data))
    with db() as con:
        require_access(con, user, series_id, "write")
        if body.version is None:
            raise HTTPException(428, "version is required")
        cur = con.execute(
            "UPDATE series SET data=?, updated=?, version=version+1, updated_by=? WHERE id=? AND version=?",
            (json.dumps(data), now, user.oid, series_id, body.version),
        )
        if cur.rowcount == 0:
            raise conflict(con, row_to_obj(get_series_or_404(con, series_id)))
        updated = row_to_obj(get_series_or_404(con, series_id))
    return updated


class DeletedOut(BaseModel):
    deleted: str


@app.delete("/api/series/{series_id}", tags=["series"], response_model=DeletedOut)
def delete_series(series_id: str, user: auth.CurrentUser = Depends(get_current_user)):
    with db() as con:
        require_access(con, user, series_id, "owner")
        con.execute("DELETE FROM series WHERE id=?", (series_id,))
    return {"deleted": series_id}


@app.get("/api/series/{series_id}/bundle", tags=["series"])
def bundle(series_id: str, user: auth.CurrentUser = Depends(get_current_user)):
    """Everything for one series in a single call (also used as the export)."""
    with db() as con:
        s = row_to_obj(require_access(con, user, series_id, "read"))
        rows = con.execute("SELECT * FROM records WHERE series_id=?", (series_id,)).fetchall()
        by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
        for r in rows:
            by_kind[r["kind"]].append(row_to_obj(r))
        all_recs = [rec for recs in by_kind.values() for rec in recs]
        people = people_map(con, s, *all_recs)
    return {"series": s, "exported": time.time(), "people": people, **by_kind}


@app.post("/api/import", tags=["series"])
def import_bundle(bundle_in: dict[str, Any], user: auth.CurrentUser = Depends(get_current_user)):
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
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid, version, created_by, updated_by) "
            "VALUES (?,?,?,?,1,?,?)",
            (sid, json.dumps(sdata), now, user.oid, user.oid, user.oid),
        )
        for k in KINDS:
            for rec in bundle_in.get(k, []):
                data = validate_record(k, remap(clean(rec)))
                con.execute(
                    "INSERT INTO records (id, series_id, kind, data, updated, version, created_by, updated_by) "
                    "VALUES (?,?,?,?,?,1,?,?)",
                    (idmap[rec["id"]], sid, k, json.dumps(data), now, user.oid, user.oid),
                )
    return {"id": sid, "name": sdata["name"]}


# ---- sharing (#11)
class MemberBody(BaseModel):
    role: str


class MemberOut(BaseModel):
    oid: str
    role: str
    display_name: str | None = None
    email: str | None = None


class MembersOut(BaseModel):
    owner_oid: str
    members: list[MemberOut]


@app.get("/api/series/{series_id}/members", tags=["sharing"], response_model=MembersOut)
def list_members(series_id: str, user: auth.CurrentUser = Depends(get_current_user)):
    with db() as con:
        row = require_access(con, user, series_id, "read")
        rows = con.execute(
            "SELECT members.oid, members.role, users.display_name, users.email "
            "FROM members LEFT JOIN users ON users.oid = members.oid WHERE series_id=?",
            (series_id,),
        ).fetchall()
    return {"owner_oid": row["owner_oid"], "members": [dict(r) for r in rows]}


class MemberRoleOut(BaseModel):
    oid: str
    role: str


@app.put("/api/series/{series_id}/members/{oid}", tags=["sharing"], response_model=MemberRoleOut)
def put_member(series_id: str, oid: str, body: MemberBody, user: auth.CurrentUser = Depends(get_current_user)):
    if body.role not in ("editor", "viewer"):
        raise HTTPException(400, "role must be 'editor' or 'viewer'")
    with db() as con:
        row = require_access(con, user, series_id, "owner")
        if oid == row["owner_oid"]:
            raise HTTPException(400, "That person already owns this series")
        if not con.execute("SELECT 1 FROM users WHERE oid=?", (oid,)).fetchone():
            raise HTTPException(400, "Unknown user - they need to have signed in at least once")
        con.execute(
            "INSERT INTO members (series_id, oid, role) VALUES (?,?,?) "
            "ON CONFLICT(series_id, oid) DO UPDATE SET role=excluded.role",
            (series_id, oid, body.role),
        )
    return {"oid": oid, "role": body.role}


@app.delete("/api/series/{series_id}/members/{oid}", tags=["sharing"], response_model=DeletedOut)
def delete_member(series_id: str, oid: str, user: auth.CurrentUser = Depends(get_current_user)):
    """The owner can remove anyone; anyone can remove themselves (leave)."""
    with db() as con:
        require_access(con, user, series_id, "read")
        if auth.AUTH_MODE == "entra":
            row = get_series_or_404(con, series_id)
            is_owner = row["owner_oid"] == user.oid
            if not (is_owner or oid == user.oid):
                raise HTTPException(403, "Not allowed")
        con.execute("DELETE FROM members WHERE series_id=? AND oid=?", (series_id, oid))
    return {"deleted": oid}


# ---- records (chapters / characters / locations / events / relationships)
@app.get("/api/series/{series_id}/{kind}", tags=["records"])
def list_records(series_id: str, kind: str, user: auth.CurrentUser = Depends(get_current_user)):
    check_kind(kind)
    with db() as con:
        require_access(con, user, series_id, "read")
        rows = con.execute(
            "SELECT * FROM records WHERE series_id=? AND kind=?", (series_id, kind)
        ).fetchall()
    return [row_to_obj(r) for r in rows]


@app.post("/api/series/{series_id}/{kind}", tags=["records"])
def create_record(series_id: str, kind: str, body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    check_kind(kind)
    rid, now = new_id(), time.time()
    data = validate_record(kind, clean(body.data))
    with db() as con:
        require_access(con, user, series_id, "write")
        con.execute(
            "INSERT INTO records (id, series_id, kind, data, updated, version, created_by, updated_by) "
            "VALUES (?,?,?,?,?,1,?,?)",
            (rid, series_id, kind, json.dumps(data), now, user.oid, user.oid),
        )
    return {**data, "id": rid, "updated": now, "version": 1, "created_by": user.oid, "updated_by": user.oid}


@app.put("/api/series/{series_id}/{kind}/{rid}", tags=["records"])
def update_record(series_id: str, kind: str, rid: str, body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    check_kind(kind)
    now = time.time()
    data = validate_record(kind, clean(body.data))
    with db() as con:
        require_access(con, user, series_id, "write")
        if body.version is None:
            raise HTTPException(428, "version is required")
        cur = con.execute(
            "UPDATE records SET data=?, updated=?, version=version+1, updated_by=? "
            "WHERE id=? AND series_id=? AND kind=? AND version=?",
            (json.dumps(data), now, user.oid, rid, series_id, kind, body.version),
        )
        if cur.rowcount == 0:
            existing = con.execute(
                "SELECT * FROM records WHERE id=? AND series_id=? AND kind=?", (rid, series_id, kind)
            ).fetchone()
            if not existing:
                raise HTTPException(404, "Record not found")
            raise conflict(con, row_to_obj(existing))
        updated = row_to_obj(con.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())
    return updated


@app.delete("/api/series/{series_id}/{kind}/{rid}", tags=["records"], response_model=DeletedOut)
def delete_record(series_id: str, kind: str, rid: str, user: auth.CurrentUser = Depends(get_current_user)):
    check_kind(kind)
    with db() as con:
        require_access(con, user, series_id, "write")
        cur = con.execute(
            "DELETE FROM records WHERE id=? AND series_id=? AND kind=?", (rid, series_id, kind)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Record not found")
        # Tidy up anything that pointed at the deleted record
        rows = con.execute(
            "SELECT * FROM records WHERE series_id=? AND kind IN ('relationships','events','locations','research')",
            (series_id,),
        ).fetchall()
        for r in rows:
            d = json.loads(r["data"])
            if r["kind"] == "relationships" and rid in (d.get("from"), d.get("to")):
                con.execute("DELETE FROM records WHERE id=?", (r["id"],))
                continue
            changed = False
            for key in ("character_ids", "location_ids", "event_ids"):
                if rid in d.get(key, []):
                    d[key] = [x for x in d[key] if x != rid]
                    changed = True
            for key in ("location_id", "chapter_id"):
                if d.get(key) == rid:
                    d[key] = ""
                    changed = True
            if changed:
                # Bumps version/updated_by too (#12) - a stale form editing
                # one of these records must see a conflict on save rather
                # than restoring the reference this delete just removed.
                con.execute(
                    "UPDATE records SET data=?, updated=?, version=version+1, updated_by=? WHERE id=?",
                    (json.dumps(d), time.time(), user.oid, r["id"]),
                )
    return {"deleted": rid}


# ---- feedback
@app.post("/api/feedback", status_code=201, tags=["feedback"])
async def submit_feedback(body: dict[str, Any], user: auth.CurrentUser = Depends(get_current_user)):
    """Files a GitHub issue from the pane's "Log Issue"/"Log Suggestion"
    buttons - see app/github_feedback.py. async because it awaits an
    outbound HTTPS call (httpx.AsyncClient) rather than blocking the
    single uvicorn worker on it."""
    feedback = feedback_mod.validate_feedback(body)
    return await feedback_mod.file_feedback(feedback)


# ---- static task pane
@app.get("/", include_in_schema=False)
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
