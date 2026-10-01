"""
Story Bible - backend for the Word task-pane add-in.

A small FastAPI + SQLite service. Every record belongs to a Series.
Records are stored as JSON blobs so new *fields* can be added without a
database migration (a field does have to be declared in app/models.py to be
kept - unknown fields are dropped, see there). Schema changes themselves (new tables/columns)
are handled by app/migrations.py and applied automatically on startup.

Env vars:
  STORYBIBLE_DB     path to the SQLite file   (default /data/storybible.db)
  MAX_BODY_BYTES    request body size cap, in bytes (default 5 MiB)
  MAX_RECORDS_PER_SERIES / MAX_SERIES_PER_OWNER  ceilings on how much one
                    series / one person can hold (defaults 20000 / 200, #71)
  MAX_USERS         cap on total accounts (default 0 = unlimited). Once reached,
                    a person who has never signed in is refused (403) at their
                    first request; existing accounts and LEGACY_OWNER_OID are
                    unaffected (#89)
  MAX_BYTES_PER_OWNER  (AUTH_MODE=entra only) storage ceiling per person: the JSON stored in all
                    series they own, shared records included (default
                    500 MiB, 0 = off). Writes that would grow past it get a
                    400; edits that shrink data and deletes always work (#89)
  WRITE_RATE_LIMIT_PER_MINUTE  per-person ceiling on write requests (POST/PUT/
                    DELETE) in AUTH_MODE=entra (default 120, 0 = off); over it
                    gets 429 + Retry-After (#89)
  FEEDBACK_RATE_LIMIT_PER_PERSON / FEEDBACK_RATE_LIMIT_GLOBAL  "Log Issue"
                    filings per hour, per person / across everyone (defaults
                    5 / 60; see app/github_feedback.py, #89)
  ENABLE_API_DOCS   serve /docs, /redoc and /openapi.json, unauthenticated
                    (default off - see API_DOCS_ENABLED, #68)
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
  ALLOWED_OIDS      comma-separated allowlist of Entra object ids, defence in
                    depth on top of Entra's own "assignment required".
                    Required for AUTH_MODE=entra; "*" deliberately allows
                    everyone Entra lets in (#70)
  LEGACY_OWNER_OID  the one Entra object id that owns series created before
                    sign-in existed; unset = nobody claims them (#70)
  SIGNUP_MODE       allowlist (default: one tenant, ALLOWED_OIDS required) or
                    open (any Microsoft account; AUTH_MODE=entra only; ALLOWED_OIDS
                    optional) (#90)
  BLOCKED_TENANTS   open mode: comma-separated tenant ids refused with 403 (#90)
  PRIVACY_URL, TERMS_URL
                    optional https links to the privacy policy / terms, shown
                    on the signed-out pane and sign-in dialog (#91)
  POLICY_VERSION    version of the privacy policy and terms (default 1.0); people
                    must accept the current one after signing in, so bump it
                    whenever the legal pages change (#111)
  ADMIN_OIDS        comma-separated Entra object ids of the administrators who
                    get the pane's Admin view (users, usage, blocking). Object
                    ids only, never emails - with open signup an email claim
                    can be forged. AUTH_MODE=entra only; unset = no admins (#82)

Nightly backups (VACUUM INTO + a JSON export per series) run in-process -
see app/backup.py for BACKUP_DIR/BACKUP_KEEP_DAYS/BACKUP_HOUR and the
manual `python -m app.backup` entry point.

"Log Issue"/"Log Suggestion" in the pane file a GitHub issue directly -
see app/github_feedback.py for GITHUB_FEEDBACK_TOKEN (#22).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import sys
from urllib.parse import urlparse
import threading
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.middleware.gzip import GZipMiddleware

from . import __version__
from . import auth
from . import backup as backup_mod
from . import github_feedback as feedback_mod
from .migrations import migrate
from .models import KIND_MODELS, MAX_ID, MAX_SHORT, REF_FIELDS, SeriesIn

DB_PATH = os.environ.get("STORYBIBLE_DB", "/data/storybible.db")
# Swagger UI / ReDoc / openapi.json (#45) describe every route and are served
# without auth, which the only-health-is-public rule (#44/#68) doesn't allow -
# so they're off unless explicitly switched on (e.g. for local development).
API_DOCS_ENABLED = os.environ.get("ENABLE_API_DOCS", "").strip().lower() in ("1", "true", "yes", "on")
MAX_BODY_BYTES = int(os.environ.get("MAX_BODY_BYTES", 5 * 1024 * 1024))
IMPORT_MAX_BODY_BYTES = int(os.environ.get("IMPORT_MAX_BODY_BYTES", 20 * 1024 * 1024))
# Per-series and per-person ceilings (#71): field sizes are bounded in
# app/models.py, and these bound how many of them there can be. Generous for a
# story bible (20,000 records is far beyond any real one); they exist so a
# runaway client or import loop can't fill the dataset.
MAX_RECORDS_PER_SERIES = int(os.environ.get("MAX_RECORDS_PER_SERIES", 20_000))
MAX_SERIES_PER_OWNER = int(os.environ.get("MAX_SERIES_PER_OWNER", 200))
# Open-signup abuse limits (#89). 0 turns each one off.
MAX_USERS = int(os.environ.get("MAX_USERS", 0))
# Version of the privacy policy and terms people must have accepted (#111).
POLICY_VERSION = os.environ.get("POLICY_VERSION", "1.0").strip() or "1.0"
MAX_BYTES_PER_OWNER = int(os.environ.get("MAX_BYTES_PER_OWNER", 500 * 1024 * 1024))
WRITE_RATE_LIMIT_PER_MINUTE = int(os.environ.get("WRITE_RATE_LIMIT_PER_MINUTE", 120))
# Same sliding-window limiter the feedback route uses (no overall cap: this is
# purely a per-person fairness limit). Per process, like _seen_users below -
# right for the single worker. None = disabled.
_write_limiter = (feedback_mod._RateLimiter(WRITE_RATE_LIMIT_PER_MINUTE, 60)
                  if WRITE_RATE_LIMIT_PER_MINUTE > 0 else None)
STATIC_DIR = Path(__file__).parent / "static"

# Record kinds that hang off a series
KINDS = ("chapters", "characters", "locations", "events", "relationships", "research")

# Logging: plain lines to stdout (never the request body or the token - see
# HardeningMiddleware below, which only ever logs method/path/status/
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
        if auth.AUTH_MODE == "none":
            logger.warning(
                "AUTH_MODE=none: the API has NO authentication - anyone who can reach this "
                "port can read and delete every series. Set STORYBIBLE_TOKEN or AUTH_MODE=entra.")
        if auth.AUTH_MODE == "entra":
            if auth.SIGNUP_MODE == "open":
                logger.warning("SIGNUP_MODE=open: any Microsoft account can sign in. "
                               "Blocked tenants: %d; MAX_USERS=%s.", len(auth.BLOCKED_TENANTS), MAX_USERS or "unlimited")
            if auth.LEGACY_OWNER_OID and auth.ALLOWED_OIDS and auth.LEGACY_OWNER_OID not in auth.ALLOWED_OIDS:
                logger.warning(
                    "LEGACY_OWNER_OID is not in ALLOWED_OIDS, so that person can never sign in "
                    "and pre-sign-in series will stay unclaimed.")
            n = con.execute("SELECT COUNT(*) FROM series WHERE owner_oid=''").fetchone()[0]
            if n and not auth.LEGACY_OWNER_OID:
                logger.warning(
                    "%d series have no owner (they predate sign-in) and no signed-in person can see them "
                    "until LEGACY_OWNER_OID is set to the Entra object id who should own them.", n)
    finally:
        con.close()


@contextmanager
def db(write: bool = False):
    """`write=True` takes SQLite's write lock immediately (BEGIN IMMEDIATE)
    instead of at the first INSERT/UPDATE. Use it wherever a handler *checks*
    something and then writes on the strength of the check (a quota, a
    reference, a version): with Python's default lazy transactions those
    reads run outside any transaction, so a concurrent write could slip in
    between the check and the insert (#71)."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA busy_timeout = 5000")   # wait rather than fail when another save holds the write lock
    con.execute("PRAGMA synchronous = NORMAL")  # safe with WAL; avoids an fsync on every write
    try:
        if write:
            con.execute("BEGIN IMMEDIATE")
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


def check_series_quota(con, user: auth.CurrentUser) -> None:
    owned = con.execute("SELECT COUNT(*) FROM series WHERE owner_oid=?", (user.oid,)).fetchone()[0]
    if owned >= MAX_SERIES_PER_OWNER:
        raise HTTPException(400, f"Series limit reached ({owned} of {MAX_SERIES_PER_OWNER})")


def owner_bytes(con, owner_oid: str) -> int:
    """Bytes of JSON stored in every series `owner_oid` owns, their records
    included (a shared series counts against its owner, not the editors)."""
    return con.execute(
        "SELECT COALESCE((SELECT SUM(LENGTH(CAST(data AS BLOB))) FROM series WHERE owner_oid=:o), 0) "
        "     + COALESCE((SELECT SUM(LENGTH(CAST(data AS BLOB))) FROM records "
        "                 WHERE series_id IN (SELECT id FROM series WHERE owner_oid=:o)), 0)",
        {"o": owner_oid},
    ).fetchone()[0]


def check_storage_quota(con, owner_oid: str, adding: int) -> None:
    """400 if storing `adding` more bytes would put `owner_oid` over
    MAX_BYTES_PER_OWNER (#89). Call inside the write lock, and only for writes
    that grow the data - shrinking an edit or deleting must always work, even
    for someone already over the line."""
    # Entra only, and never for the unowned ('') pre-auth bucket: in none/token
    # mode every series belongs to one shared identity, so a "per person"
    # ceiling would really be a server-wide one. Cost: one SUM over the
    # owner's rows per growing write - fine at story-bible sizes.
    if MAX_BYTES_PER_OWNER <= 0 or adding <= 0 or auth.AUTH_MODE != "entra" or not owner_oid:
        return
    used = owner_bytes(con, owner_oid)
    if used + adding > MAX_BYTES_PER_OWNER:
        raise HTTPException(400, f"Storage limit reached ({used // 1024 // 1024} of "
                                 f"{MAX_BYTES_PER_OWNER // 1024 // 1024} MiB used) - the series' owner "
                                 "needs to free up space")


def iter_refs(kind: str, data: dict):
    """Every (field, referenced id) in a record - see models.REF_FIELDS."""
    for field in REF_FIELDS.get(kind, {}):
        v = data.get(field)
        if isinstance(v, list):
            for x in v:
                yield field, x
        elif v:
            yield field, v


def check_refs(con, series_id: str, kind: str, data: dict, already: frozenset = frozenset()) -> None:
    """400 if a record points at an id that isn't a record of the right kind
    in THIS series (#71) - otherwise one series could reference another's ids,
    and a typo would silently produce a dangling link. `already` is the
    (field, id) pairs the stored record had before this edit: those are not
    re-checked, or a record that arrived with a dangling link (from an older
    import) could never be saved again - only *new* links must resolve."""
    refs = [ref for ref in iter_refs(kind, data) if ref not in already]
    if not refs:
        return
    found: dict[str, str] = {}
    ids = sorted({r for _, r in refs})
    for i in range(0, len(ids), 500):  # stay well under SQLite's variable limit
        chunk = ids[i:i + 500]
        for row in con.execute(
            f"SELECT id, kind FROM records WHERE series_id=? AND id IN ({','.join('?' * len(chunk))})",
            (series_id, *chunk),
        ):
            found[row["id"]] = row["kind"]
    bad = sorted({f for f, r in refs if found.get(r) != REF_FIELDS[kind][f]})
    if bad:
        raise HTTPException(
            400, f"{', '.join(bad)}: refers to a record that doesn't exist in this series (or is the wrong kind)")


def scrub_refs(kind: str, data: dict, valid: dict[str, set[str]]) -> int:
    """Import-side counterpart of check_refs: drop references that don't
    resolve inside the bundle being imported (the old data may already have
    had dangling ones, and a foreign id must never survive into the new
    series). Returns how many were dropped."""
    dropped = 0
    for field, target in REF_FIELDS.get(kind, {}).items():
        v = data.get(field)
        if isinstance(v, list):
            # isinstance first: an unhashable item (a dict) in a hand-edited
            # bundle must be dropped, not raise on the set lookup.
            keep = [x for x in v if isinstance(x, str) and x in valid[target]]
            dropped += len(v) - len(keep)
            data[field] = keep
        elif v and not (isinstance(v, str) and v in valid[target]):
            data[field] = ""
            dropped += 1
    return dropped


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


_DIR_PROBE_TTL_SECONDS = 5
_dir_probe_cache: dict[str, float] = {}  # data dir -> monotonic time of last successful probe
_dir_probe_lock = threading.Lock()


def _data_dir_writable_cached(data_dir: Path) -> bool:
    """/api/health is unauthenticated, and the probe above creates and
    deletes a file - so anyone who can reach the port could otherwise make
    the server do disk writes as fast as they can send requests (#68).
    Docker polls every 30 s, so a few seconds of caching costs monitoring
    nothing."""
    key = str(data_dir)
    with _dir_probe_lock:  # concurrent requests at expiry must not each probe
        hit = _dir_probe_cache.get(key)
        if hit and time.monotonic() - hit < _DIR_PROBE_TTL_SECONDS:
            return True
        ok = _data_dir_writable(data_dir)
        if ok:
            # Only successes are cached: a failure is reported the moment
            # the directory recovers, and one transient I/O error can't
            # keep /api/health at 503 for the whole TTL.
            _dir_probe_cache[key] = time.monotonic()
        else:
            _dir_probe_cache.pop(key, None)
        return ok


def _sanitize_for_log(s: str) -> str:
    """A request path is attacker-controlled and logged verbatim elsewhere
    in this file - without this, a percent-encoded newline (`%0A`) in a URL
    would let an unauthenticated caller forge fake-looking log lines."""
    return s.replace("\r", "\\r").replace("\n", "\\n")


# ------------------------------------------------------------------------- auth
def upsert_user(con: sqlite3.Connection, user: auth.CurrentUser) -> bool:
    """Record/refresh a signed-in person in the `users` table (#10).
    Returns True the first time this oid is ever seen."""
    now = time.time()
    row = con.execute("SELECT oid FROM users WHERE oid=?", (user.oid,)).fetchone()
    if row:
        con.execute(
            "UPDATE users SET email=?, display_name=?, tid=?, last_seen=? WHERE oid=?",
            (user.email, user.display_name, user.tid, now, user.oid),
        )
        return False
    con.execute(
        "INSERT INTO users (oid, email, display_name, tid, first_seen, last_seen) VALUES (?,?,?,?,?,?)",
        (user.oid, user.email, user.display_name, user.tid, now, now),
    )
    return True


# How often a signed-in person's `users` row is rewritten (#72). It used to be
# on every request, which took SQLite's single write lock (and a second
# connection) for what is really a read - the pane makes several calls per
# click. last_seen is only "roughly when", so minutes-old is fine.
LAST_SEEN_REFRESH_SECONDS = 300
# (DB path, oid) -> (email, display name, monotonic time of the last write).
# Per process, which is right for this app's single worker; a second worker
# would just do one extra write. Keyed by DB path so tests (and anything else
# that points the app at a different file) get their own entries. Replacing the
# database file underneath a RUNNING process is not supported anyway - restart
# it, which also clears this - see docs/RESTORE.md.
_seen_users: dict[tuple[str, str], tuple[str, str, str, float]] = {}
# Bumped by every block (#82). get_current_user reads it before its DB check and
# only caches the person if it hasn't moved, so a request that was already past
# the blocked_users check can't re-create the cache entry a block just evicted.
_block_epoch = 0


def get_current_user(
    authorization: str | None = Header(default=None),
    x_token: str | None = Header(default=None),
) -> auth.CurrentUser:
    """The one auth dependency every /api/* route (other than /api/health
    and /api/config) takes. Behaviour depends entirely on AUTH_MODE, so
    every existing none/token deployment (and every test written before
    #10 landed) keeps working unchanged - only AUTH_MODE=entra does real
    per-person validation."""
    if auth.AUTH_MODE == "none":
        return auth.LOCAL_USER
    if auth.AUTH_MODE == "token":
        # compare_digest on fixed-length digests: a plain != short-circuits
        # on the first differing byte, and compare_digest alone still
        # returns early on a length mismatch (#70).
        if auth.TOKEN and not hmac.compare_digest(
            hashlib.sha256((x_token or "").encode()).digest(), hashlib.sha256(auth.TOKEN.encode()).digest()
        ):
            raise HTTPException(status_code=401, detail="Bad or missing X-Token")
        return auth.SHARED_USER
    # entra
    try:
        token = auth.bearer_token(authorization)
        user = auth.resolve_entra_user(token)
    except auth.AuthError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    if not user.is_pipeline:
        key, now = (DB_PATH, user.oid), time.monotonic()
        epoch = _block_epoch
        seen = _seen_users.get(key)
        if seen and seen[:3] == (user.email, user.display_name, user.tid) and now - seen[3] < LAST_SEEN_REFRESH_SECONDS:
            return user  # already recorded a moment ago: no connection, no write
        # write=True when capped so two first sign-ins can't both squeeze past
        # MAX_USERS (#89); uncapped, this stays the cheap lazy transaction.
        with db(write=MAX_USERS > 0) as con:
            if con.execute("SELECT 1 FROM blocked_users WHERE oid=?", (user.oid,)).fetchone():
                # #82. Never reaches the _seen_users cache below, and
                # admin_block_user() evicts any entry made before the block.
                raise HTTPException(403, "This account has been disabled")
            known = con.execute("SELECT tid FROM users WHERE oid=?", (user.oid,)).fetchone()
            if known and known["tid"] and user.tid and known["tid"] != user.tid:
                # Same oid from a different tenant: oid is the primary key, so
                # this would take over (and overwrite) another person's account.
                logger.warning("entra auth failed: oid already belongs to another tenant")
                raise HTTPException(403, "Not authorized")
            if MAX_USERS > 0 and not known:
                # (LEGACY_OWNER_OID is exempt: refusing them would strand the pre-sign-in series.)
                if (user.oid != auth.LEGACY_OWNER_OID
                        and con.execute("SELECT COUNT(*) FROM users").fetchone()[0] >= MAX_USERS):
                    raise HTTPException(403, "This server is not accepting new accounts right now")
            upsert_user(con, user)
            # In open mode also require the home tenant: oids are only unique
            # within a tenant's own namespace, so never hand pre-sign-in series
            # to a foreign-tenant principal presenting the same oid (#90).
            if (auth.LEGACY_OWNER_OID and user.oid == auth.LEGACY_OWNER_OID
                    and (auth.SIGNUP_MODE != "open" or auth.is_home_tenant(user.tid))
                    and con.execute("SELECT 1 FROM series WHERE owner_oid='' LIMIT 1").fetchone()):
                # #11/#70: series left over from before auth existed
                # (owner_oid == '') go to the one person named in
                # LEGACY_OWNER_OID - NOT to whoever happens to sign in
                # first, which let any unintended tenant user take them.
                # The SELECT keeps this a read (no write lock) once
                # everything has been claimed.
                con.execute("UPDATE series SET owner_oid=? WHERE owner_oid=''", (user.oid,))
        if epoch == _block_epoch:
            _seen_users[key] = (user.email, user.display_name, user.tid, now)
    return user


def require_person(user: auth.CurrentUser = Depends(get_current_user)) -> auth.CurrentUser:
    """For routes that create data or expose directory information: the
    review pipeline is documented as read-only (#10, PLAN.md Phase 6), and
    require_access() only protects *existing* series - creating a new one,
    importing, filing feedback and listing users have no series to check,
    so they need this instead (#70)."""
    if user.is_pipeline:
        raise HTTPException(403, "The review pipeline is read-only")
    return user


def is_admin(user: auth.CurrentUser) -> bool:
    # Open mode: an oid is only unique within its own tenant, so an admin must
    # also be signed in from the home tenant (#90).
    return (auth.AUTH_MODE == "entra" and not user.is_pipeline and user.oid.lower() in auth.ADMIN_OIDS
            and (auth.SIGNUP_MODE != "open" or auth.is_home_tenant(user.tid)))


def require_admin(user: auth.CurrentUser = Depends(require_person)) -> auth.CurrentUser:
    """Admin-only routes (#82). 404, not 403, for everyone else so the routes
    aren't advertised. Admins are named by oid in ADMIN_OIDS - see auth.py for
    why never by email."""
    if not is_admin(user):
        raise HTTPException(404, "Not found")
    return user


def limit_writes(user: auth.CurrentUser = Depends(get_current_user)) -> None:
    """Per-person write rate limit (#89), a dependency on every POST/PUT/DELETE
    route. Only in AUTH_MODE=entra: in none/token there is one shared identity,
    so a "per person" limit would just throttle the one bible. Counted before
    the handler runs, so a flood of rejected requests (403/409/...) is limited
    too. Auth runs first (it's a dependency), so this never sees a bad token."""
    if _write_limiter is None or auth.AUTH_MODE != "entra" or user.is_pipeline:
        return
    if _write_limiter.reserve(user.oid) is None:
        raise HTTPException(429, "Too many changes in a short time - wait a moment and try again",
                            headers={"Retry-After": "60"})


WRITE_LIMIT = [Depends(limit_writes)]


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
    {"name": "admin", "description": "Registered-user overview and blocking, for the administrators named in "
        "ADMIN_OIDS (#82). Account data and counts only - never story content. 404 for everyone else."},
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
        "for the full data model and the AUTH_MODE options (send X-Token or a bearer token).",
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
    docs_url="/docs" if API_DOCS_ENABLED else None,
    redoc_url="/redoc" if API_DOCS_ENABLED else None,
    openapi_url="/openapi.json" if API_DOCS_ENABLED else None,
)
init_db()


