import os
import sqlite3
import tempfile

from app.migrations import SCHEMA_VERSION, migrate


def _new_db() -> sqlite3.Connection:
    # mkstemp(), not mktemp(): the latter is a TOCTOU race between naming
    # the file and creating it (CodeQL py/insecure-temporary-file, #28).
    # sqlite3.connect() is happy to open the pre-created empty file as a
    # fresh database.
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.isolation_level = None  # autocommit; migrate() drives its own transactions
    return con


def test_fresh_db_reaches_latest_version():
    con = _new_db()
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # tables exist and are usable
    con.execute("INSERT INTO series (id, data, updated) VALUES ('s1','{}',0)")
    con.execute("INSERT INTO records (id, series_id, kind, data, updated) VALUES ('r1','s1','characters','{}',0)")
    assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    con.close()


def test_v3_ownership_and_sharing_schema():
    con = _new_db()
    migrate(con)
    con.execute("INSERT INTO series (id, data, updated) VALUES ('s1', '{}', 0)")
    # owner_oid defaults to '' for a series inserted without one (matches
    # what a pre-#11 database's existing rows look like after upgrading)
    assert con.execute("SELECT owner_oid FROM series WHERE id='s1'").fetchone()[0] == ""

    con.execute("PRAGMA foreign_keys = ON")
    con.execute("INSERT INTO members (series_id, oid, role) VALUES ('s1', 'u1', 'editor')")
    assert con.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 1
    con.execute("DELETE FROM series WHERE id='s1'")
    assert con.execute("SELECT COUNT(*) FROM members").fetchone()[0] == 0  # cascade delete
    con.close()


def test_pre_migration_db_upgrades_cleanly():
    """A database written before migrations existed (tables already
    present, user_version still 0 by default) should upgrade with no
    errors and no data loss."""
    con = _new_db()
    con.execute(
        """CREATE TABLE series (
               id TEXT PRIMARY KEY, data TEXT NOT NULL, updated REAL NOT NULL)"""
    )
    con.execute(
        """CREATE TABLE records (
               id TEXT PRIMARY KEY, series_id TEXT NOT NULL, kind TEXT NOT NULL,
               data TEXT NOT NULL, updated REAL NOT NULL)"""
    )
    con.execute("""INSERT INTO series VALUES ('s1', '{"name":"Existing"}', 0)""")
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0

    migrate(con)

    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    row = con.execute("SELECT data FROM series WHERE id='s1'").fetchone()
    assert row[0] == '{"name":"Existing"}'
    con.close()


def test_rerun_is_a_noop():
    con = _new_db()
    migrate(con)
    migrate(con)  # should not error or re-run migration 1
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    con.close()


def test_refuses_newer_database():
    con = _new_db()
    con.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    try:
        migrate(con)
        assert False, "expected RuntimeError for a database newer than this build"
    except RuntimeError as e:
        assert "newer" in str(e)
    # left untouched, not silently downgraded
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
    con.close()


def test_failed_migration_rolls_back_and_leaves_version_unchanged(monkeypatch):
    import app.migrations as migrations

    def boom(con):
        con.execute("CREATE TABLE series (id TEXT PRIMARY KEY)")  # a real change...
        raise RuntimeError("simulated failure partway through")   # ...that should not stick

    monkeypatch.setattr(migrations, "MIGRATIONS", [boom])
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 1)
    con = _new_db()
    try:
        migrate(con)
        assert False, "expected the simulated failure to propagate"
    except RuntimeError as e:
        assert "simulated failure" in str(e)
    assert con.execute("PRAGMA user_version").fetchone()[0] == 0
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    assert tables == []  # the CREATE TABLE was rolled back
    con.close()


def test_v6_adds_blocked_users_and_upgrades_a_v5_database():
    """#82: blocks live in their own table, with no foreign key to users, so
    removing a user row doesn't remove the block."""
    from app.migrations import MIGRATIONS
    con = _new_db()
    for v in range(1, 6):
        con.execute("BEGIN IMMEDIATE"); MIGRATIONS[v - 1](con); con.execute(f"PRAGMA user_version = {v}"); con.commit()
    con.execute("INSERT INTO users (oid, first_seen, last_seen) VALUES ('u', 0, 0)")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 6
    con.execute("INSERT INTO blocked_users (oid, blocked_at, blocked_by) VALUES ('u', 1, 'admin')")
    con.execute("DELETE FROM users WHERE oid='u'")
    assert con.execute("SELECT reason FROM blocked_users WHERE oid='u'").fetchone() == ("",)
    con.close()


def test_v7_adds_users_tid_and_upgrades_a_v6_database():
    """#90: the tenant a person signed in from; existing rows get ''."""
    from app.migrations import MIGRATIONS
    con = _new_db()
    for v in range(1, 7):
        con.execute("BEGIN IMMEDIATE"); MIGRATIONS[v - 1](con); con.execute(f"PRAGMA user_version = {v}"); con.commit()
    con.execute("INSERT INTO users (oid, first_seen, last_seen) VALUES ('u', 0, 0)")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 7
    assert con.execute("SELECT tid FROM users WHERE oid='u'").fetchone() == ("",)
    con.close()


def test_v8_adds_deleted_users_tombstones_and_upgrades_a_v7_database():
    """#86: oid + time only, no foreign key, so it outlives the users row."""
    from app.migrations import MIGRATIONS
    con = _new_db()
    for v in range(1, 8):
        con.execute("BEGIN IMMEDIATE"); MIGRATIONS[v - 1](con); con.execute(f"PRAGMA user_version = {v}"); con.commit()
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 8
    cols = [r[1] for r in con.execute("PRAGMA table_info(deleted_users)")]
    assert cols == ["oid", "deleted_at"]
    con.close()


def test_v9_adds_policy_acceptance_columns_and_upgrades_a_v8_database():
    """#111: existing users start with no accepted version, so they are asked once."""
    from app.migrations import MIGRATIONS
    con = _new_db()
    for v in range(1, 9):
        con.execute("BEGIN IMMEDIATE"); MIGRATIONS[v - 1](con); con.execute(f"PRAGMA user_version = {v}"); con.commit()
    con.execute("INSERT INTO users (oid, first_seen, last_seen) VALUES ('u', 0, 0)")
    migrate(con)
    assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 9
    assert con.execute("SELECT policy_version, policy_accepted_at FROM users WHERE oid='u'").fetchone() == ("", 0)
    con.close()
