# Backups

Two things happen automatically, once a day:

- **A consistent database copy**, via SQLite's `VACUUM INTO`. This is safe
  to run against the live database (WAL mode, other connections still
  reading/writing) and produces a single, self-contained, up-to-date file -
  unlike a raw filesystem/ZFS snapshot of a WAL-mode database, which isn't
  guaranteed to catch the main file and the `-wal` file at the same instant.
- **A JSON export of every series**, in the same shape as the pane's own
  "Export JSON" button. This is the format most likely to still make sense
  after a future schema change, so it's worth keeping even though the
  database copy above is the primary backup.

Both land under `BACKUP_DIR` (default: a `backups/` folder next to the
database, so `/data/backups` in the container):

```
/data/backups/storybible-20260214-030000.db
/data/backups/json/20260214/<series name>-<id>.json
/data/backups/.last_success        (an internal marker, not a backup)
```

Backups older than `BACKUP_KEEP_DAYS` (default 14) are deleted after each
run.

## Running it

- **Automatically**: the app runs this itself, once a day at `BACKUP_HOUR`
  local time (default 3am - see `TZ` in the container's environment for
  what "local" means). No separate cron job needed.
- **By hand**: `python -m app.backup`, using the same `STORYBIBLE_DB` (and
  optionally `BACKUP_DIR`/`BACKUP_KEEP_DAYS`) the server itself uses.

## Monitoring

`GET /api/health` (unauthenticated - a monitoring tool won't have
`STORYBIBLE_TOKEN` either) includes `"backup_ok": true` if the last
successful backup is under 36 hours old, and `false` if it's older than that
or one has never succeeded. (This used to be a separate
`/api/health/backup` route; it was folded in so `/api/health` is the only
public API route, #68.) See [MONITORING.md](MONITORING.md) for actually wiring an uptime checker
to this (and to `/api/health`) so a silently-broken backup job doesn't go
unnoticed for months.

## What these backups do and do not protect against

The backups above are written **into the same dataset as the live database**
(`/data/backups`), so they protect against corruption, a bad edit, or an
accidental delete inside the app - but **not against the pool or the NAS
itself being lost** (drive failure, theft, fire, ransomware on the box).

They are also **plaintext**: a backup is every series, readable by anyone who
can read the files. The app creates the folders `0700` and the files `0600`
(owner-only), but that is only a guard against other accounts on the same
machine - it is not encryption, and it says nothing about whoever administers
the NAS (`SECURITY.md` records that as accepted).

## TrueNAS: snapshot the dataset too, and copy it off the box

A periodic ZFS snapshot task on the app's dataset (e.g. `tank/apps/storybible`,
hourly or daily, however much history you want) gives you point-in-time
history that survives a bad deletion of the backup files themselves. **It does
not survive losing the pool**, because a snapshot lives in the same pool.

For that, send a copy **somewhere else**: a ZFS replication task to a second
machine or pool, or a Cloud Sync / rsync task to storage you trust with the
contents (encrypt it if it leaves your network). Test a restore from the
off-box copy once ([RESTORE.md](RESTORE.md)) - an untested backup is a hope, not
a backup.

## Restoring

See [RESTORE.md](RESTORE.md).