# Security headers on every response (#69). The CSP only restricts what the
# pane may *load*; it has no frame-ancestors on purpose - Word Online and the
# other Office hosts embed the pane from a range of Microsoft origins, and a
# wrong list there would blank the pane in a way that can't be tested here.
# Override with CONTENT_SECURITY_POLICY (or "off") if a host needs more, without
# a rebuild.
DEFAULT_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' https://appsforoffice.microsoft.com",
    # Inline styles: the pane builds markup with style="..." attributes and
    # Quill positions its tooltips with inline styles.
    "style-src 'self' 'unsafe-inline'",
    # Research entries may embed external images (the sanitizer allows http, https and data:).
    "img-src 'self' data: blob: https: http:",
    "font-src 'self' data:",
    "connect-src 'self' https://login.microsoftonline.com https://appsforoffice.microsoft.com",
    "frame-src https://login.microsoftonline.com",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])
# `or`: a blank value (CONTENT_SECURITY_POLICY: "" in a compose file) means "use
# the default", not "no CSP" - turning it off takes an explicit "off".
CONTENT_SECURITY_POLICY = os.environ.get("CONTENT_SECURITY_POLICY", "").strip() or DEFAULT_CSP


def response_headers(path: str) -> list[tuple[str, str]]:
    """Cache-Control + security headers for a response to `path`.
    Cache-Control: /api/* gets no-store; everything else (the task pane's
    index.html and static assets) gets no-cache, so Word/a browser always
    revalidates instead of running a stale app.js after a deploy (#4).
    StaticFiles already sets ETag/Last-Modified and honours conditional
    GETs, so in practice that revalidation is a cheap 304 most of the time."""
    headers = [
        ("Cache-Control", "no-store" if path.startswith("/api/") else "no-cache"),
        ("X-Content-Type-Options", "nosniff"),
        # Not "no-referrer": research entries may embed external images, and
        # hosts with hotlink protection reject a request with no Referer at all.
        ("Referrer-Policy", "strict-origin-when-cross-origin"),
        ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"),
    ]
    # Swagger UI / ReDoc (only served with ENABLE_API_DOCS) use an inline script
    # and jsdelivr-hosted assets, which this policy forbids - they're a dev
    # tool, not the pane, so they're exempt rather than loosening it for all.
    if path not in ("/docs", "/redoc") and CONTENT_SECURITY_POLICY.lower() != "off":
        headers.append(("Content-Security-Policy", CONTENT_SECURITY_POLICY))
    return headers


