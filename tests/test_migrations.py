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
    con.execute("INSERT INTO series VALUES ('s1','{}',0)")
    con.execute("INSERT INTO records VALUES ('r1','s1','characters','{}',0)")
    assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
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
