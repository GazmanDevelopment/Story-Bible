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

`GET /api/health/backup` (unauthenticated, like `/api/health` - a
monitoring tool won't have `STORYBIBLE_TOKEN` either) returns:

- **200** `{"ok": true, "age_seconds": ...}` if the last successful backup
  is under 36 hours old.
- **503** if it's older than that, or if a backup has never succeeded.

Point an uptime check (Uptime Kuma or similar - see #17) at this URL so a
silently-broken backup job doesn't go unnoticed for months.

## TrueNAS: snapshot the dataset too

Backups above live *inside* the same dataset as the live database, which
protects against corruption or a bad edit but not against the drive/pool
itself failing. Add a periodic ZFS snapshot task on the app's dataset (e.g.
`tank/apps/storybible`, hourly or daily, however much history you want) so
there's a copy outside the container/dataset's own filesystem entirely.

## Restoring

See [RESTORE.md](RESTORE.md).
