"""
Backups for the Story Bible SQLite database (#5).

Two things happen on each backup run:
- a consistent full copy of the database, via SQLite's own `VACUUM INTO`.
  Safe to run against the live, WAL-mode database - unlike a raw
  filesystem/ZFS snapshot, which isn't guaranteed to catch the main file
  and the -wal file at the same instant, VACUUM INTO reads a single
  consistent view (as of when it starts) and writes it straight to the
  destination file.
- a JSON export of every series (the same shape as the pane's own "Export
  JSON" button), the format most likely to still make sense after a future
  schema change.

Both land under backup_dir() (default: a `backups/` folder next to the
database). Retention (BACKUP_KEEP_DAYS, default 14) prunes older backups of
both kinds after each run - a retention failure is logged but doesn't fail
the run, since the important signal for monitoring is "do we have a recent,
valid backup", not "did cleanup of old ones also succeed".

Run manually with `python -m app.backup`. Inside the running app,
scheduler() runs it automatically once a day, at BACKUP_HOUR local time
(see app/main.py's lifespan).

Env vars:
  BACKUP_DIR         where backups are written (default: <db dir>/backups)
  BACKUP_KEEP_DAYS   how many days of backups to keep (default 14)
  BACKUP_HOUR        local hour (0-23) the in-app scheduler runs at (default 3)

Note on this module's style: unlike app/main.py's DB_PATH/TOKEN/etc (read
once at import time), the settings here are read fresh on every call rather
than cached as module constants. That's deliberate: it avoids this module
needing to import app.main at the top level (app.main will import *this*
module, to wire up the scheduler and /api/health/backup - reading env vars
lazily, inside functions, sidesteps the circular import instead of needing
either side to special-case it), and it makes tests able to just
monkeypatch an environment variable rather than reach into module state.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


def backup_dir() -> Path:
    env = os.environ.get("BACKUP_DIR", "").strip()
    if env:
        return Path(env)
    from . import main as app_main  # lazy: see module docstring
    return Path(app_main.DB_PATH).parent / "backups"


def keep_days() -> int:
    return int(os.environ.get("BACKUP_KEEP_DAYS", 14))


def backup_hour() -> int:
    return int(os.environ.get("BACKUP_HOUR", 3))


def _marker_path() -> Path:
    return backup_dir() / ".last_success"


def last_backup_age_seconds() -> float | None:
    """Seconds since the last successful run_backup(), or None if there
    has never been one (a fresh install, or backups have never run)."""
    try:
        return time.time() - float(_marker_path().read_text())
    except (OSError, ValueError):
        return None


def _backup_database(bdir: Path) -> Path:
    from . import main as app_main
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"storybible-{stamp}.db"
    # VACUUM INTO refuses to overwrite an existing file, and the timestamp
    # above is only second-resolution - two runs within the same second
    # (a manual retry right after a scheduled run, for instance) would
    # otherwise collide. Not a TOCTOU race worth worrying about here: this
    # app runs backups from a single process at a time, never concurrently.
    suffix = 1
    while dest.exists():
        dest = bdir / f"storybible-{stamp}-{suffix}.db"
        suffix += 1
    # sqlite3.Connection's context manager only commits/rolls back the
    # transaction on exit - it does not close the connection (a common
    # surprise; app/main.py's db() closes explicitly for the same reason).
    con = sqlite3.connect(app_main.DB_PATH)
    try:
        con.execute("VACUUM INTO ?", (str(dest),))
    finally:
        con.close()
    return dest


def _export_json(json_root: Path) -> Path:
    """Exports every series into json_root/YYYYMMDD/. Re-running on the
    same day overwrites that day's files - one export per day is what
    the retention/folder-naming scheme is built around, not one per run."""
    from fastapi import HTTPException

    from . import auth
    from . import main as app_main
    day_dir = json_root / datetime.now().strftime("%Y%m%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    # A direct in-process call, not a real request - list_series/bundle's
    # `user` param needs a real CurrentUser. SYSTEM_USER (is_pipeline=True)
    # sees every series regardless of AUTH_MODE/ownership, which a backup
    # of the whole database must (#10/#11 are about API callers, not this).
    for s in app_main.list_series(user=auth.SYSTEM_USER):
        try:
            data = app_main.bundle(s["id"], user=auth.SYSTEM_USER)
        except HTTPException:
            # Deleted between list_series() and here. Not run_backup()'s
            # problem to fail over - skip it rather than losing the whole
            # night's backup (including the database copy that already
            # succeeded) to one series' bad timing.
            app_main.logger.info("backup: series %s vanished mid-export, skipping", s["id"])
            continue
        safe_name = re.sub(r"[^\w-]+", "_", data["series"].get("name") or "series").strip("_") or "series"
        (day_dir / f"{safe_name}-{s['id']}.json").write_text(json.dumps(data, indent=2))
    return day_dir


def _prune_older_than(dir_: Path, pattern: str, days: int) -> None:
    """Deletes whatever in `dir_` matches `pattern` and is older than
    `days` - a plain file (a *.db backup) or a directory (a json/ day-
    folder) alike, since retention needs to prune both the same way.

    days <= 0 means "keep forever" and skips pruning entirely - not just
    for a sensible "disable retention" reading of BACKUP_KEEP_DAYS=0, but
    because a cutoff of "now" would otherwise delete the backup run_backup()
    just made moments earlier in the same run (its mtime is a moment
    before "now", same as everything else here, so it would read as
    already-expired)."""
    if days <= 0 or not dir_.exists():
        return
    cutoff = time.time() - days * 86400
    for p in dir_.glob(pattern):
        if p.stat().st_mtime >= cutoff:
            continue
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()


def run_backup() -> dict[str, Any]:
    """One full backup cycle. Returns a small summary (also printed by the
    CLI entry point below). The success marker is written last, and only
    if the backup and export themselves succeeded - a retention failure
    doesn't stop it from being written."""
    from . import main as app_main

    bdir = backup_dir()
    db_backup = _backup_database(bdir)
    json_dir = _export_json(bdir / "json")

    for label, dir_, pattern in (
        ("database backups", bdir, "storybible-*.db"),
        ("json export folders", bdir / "json", "*"),
    ):
        try:
            _prune_older_than(dir_, pattern, keep_days())
        except Exception:
            app_main.logger.exception("backup retention (%s) failed", label)

    _marker_path().write_text(str(time.time()))
    return {"db_backup": db_backup, "json_dir": json_dir}


