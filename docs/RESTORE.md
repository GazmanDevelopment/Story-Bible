# Restoring from a backup

Two kinds of backup exist (see [BACKUP.md](BACKUP.md)): a full database copy
(`storybible-YYYYMMDD-HHMMSS.db`) and a per-series JSON export. Prefer the
database copy - it's a complete, exact snapshot; the JSON export is the
fallback if the database copy itself is unavailable or from further back
than you'd like (e.g. the schema has since changed and you want history
imported into a *current* database rather than an old file swapped in
directly).

## Restore from a database backup

1. Stop the app (`docker stop <container>`, or bring the Custom App down on
   TrueNAS).
2. Pick the backup to restore, e.g.
   `/data/backups/storybible-20260214-030000.db`.
3. Move the *live* database out of the way rather than deleting it
   outright, in case you picked the wrong file:
   ```
   mv /data/storybible.db /data/storybible.db.before-restore
   ```
4. Remove any leftover WAL/SHM files next to it - they belong to the old
   database, not the one you're about to put in its place:
   ```
   rm -f /data/storybible.db-wal /data/storybible.db-shm
   ```
5. Copy the backup into place:
   ```
   cp /data/backups/storybible-20260214-030000.db /data/storybible.db
   ```
6. Start the app again and confirm `/api/health` reports `"ok": true`, and
   that the data in the pane looks like what you expect.
7. Once you're confident it's correct, remove the `.before-restore` file
   from step 3.

## Restore from a JSON export

Each file under `/data/backups/json/<date>/` is one series, in the same
format the pane's Series tab imports (Series tab → **Import JSON**). This
creates each series as a **new** series (ids are remapped, per
`POST /api/import`) rather than overwriting anything, so it's safe to try
without touching the live database at all.

## Doing an actual restore drill

The steps above are the mechanics; a drill is *actually doing them* against
a real backup and confirming the result, so the runbook is trustworthy
before you need it for real. Do this at least once after deploying, and
again after any change to the backup/restore process itself:

1. Start a second, throwaway container from the same image, with its own
   empty `/data` volume, on a different port.
2. Copy a real backup file from the production dataset into that
   container's `/data`, following the steps above.
3. Start the throwaway container and check the data is intact.
4. Tear it down - nothing here should touch the production container or
   dataset.

This part needs a real Docker/TrueNAS environment to run, so it hasn't been
executed as part of this change (see the automated test in
`tests/test_backup.py` for what *has* been verified without one: that
`run_backup()` produces a database file that genuinely opens and contains
the expected rows, and a JSON export that round-trips through
`POST /api/import`). Treat this drill as an open item until it's been run
for real at least once.
