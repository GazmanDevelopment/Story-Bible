"""
Schema migrations for the Story Bible SQLite database.

Each migration is a plain function that takes a connection and applies
DDL. Migrations are numbered from 1 and tracked with SQLite's built-in
``PRAGMA user_version``, so upgrading a live database is automatic: on
startup the app runs whichever migrations haven't been applied yet, each
in its own transaction, and refuses to start if the database's version is
newer than this build understands (an old release running against a
database a newer one already upgraded).

To add a migration: write a new `_vN_...` function and append it to
MIGRATIONS. Never edit an already-shipped migration - write a new one
instead, even to fix a mistake, since live databases may already be past
it.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

Migration = Callable[[sqlite3.Connection], None]


def _v1_initial_schema(con: sqlite3.Connection) -> None:
    """The tables the demo shipped with. IF NOT EXISTS so a database
    written before migrations existed (tables already present, user_version
    still 0) upgrades cleanly: this just records that its schema matches
    version 1, with no data touched."""
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


# Ordered by version: MIGRATIONS[0] is version 1, MIGRATIONS[1] is version 2, etc.
MIGRATIONS: list[Migration] = [
    _v1_initial_schema,
]

SCHEMA_VERSION = len(MIGRATIONS)


def migrate(con: sqlite3.Connection) -> None:
    """Bring `con`'s database up to SCHEMA_VERSION.

    Every pending migration runs in its own transaction (commit per step,
    not one big transaction for the whole run), so a failure partway
    through leaves the database at the last fully-applied version rather
    than in a half-migrated state.

    `con` must be in autocommit mode (`con.isolation_level = None`) so the
    explicit BEGIN/COMMIT/ROLLBACK below has full control - including over
    the CREATE TABLE/INDEX statements migrations issue, which Python's
    sqlite3 module does not otherwise wrap in a transaction the same way.
    """
    current = con.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema version {current} is newer than this build of "
            f"Story Bible understands (latest known: {SCHEMA_VERSION}). "
            "Refusing to start - is an older version running against a "
            "database a newer version already upgraded?"
        )
    for version in range(current + 1, SCHEMA_VERSION + 1):
        migration = MIGRATIONS[version - 1]
        con.execute("BEGIN IMMEDIATE")
        try:
            migration(con)
            # PRAGMA doesn't accept bound parameters; `version` is our own int, not input.
            con.execute(f"PRAGMA user_version = {version}")
        except Exception:
            con.rollback()
            raise
        else:
            con.commit()