def seconds_until_next_run(hour: int, now: datetime | None = None) -> float:
    now = now or datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


_SCHEDULER_RETRY_DELAY = 3600  # after a failure, before trying the loop again


async def scheduler() -> None:
    """Runs forever (until cancelled): sleeps until the next BACKUP_HOUR,
    runs a backup in a worker thread (VACUUM INTO/file I/O are blocking -
    this keeps them off the event loop that's also serving requests).

    The whole loop body is one try/except, not just the backup call: an
    invalid BACKUP_HOUR (or any other failure computing the schedule) must
    not permanently kill this background task, since nothing else notices
    or restarts it - a config mistake would otherwise silently disable all
    future backups for the life of the process. On failure it retries in
    an hour rather than spinning tightly on an error that a bad env var
    won't fix on its own between one iteration and the next.
    """
    from . import main as app_main
    while True:
        try:
            await asyncio.sleep(seconds_until_next_run(backup_hour()))
            await asyncio.to_thread(run_backup)
        except Exception:
            app_main.logger.exception("scheduled backup failed")
            await asyncio.sleep(_SCHEDULER_RETRY_DELAY)


if __name__ == "__main__":
    result = run_backup()
    print(f"Database backup: {result['db_backup']}")
    print(f"JSON export:     {result['json_dir']}")
