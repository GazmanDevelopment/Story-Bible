"""
Re-apply account deletions after restoring a backup (#86).

    python scripts/apply_tombstones.py /data/storybible.db [more.db ...]

A restored database predates any account deletions made since the backup was
taken, so those people's data would reappear. Deletions are recorded as
tombstones (oid + time) in the `deleted_users` table. Copy the live
database's tombstones into the restored one first (docs/RESTORE.md does this
with sqlite3's ATTACH), then run this: it erases each tombstoned oid's data
again using the same code as DELETE /api/me. Safe to run repeatedly.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import delete_account_data  # noqa: E402
from app.migrations import migrate  # noqa: E402


def apply(db_path: str) -> int:
    con = sqlite3.connect(db_path, isolation_level=None)
    con.row_factory = sqlite3.Row
    try:
        migrate(con)
        # Skip anyone whose account in this database was created *after* they
        # deleted (they signed up again; that is a fresh account, not the one
        # that was erased).
        oids = [r[0] for r in con.execute(
            "SELECT d.oid FROM deleted_users d LEFT JOIN users u ON u.oid = d.oid "
            "WHERE u.oid IS NULL OR u.first_seen <= d.deleted_at")]
        con.execute("BEGIN IMMEDIATE")
        for oid in oids:
            delete_account_data(con, oid)
        con.execute("COMMIT")
        return len(oids)
    finally:
        con.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    for path in sys.argv[1:]:
        print(f"{path}: re-applied {apply(path)} deletion(s)")