class BodyTooLarge(Exception):
    """Raised from the wrapped `receive` the moment the request body passes
    its cap, so the rest of it is never read."""


class HardeningMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware) so it can cap the body *while it
    streams* (#69). Three concerns in one place so the ordering between them
    can't drift apart:
    - reject an oversized request body with 413: up front from Content-Length
      when the client states one, and otherwise the moment the bytes actually
      received pass the cap - so a chunked upload (no Content-Length) or a
      lying one can't make the server buffer gigabytes first
    - log every request to stdout: method, path, status, time taken - never
      the body or the X-Token header
    - Cache-Control + security headers on every response (response_headers)

    An unhandled exception (not an HTTPException - one of those is already a
    normal Response by the time it gets here) propagates out of this
    middleware untouched: Starlette installs the Exception/500 handler on
    ServerErrorMiddleware, which wraps *outside* this one. unhandled_
    exception_handler() below produces that response, so it does its own
    logging and header-setting rather than relying on this class, which it
    never reaches for that path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        path = scope["path"]
        # /api/import gets its own, larger cap: it's a whole series bundle in
        # one request rather than a single record, and research entries (#43)
        # can each carry embedded images.
        limit = IMPORT_MAX_BODY_BYTES if path == "/api/import" else MAX_BODY_BYTES
        status = None
        exceeded = False

        async def emit(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                for name, value in response_headers(path):
                    headers[name] = value
            await send(message)

        async def guarded_send(message):
            # FastAPI turns any exception raised while it parses a body into
            # its own generic 400 - once the cap has tripped, that (and
            # anything else the app says) is discarded and replaced by the
            # 413 sent below.
            if not exceeded:
                await emit(message)

        received = 0

        async def limited_receive():
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    exceeded = True
                    raise BodyTooLarge()
            return message

        too_large = JSONResponse({"detail": "Request body too large"}, status_code=413)
        declared = next((v for k, v in scope["headers"] if k == b"content-length"), None)
        if declared is not None and declared.isdigit() and int(declared) > limit:
            exceeded = True
        else:
            try:
                await self.app(scope, limited_receive, guarded_send)
            except BodyTooLarge:
                pass
        if exceeded and status is None:
            # (If the app had already started its own response before the cap
            # tripped there's nothing sane left to send; it's been truncated.)
            await too_large(scope, receive, emit)
        if status is not None:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("%s %s %s %.1fms", scope["method"], _sanitize_for_log(path), status, elapsed_ms)


# Compress responses over 1 KB when the client accepts gzip (#72): a large
# bundle is JSON and shrinks ~85x, which is most of the transfer time over a
# VPN or the reverse proxy. Added first so it sits inside HardeningMiddleware.
class SelectiveGZip:
    """GZipMiddleware, except for /assets/ (the PNG icons): already
    compressed, so gzip would burn CPU on the single worker for nothing.
    (Compressing API JSON is safe against BREACH-style attacks here: auth is
    a header, not a cookie, so another site can't make a victim's browser
    send an authenticated request whose size an observer could then probe.)"""

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].startswith("/assets/"):
            await self.app(scope, receive, send)
        else:
            await self.gzip(scope, receive, send)


app.add_middleware(SelectiveGZip)
app.add_middleware(HardeningMiddleware)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Without this, an unhandled exception (e.g. a raw sqlite3 error) gets
    Starlette's bare default 500 response - unlogged, with no Cache-Control
    set at all. This runs in ServerErrorMiddleware, outside
    HardeningMiddleware (see its docstring), so it has to do both itself."""
    path = request.scope["path"]  # same source HardeningMiddleware uses, so the two agree
    logger.exception("%s %s 500 (unhandled)", request.method, _sanitize_for_log(path))
    response = JSONResponse({"detail": "Internal server error"}, status_code=500)
    for name, value in response_headers(path):
        response.headers[name] = value
    return response


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """FastAPI parses the JSON body *before* it runs a route's dependencies,
    so a malformed-JSON POST from someone with no credentials at all used to
    get a 422 (and a free JSON parse) instead of a 401 (#69). Every other
    validation error is raised after the auth dependency has already
    succeeded, so only the JSON-syntax case needs the check repeated here.
    Authentication only: a signed-in caller who then fails a route-level
    permission check (require_person / require_access) still sees a 422 for
    malformed JSON rather than 403/404, since those checks need the parsed
    body's route context. It repeats get_current_user, so in entra mode that
    request pays for a second token validation - acceptable for a rare error
    path."""
    if request.url.path.startswith("/api/") and any(e.get("type") == "json_invalid" for e in exc.errors()):
        try:
            await run_in_threadpool(
                get_current_user, request.headers.get("authorization"), request.headers.get("x-token")
            )
        except HTTPException as denied:
            return JSONResponse({"detail": denied.detail}, status_code=denied.status_code)
    return await request_validation_exception_handler(request, exc)


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


def accessible_series_rows(con, user: auth.CurrentUser, columns: str = "*") -> list[sqlite3.Row]:
    """The series this caller can see, filtered by SQLite rather than by
    loading every series' JSON into Python and discarding most of it (#72)."""
    if auth.AUTH_MODE != "entra" or user.is_pipeline:
        return con.execute(f"SELECT {columns} FROM series").fetchall()
    return con.execute(
        f"SELECT {columns} FROM series WHERE owner_oid=? OR id IN (SELECT series_id FROM members WHERE oid=?)",
        (user.oid, user.oid),
    ).fetchall()


# ------------------------------------------------------- concurrency (#12)
# The two non-entra identities don't get a `users` row (see app/auth.py) -
# resolved here instead of on every request in the common no-auth case.
# #86: created_by/updated_by on other people's rows are rewritten to this when
# their author deletes their account.
DELETED_USER_OID = "deleted-user"
_SYNTHETIC_DISPLAY_NAMES = {auth.LOCAL_USER.oid: auth.LOCAL_USER.display_name, auth.SHARED_USER.oid: auth.SHARED_USER.display_name,
                            DELETED_USER_OID: "Deleted user"}


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
MAX_BACKUP_AGE_SECONDS = 36 * 3600


def _backup_ok() -> bool:
    """True if the last successful backup is under MAX_BACKUP_AGE_SECONDS
    old. A bare boolean - the exact age used to be its own unauthenticated
    route (/api/health/backup), which #68 folded in here so the only public
    API route is /api/health (plus /api/config, which the pane needs before
    it can sign in)."""
    age = backup_mod.last_backup_age_seconds()
    # 0 <= : a marker in the future (clock stepped back, volume restored from
    # a skewed host) must not read as "fresh" and silence the alert.
    return age is not None and 0 <= age <= MAX_BACKUP_AGE_SECONDS


class HealthResponse(BaseModel):
    ok: bool
    auth: bool
    version: str
    backup_ok: bool


@app.get("/api/health", tags=["health"], response_model=HealthResponse,
         responses={503: {"description": "Database unreachable, or the data directory isn't writable"}},
         description="Unauthenticated. `ok` and the status code reflect only the database and data "
                     "directory; `backup_ok` is false if no backup has succeeded in the last 36 hours.")
def health():
    # backup_ok deliberately does NOT feed into `ok`/the status code: Docker's
    # HEALTHCHECK restarts on a non-200, and a fresh install has no backup
    # yet for up to a day. Monitoring alerts on backup_ok=false separately
    # (docs/MONITORING.md).
    body: dict[str, Any] = {"ok": True, "auth": auth.AUTH_MODE != "none", "version": __version__,
                            "backup_ok": _backup_ok()}
    if not _db_ok():
        body.update(ok=False, error="database unavailable")
        return JSONResponse(body, status_code=503)
    if not _data_dir_writable_cached(Path(DB_PATH).parent):
        body.update(ok=False, error="data directory not writable")
        return JSONResponse(body, status_code=503)
    return body


# ---- auth (#10)
class ConfigOut(BaseModel):
    authMode: str
    tenantId: str
    clientId: str
    authority: str = ""     # the MSAL authority to sign in against (#91); "" outside entra mode
    privacyUrl: str = ""    # optional legal links shown before first sign-in
    termsUrl: str = ""
    policyVersion: str = ""  # current privacy policy / terms version (#111)


def _https_url(name: str) -> str:
    """An optional env-configured link, only ever a well-formed https URL: it is
    rendered as an href in the pane, so javascript:, data:, a bare "https://"
    or anything with whitespace is dropped."""
    v = os.environ.get(name, "").strip()
    u = urlparse(v)
    return v if u.scheme == "https" and u.netloc and not any(ch.isspace() for ch in v) else ""


@app.get("/api/config", tags=["auth"], response_model=ConfigOut)
def get_config():
    """Public (no auth) - the pane needs this before it has any way to
    authenticate, to know *how* to sign in (#13). Only ever the tenant/
    client id, authority and the public legal links, all already public in the
    app's own manifest/redirect URIs - nothing here is a secret."""
    entra = auth.AUTH_MODE == "entra"
    return {
        "authMode": auth.AUTH_MODE,
        "tenantId": auth.ENTRA_TENANT_ID,
        "clientId": auth.ENTRA_CLIENT_ID,
        "authority": auth.authority() if entra else "",
        "privacyUrl": _https_url("PRIVACY_URL"),
        "termsUrl": _https_url("TERMS_URL"),
        "policyVersion": POLICY_VERSION,
    }


class MeOut(BaseModel):
    oid: str
    email: str
    displayName: str
    isPipeline: bool
    isAdmin: bool = False
    policyVersion: str = ""       # the version they accepted ("" = none yet) (#111)
    policyAcceptedAt: float = 0
    policyCurrent: bool = True    # accepted the current version (always true without accounts)


def _me_out(user: auth.CurrentUser) -> dict:
    """The /api/me body. Policy state is read from the DB every time rather
    than the _seen_users cache, so a POLICY_VERSION bump is noticed at once.
    Anyone without a users row (none/token mode, the pipeline) counts as current."""
    out = {"oid": user.oid, "email": user.email, "displayName": user.display_name,
           "isPipeline": user.is_pipeline, "isAdmin": is_admin(user),
           "policyVersion": "", "policyAcceptedAt": 0, "policyCurrent": True}
    if auth.AUTH_MODE == "entra" and not user.is_pipeline:
        with db() as con:
            row = con.execute("SELECT policy_version, policy_accepted_at FROM users WHERE oid=?",
                              (user.oid,)).fetchone()
        if row:
            out.update(policyVersion=row["policy_version"], policyAcceptedAt=row["policy_accepted_at"],
                       policyCurrent=row["policy_version"] == POLICY_VERSION)
    return out


@app.get("/api/me", tags=["auth"], response_model=MeOut)
def get_me(user: auth.CurrentUser = Depends(get_current_user)):
    return _me_out(user)


class AcceptPolicyBody(BaseModel):
    version: str = Field(max_length=64)


@app.post("/api/me/accept-policy", tags=["auth"], response_model=MeOut, dependencies=WRITE_LIMIT)
def accept_policy(body: AcceptPolicyBody, user: auth.CurrentUser = Depends(require_person)):
    """Record that the caller accepted the privacy policy and terms (#111).
    `version` must be the current POLICY_VERSION, so a stale pane can't accept
    a version it never showed."""
    if auth.AUTH_MODE != "entra":
        raise HTTPException(400, "Accounts are only used when the server requires sign-in")
    if body.version != POLICY_VERSION:
        raise HTTPException(409, "The policy has changed - reload to see the current version")
    with db(write=True) as con:
        con.execute("UPDATE users SET policy_version=?, policy_accepted_at=? WHERE oid=?",
                    (POLICY_VERSION, time.time(), user.oid))
    return _me_out(user)


def delete_account_data(con: sqlite3.Connection, oid: str) -> dict[str, int]:
    """Erase everything held about `oid` (#86), inside the caller's write
    transaction: their users row, every series they own (records and member
    rows too), their memberships in other people's series, and their name on
    other people's rows (replaced by DELETED_USER_OID). Leaves blocked_users
    alone so deleting is no way round a block, and records a tombstone (oid +
    time only) so a restore from backup can re-apply the deletion. Also used
    by scripts/apply_tombstones.py."""
    owned = [r[0] for r in con.execute("SELECT id FROM series WHERE owner_oid=?", (oid,))]
    shared_with = 0
    for sid in owned:
        shared_with += con.execute("SELECT COUNT(*) FROM members WHERE series_id=?", (sid,)).fetchone()[0]
        con.execute("DELETE FROM members WHERE series_id=?", (sid,))
        con.execute("DELETE FROM records WHERE series_id=?", (sid,))
        con.execute("DELETE FROM series WHERE id=?", (sid,))
    left = con.execute("DELETE FROM members WHERE oid=?", (oid,)).rowcount
    for table in ("series", "records"):
        for col in ("created_by", "updated_by"):
            con.execute(f"UPDATE {table} SET {col}=? WHERE {col}=?", (DELETED_USER_OID, oid))
    con.execute("DELETE FROM users WHERE oid=?", (oid,))
    con.execute("INSERT OR REPLACE INTO deleted_users (oid, deleted_at) VALUES (?,?)", (oid, time.time()))
    return {"series_deleted": len(owned), "shared_with": shared_with, "memberships_left": left}


class DeleteAccountBody(BaseModel):
    confirm: str = Field("", max_length=320)


class AccountDeletedOut(BaseModel):
    deleted: bool
    series_deleted: int
    shared_with: int
    memberships_left: int


@app.delete("/api/me", tags=["auth"], response_model=AccountDeletedOut, dependencies=WRITE_LIMIT)
def delete_me(body: DeleteAccountBody, user: auth.CurrentUser = Depends(require_person)):
    """Right to erasure (#86): delete the caller's account and data - see
    delete_account_data for exactly what goes. `confirm` must be the
    caller's email (or the word DELETE) so a stray call can't wipe an
    account. Only meaningful with real accounts: in none/token mode there is
    one shared bible, which this must never wipe."""
    if auth.AUTH_MODE != "entra":
        raise HTTPException(400, "Accounts are only used when the server requires sign-in")
    typed = body.confirm.strip().lower()
    if not typed or typed not in {"delete", user.email.strip().lower()} - {""}:
        raise HTTPException(400, "Type your email address to confirm")
    with db(write=True) as con:
        result = delete_account_data(con, user.oid)
    global _block_epoch
    _block_epoch += 1
    _seen_users.pop((DB_PATH, user.oid), None)
    logger.info("user %s deleted their account", _sanitize_for_log(user.oid))
    return {"deleted": True, **result}


@app.get("/api/me/export", tags=["auth"])
def export_me(user: auth.CurrentUser = Depends(require_person)):
    """Download my data (#86, #100): the caller's account record, their
    memberships in other people's series, and every series they own in the
    usual bundle format."""
    with db() as con:
        u = con.execute("SELECT oid, email, display_name, tid, first_seen, last_seen, "
                        "policy_version, policy_accepted_at FROM users WHERE oid=?",
                        (user.oid,)).fetchone()
        memberships = [dict(r) for r in con.execute(
            "SELECT series_id, role FROM members WHERE oid=? ORDER BY series_id", (user.oid,))]
        series = [build_bundle(con, r) for r in con.execute(
            "SELECT * FROM series WHERE owner_oid=? ORDER BY id", (user.oid,))]
    return JSONResponse({"exported": time.time(), "account": dict(u) if u else {"oid": user.oid},
                         "memberships": memberships, "series": series},
                        headers={"Content-Disposition": 'attachment; filename="storybible-my-data.json"'})


class UserOut(BaseModel):
    oid: str
    email: str
    display_name: str


@app.get("/api/users", tags=["auth"], response_model=list[UserOut])
def list_users(user: auth.CurrentUser = Depends(require_person)):
    """Your contacts (#11, #88): you, plus the owners and members of every
    series you can access. With open signup (#85) the users table holds
    strangers, so it is no longer listed wholesale; to share with someone
    you don't yet share a series with, look them up by exact email via
    /api/users/lookup. AUTH_MODE none/token has one shared bible, so
    everyone is a contact there."""
    with db() as con:
        if auth.AUTH_MODE != "entra":
            rows = con.execute("SELECT oid, email, display_name FROM users ORDER BY display_name").fetchall()
        else:
            rows = con.execute(
                "WITH mine AS (SELECT id, owner_oid FROM series WHERE owner_oid=:me "
                "              OR id IN (SELECT series_id FROM members WHERE oid=:me)) "
                "SELECT oid, email, display_name FROM users WHERE oid=:me "
                "OR oid IN (SELECT owner_oid FROM mine) "
                "OR oid IN (SELECT oid FROM members WHERE series_id IN (SELECT id FROM mine)) "
                "ORDER BY display_name",
                {"me": user.oid},
            ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/users/lookup", tags=["auth"], response_model=UserOut)
def lookup_user(email: str | None = None, oid: str | None = None,
                user: auth.CurrentUser = Depends(require_person)):
    """Find one person by exact email (case-insensitive) or exact oid, so a
    series can be shared with someone new without enumerating the directory
    (#88). No partial or wildcard matching: LIKE metacharacters are just
    literal characters here. 404 if nobody matches."""
    email = (email or "").strip()
    if bool(email) == bool(oid):
        raise HTTPException(400, "Give exactly one of email or oid")
    with db() as con:
        if email:
            rows = con.execute(
                "SELECT oid, email, display_name FROM users WHERE lower(email)=lower(?)", (email,)
            ).fetchall()
        else:
            rows = con.execute("SELECT oid, email, display_name FROM users WHERE oid=?", (oid,)).fetchall()
    if len(rows) > 1:  # users.email isn't unique (e.g. a guest and a member account)
        raise HTTPException(409, "More than one account has that email - share using their oid instead")
    if not rows:
        raise HTTPException(404, "No matching user - they need to have signed in at least once")
    return dict(rows[0])


# ---- series
class AdminUserOut(BaseModel):
    oid: str
    email: str
    display_name: str
    first_seen: float
    last_seen: float
    series_owned: int
    series_shared: int
    records: dict[str, int]
    bytes_used: int
    blocked: bool
    blocked_at: float | None = None
    blocked_by: str | None = None
    blocked_reason: str | None = None


class AdminUsersOut(BaseModel):
    total: int
    users: list[AdminUserOut]


@app.get("/api/admin/users", tags=["admin"], response_model=AdminUsersOut)
def admin_list_users(limit: int = Query(500, ge=1, le=1000), admin: auth.CurrentUser = Depends(require_admin)):
    """Everyone who has signed in - blocked people first (a blocked person stops
    being active, so they would otherwise sink past `limit` and could never be
    unblocked), then most recently active - with how much
    each owns (#82). Counts and sizes only, never record contents. A handful
    of grouped queries rather than one per user. `last_seen` is refreshed at
    most every LAST_SEEN_REFRESH_SECONDS, so it is "roughly when"."""
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        users = con.execute(
            "SELECT u.oid, u.email, u.display_name, u.first_seen, u.last_seen, "
            "       b.blocked_at, b.blocked_by, b.reason "
            "FROM users u LEFT JOIN blocked_users b ON b.oid = u.oid "
            "ORDER BY b.blocked_at IS NULL, u.last_seen DESC LIMIT ?", (limit,)).fetchall()
        owned = dict(con.execute("SELECT owner_oid, COUNT(*) FROM series GROUP BY owner_oid").fetchall())
        shared = dict(con.execute("SELECT oid, COUNT(*) FROM members GROUP BY oid").fetchall())
        series_bytes = dict(con.execute(
            "SELECT owner_oid, SUM(LENGTH(CAST(data AS BLOB))) FROM series GROUP BY owner_oid").fetchall())
        kinds: dict[str, dict[str, int]] = {}
        rec_bytes: dict[str, int] = {}
        for owner, kind, n, size in con.execute(
                "SELECT s.owner_oid, r.kind, COUNT(*), SUM(LENGTH(CAST(r.data AS BLOB))) "
                "FROM records r JOIN series s ON s.id = r.series_id GROUP BY s.owner_oid, r.kind"):
            kinds.setdefault(owner, {})[kind] = n
            rec_bytes[owner] = rec_bytes.get(owner, 0) + size
    return {"total": total, "users": [{
        "oid": u["oid"], "email": u["email"], "display_name": u["display_name"],
        "first_seen": u["first_seen"], "last_seen": u["last_seen"],
        "series_owned": owned.get(u["oid"], 0), "series_shared": shared.get(u["oid"], 0),
        "records": {k: kinds.get(u["oid"], {}).get(k, 0) for k in KINDS},
        "bytes_used": (series_bytes.get(u["oid"]) or 0) + rec_bytes.get(u["oid"], 0),
        "blocked": u["blocked_at"] is not None, "blocked_at": u["blocked_at"],
        "blocked_by": u["blocked_by"], "blocked_reason": u["reason"],
    } for u in users]}


class BlockedOut(BaseModel):
    blocked: bool


class BlockBody(BaseModel):
    reason: str = Field("", max_length=500)


@app.put("/api/admin/users/{oid}/block", tags=["admin"], response_model=BlockedOut, dependencies=WRITE_LIMIT)
def admin_block_user(oid: str, body: BlockBody | None = None, admin: auth.CurrentUser = Depends(require_admin)):
    """Block a signed-in person (#82): every later request from them gets 403.
    Their data stays (blocking is not deleting). You can't block yourself or
    another administrator."""
    if oid.lower() == admin.oid.lower() or oid.lower() in auth.ADMIN_OIDS:
        raise HTTPException(400, "Administrators can't be blocked")
    with db(write=True) as con:
        if not con.execute("SELECT 1 FROM users WHERE oid=?", (oid,)).fetchone():
            raise HTTPException(404, "No such user")
        con.execute("INSERT OR REPLACE INTO blocked_users (oid, blocked_at, blocked_by, reason) VALUES (?,?,?,?)",
                    (oid, time.time(), admin.oid, (body.reason if body else "").strip()))
    # Take effect now, not after the 5-minute cache entry expires.
    global _block_epoch
    _block_epoch += 1
    _seen_users.pop((DB_PATH, oid), None)
    logger.info("admin %s blocked user %s", _sanitize_for_log(admin.oid), _sanitize_for_log(oid))
    return {"blocked": True}


@app.delete("/api/admin/users/{oid}/block", tags=["admin"], response_model=BlockedOut, dependencies=WRITE_LIMIT)
def admin_unblock_user(oid: str, admin: auth.CurrentUser = Depends(require_admin)):
    with db(write=True) as con:
        con.execute("DELETE FROM blocked_users WHERE oid=?", (oid,))  # idempotent: a double click is not an error
    logger.info("admin %s unblocked user %s", _sanitize_for_log(admin.oid), _sanitize_for_log(oid))
    return {"blocked": False}


@app.get("/api/series", tags=["series"])
def list_series(summary: bool = False, user: auth.CurrentUser = Depends(get_current_user)):
    """`?summary=true` returns just id/name/version/owner - what a series
    picker needs - instead of every series' full settings (a series
    description can be 200,000 characters, and this is called after every
    series change; #72). The default is unchanged for other API callers."""
    with db() as con:
        if summary:
            rows = accessible_series_rows(
                con, user,
                "id, json_extract(data, '$.name') AS name, updated, version, owner_oid, created_by, updated_by")
            out = [{"id": r["id"], "name": r["name"] or "", "updated": r["updated"], "version": r["version"],
                    "owner_oid": r["owner_oid"], "created_by": r["created_by"], "updated_by": r["updated_by"]}
                   for r in rows]
        else:
            out = [row_to_obj(r) for r in accessible_series_rows(con, user)]
    return sorted(out, key=lambda s: (s.get("name") or "").lower())


@app.post("/api/series", tags=["series"], dependencies=WRITE_LIMIT)
def create_series(body: Body, user: auth.CurrentUser = Depends(require_person)):
    sid = new_id()
    data = validate_series(clean(body.data))
    now = time.time()
    with db(write=True) as con:
        check_series_quota(con, user)
        check_storage_quota(con, user.oid, len(json.dumps(data)))
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid, version, created_by, updated_by) "
            "VALUES (?,?,?,?,1,?,?)",
            (sid, json.dumps(data), now, user.oid, user.oid, user.oid),
        )
    return {**data, "id": sid, "updated": now, "owner_oid": user.oid, "version": 1,
            "created_by": user.oid, "updated_by": user.oid}


@app.put("/api/series/{series_id}", tags=["series"], dependencies=WRITE_LIMIT)
def update_series(series_id: str, body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    now = time.time()
    data = validate_series(clean(body.data))
    with db(write=True) as con:
        row = require_access(con, user, series_id, "write")
        if body.version is None:
            raise HTTPException(428, "version is required")
        encoded = json.dumps(data)
        check_storage_quota(con, row["owner_oid"], len(encoded) - len(row["data"]))
        cur = con.execute(
            "UPDATE series SET data=?, updated=?, version=version+1, updated_by=? WHERE id=? AND version=?",
            (encoded, now, user.oid, series_id, body.version),
        )
        if cur.rowcount == 0:
            raise conflict(con, row_to_obj(get_series_or_404(con, series_id)))
        updated = row_to_obj(get_series_or_404(con, series_id))
    return updated


class DeletedOut(BaseModel):
    deleted: str


@app.delete("/api/series/{series_id}", tags=["series"], response_model=DeletedOut, dependencies=WRITE_LIMIT)
def delete_series(series_id: str, user: auth.CurrentUser = Depends(get_current_user)):
    with db() as con:
        require_access(con, user, series_id, "owner")
        con.execute("DELETE FROM series WHERE id=?", (series_id,))
    return {"deleted": series_id}


def bundle_etag(con, series_row: sqlite3.Row) -> str:
    """A cheap validator for GET /bundle (#72): the series' version and owner
    plus every record's (id, version), and the users table (display names
    appear in the bundle's `people` map). Every write bumps a version, so
    anything that would change the bundle changes this; nothing here parses a
    record's JSON. Weak (`W/`): the body also carries an `exported` timestamp,
    so it is equivalent, not byte-identical, between responses. Cost is one
    pass over (id, version) for the series plus the users table (a handful of
    rows); a display-name change invalidates every series' validator, which
    only costs those clients one re-download."""
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{__version__}|{series_row['id']}|{series_row['version']}|{series_row['owner_oid']}".encode())
    for r in con.execute("SELECT id, version FROM records WHERE series_id=? ORDER BY id", (series_row["id"],)):
        h.update(f"|{r['id']}:{r['version']}".encode())
    for r in con.execute("SELECT oid, display_name FROM users ORDER BY oid"):
        h.update(f"|{r['oid']}={r['display_name']}".encode())
    return f'W/"{h.hexdigest()}"'


def build_bundle(con, series_row: sqlite3.Row) -> dict[str, Any]:
    s = row_to_obj(series_row)
    rows = con.execute("SELECT * FROM records WHERE series_id=?", (series_row["id"],)).fetchall()
    by_kind: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for r in rows:
        by_kind[r["kind"]].append(row_to_obj(r))
    all_recs = [rec for recs in by_kind.values() for rec in recs]
    return {"series": s, "exported": time.time(), "people": people_map(con, s, *all_recs), **by_kind}


def export_bundle(series_id: str, user: auth.CurrentUser) -> dict[str, Any]:
    """The bundle as a plain dict, for in-process callers (the nightly backup)."""
    with db() as con:
        return build_bundle(con, require_access(con, user, series_id, "read"))


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    if header.strip() == "*":  # "if the resource exists at all" - it does, access was checked above
        return True
    bare = etag.removeprefix("W/")
    return any(t.strip().removeprefix("W/") == bare for t in header.split(","))


@app.get("/api/series/{series_id}/bundle", tags=["series"],
         responses={304: {"description": "Unchanged since the ETag in If-None-Match"}})
def bundle(series_id: str, request: Request, user: auth.CurrentUser = Depends(get_current_user)):
    """Everything for one series in a single call (also used as the export).
    Sends an ETag; a client that repeats it in If-None-Match gets a bodyless
    304 when nothing in the series has changed (#72)."""
    with db() as con:
        row = require_access(con, user, series_id, "read")
        etag = bundle_etag(con, row)
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers={"ETag": etag})
        data = build_bundle(con, row)
    return JSONResponse(data, headers={"ETag": etag})


@app.post("/api/import", tags=["series"], dependencies=WRITE_LIMIT)
def import_bundle(bundle_in: dict[str, Any], user: auth.CurrentUser = Depends(require_person)):
    """Restore an exported bundle as a NEW series (ids are remapped).

    Everything is validated and remapped up front, and only then is the write
    transaction opened to insert it (#71) - so a large or bad import never
    holds SQLite's single write lock while it is being checked."""
    if not isinstance(bundle_in.get("series"), dict):
        raise HTTPException(400, "Not a Story Bible export")
    for k in KINDS:
        recs = bundle_in.get(k, [])
        if not isinstance(recs, list) or not all(
            isinstance(r, dict) and isinstance(r.get("id"), str) and 0 < len(r["id"]) <= MAX_ID for r in recs
        ):
            raise HTTPException(400, f"'{k}' must be a list of records, each with a string id (1-{MAX_ID} characters)")
    all_ids = [rec["id"] for k in KINDS for rec in bundle_in.get(k, [])]
    if len(all_ids) != len(set(all_ids)):
        # Two records sharing an id would collide in idmap below and both
        # get remapped to the same new id, tripping the records.id primary
        # key on insert (a 500) instead of a clean 400 here.
        raise HTTPException(400, "Duplicate record id in import bundle")
    if len(all_ids) > MAX_RECORDS_PER_SERIES:
        raise HTTPException(400, f"The bundle has {len(all_ids)} records, the limit per series is {MAX_RECORDS_PER_SERIES}")

    idmap: dict[str, str] = {}
    sid = new_id()
    raw_sdata = clean(bundle_in["series"])
    name_was_given = "name" in raw_sdata
    sdata = validate_series(raw_sdata)
    if not name_was_given:
        sdata["name"] = "Imported"
    with db() as con:
        # (The series quota is checked below, inside the write lock, where it
        # can't race with another create.)
        # Only series this caller can see: comparing against everyone's
        # made the "(imported)" suffix reveal that somebody else has a
        # series with this name (#70).
        existing = {r["name"] for r in accessible_series_rows(con, user, "json_extract(data, '$.name') AS name")}
    if sdata["name"] in existing:
        suffix = " (imported)"
        sdata["name"] = sdata["name"][: MAX_SHORT - len(suffix)] + suffix  # stay inside the name limit
    for k in KINDS:
        for rec in bundle_in.get(k, []):
            idmap[rec["id"]] = new_id()
    valid = {k: {idmap[rec["id"]] for rec in bundle_in.get(k, [])} for k in KINDS}

    def remap(v):
        if isinstance(v, str):
            return idmap.get(v, v)
        if isinstance(v, list):
            return [remap(x) for x in v]
        if isinstance(v, dict):
            return {kk: remap(vv) for kk, vv in v.items()}
        return v

    prepared: list[tuple[str, str, dict]] = []
    dropped = 0
    for k in KINDS:
        for rec in bundle_in.get(k, []):
            raw = remap(clean(rec))
            # Scrub before validating: a dangling reference from old data can
            # be an arbitrary (even over-long) foreign id, which would
            # otherwise fail validation instead of simply being dropped.
            lost = scrub_refs(k, raw, valid)
            try:
                data = validate_record(k, raw)
            except HTTPException as e:
                raise HTTPException(400, f"{k} record '{rec['id']}': {e.detail}")
            dropped += lost
            if k == "relationships" and lost:
                dropped += 1  # a relationship missing an end is meaningless: skip it entirely
                continue
            prepared.append((idmap[rec["id"]], k, data))

    now = time.time()
    with db(write=True) as con:
        check_series_quota(con, user)  # again, now inside the write lock, so two imports can't both squeeze in
        check_storage_quota(con, user.oid, len(json.dumps(sdata)) + sum(len(json.dumps(d)) for _, _, d in prepared))
        con.execute(
            "INSERT INTO series (id, data, updated, owner_oid, version, created_by, updated_by) "
            "VALUES (?,?,?,?,1,?,?)",
            (sid, json.dumps(sdata), now, user.oid, user.oid, user.oid),
        )
        con.executemany(
            "INSERT INTO records (id, series_id, kind, data, updated, version, created_by, updated_by) "
            "VALUES (?,?,?,?,?,1,?,?)",
            [(rid, sid, k, json.dumps(data), now, user.oid, user.oid) for rid, k, data in prepared],
        )
    return {"id": sid, "name": sdata["name"], "dropped_references": dropped}


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


@app.put("/api/series/{series_id}/members/{oid}", tags=["sharing"], response_model=MemberRoleOut, dependencies=WRITE_LIMIT)
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


@app.delete("/api/series/{series_id}/members/{oid}", tags=["sharing"], response_model=DeletedOut, dependencies=WRITE_LIMIT)
def delete_member(series_id: str, oid: str, user: auth.CurrentUser = Depends(get_current_user)):
    """The owner can remove anyone; anyone can remove themselves (leave)."""
    if user.is_pipeline:
        raise HTTPException(403, "The review pipeline is read-only")
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


@app.post("/api/series/{series_id}/{kind}", tags=["records"], dependencies=WRITE_LIMIT)
def create_record(series_id: str, kind: str, body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    check_kind(kind)
    rid, now = new_id(), time.time()
    data = validate_record(kind, clean(body.data))
    with db(write=True) as con:
        row = require_access(con, user, series_id, "write")
        count = con.execute("SELECT COUNT(*) FROM records WHERE series_id=?", (series_id,)).fetchone()[0]
        if count >= MAX_RECORDS_PER_SERIES:
            raise HTTPException(400, f"This series already has {count} records, the limit is {MAX_RECORDS_PER_SERIES}")
        check_refs(con, series_id, kind, data)
        encoded = json.dumps(data)
        check_storage_quota(con, row["owner_oid"], len(encoded))
        con.execute(
            "INSERT INTO records (id, series_id, kind, data, updated, version, created_by, updated_by) "
            "VALUES (?,?,?,?,?,1,?,?)",
            (rid, series_id, kind, encoded, now, user.oid, user.oid),
        )
    return {**data, "id": rid, "updated": now, "version": 1, "created_by": user.oid, "updated_by": user.oid}


@app.put("/api/series/{series_id}/{kind}/{rid}", tags=["records"], dependencies=WRITE_LIMIT)
def update_record(series_id: str, kind: str, rid: str, body: Body, user: auth.CurrentUser = Depends(get_current_user)):
    check_kind(kind)
    now = time.time()
    data = validate_record(kind, clean(body.data))
    with db(write=True) as con:
        row = require_access(con, user, series_id, "write")
        if body.version is None:
            raise HTTPException(428, "version is required")
        current = con.execute(
            "SELECT version, data FROM records WHERE id=? AND series_id=? AND kind=?", (rid, series_id, kind)
        ).fetchone()
        if current and current["version"] == body.version:
            # Only for an up-to-date edit: a stale one falls through to the
            # UPDATE below and gets the 409 conflict prompt (a deleted
            # character's cascade bumps the versions of what referenced it),
            # not a confusing 400 about a reference the user never touched.
            check_refs(con, series_id, kind, data, frozenset(iter_refs(kind, json.loads(current["data"]))))
        encoded = json.dumps(data)
        if current:
            check_storage_quota(con, row["owner_oid"], len(encoded) - len(current["data"]))
        cur = con.execute(
            "UPDATE records SET data=?, updated=?, version=version+1, updated_by=? "
            "WHERE id=? AND series_id=? AND kind=? AND version=?",
            (encoded, now, user.oid, rid, series_id, kind, body.version),
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


@app.delete("/api/series/{series_id}/{kind}/{rid}", tags=["records"], response_model=DeletedOut, dependencies=WRITE_LIMIT)
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
async def submit_feedback(body: dict[str, Any], user: auth.CurrentUser = Depends(require_person)):
    """Files a GitHub issue from the pane's "Log Issue"/"Log Suggestion"
    buttons - see app/github_feedback.py. async because it awaits an
    outbound HTTPS call (httpx.AsyncClient) rather than blocking the
    single uvicorn worker on it."""
    feedback = feedback_mod.validate_feedback(body)
    return await feedback_mod.file_feedback(feedback, user)


# ---- static task pane
@app.get("/", include_in_schema=False)
def root():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
