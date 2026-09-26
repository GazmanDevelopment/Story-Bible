import asyncio
import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")  # mkstemp, not mktemp - see #28
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import app.backup as backup
import app.main as main
from fastapi.testclient import TestClient

c = TestClient(main.app)


def _seed_series(name: str) -> str:
    s = c.post("/api/series", json={"data": {"name": name}}).json()
    c.post(f"/api/series/{s['id']}/characters", json={"data": {"name": "Test Character", "age": "30"}})
    return s["id"]


# --------------------------------------------------------------------- run_backup
def test_backup_creates_a_real_openable_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    sid = _seed_series("Backup Copy Test")

    result = backup.run_backup()

    db_backup = result["db_backup"]
    assert db_backup.exists()
    con = sqlite3.connect(db_backup)
    try:
        row = con.execute("SELECT data FROM series WHERE id=?", (sid,)).fetchone()
        assert row is not None
        assert json.loads(row[0])["name"] == "Backup Copy Test"
    finally:
        con.close()

def test_backup_json_export_matches_the_series(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    sid = _seed_series("JSON Export Test")

    result = backup.run_backup()

    json_dir = result["json_dir"]
    assert json_dir.name == datetime.now().strftime("%Y%m%d")
    files = list(json_dir.glob(f"*-{sid}.json"))
    assert len(files) == 1
    exported = json.loads(files[0].read_text())
    assert exported["series"]["name"] == "JSON Export Test"
    assert exported["characters"][0]["name"] == "Test Character"

def test_export_survives_a_series_deleted_mid_run(tmp_path, monkeypatch):
    """A series deleted between list_series() and bundle() (get_series_or_404
    -> HTTPException) must not sink the whole night's backup - especially
    not the database copy, which by this point has already succeeded."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    good_sid = _seed_series("Survives Deletion Test")
    real_list_series = main.list_series
    monkeypatch.setattr(
        main, "list_series",
        lambda: real_list_series() + [{"id": "never-existed", "name": "ghost"}],
    )

    result = backup.run_backup()  # must not raise

    assert result["db_backup"].exists()
    files = list(result["json_dir"].glob(f"*-{good_sid}.json"))
    assert len(files) == 1  # the real series was still exported
    assert not list(result["json_dir"].glob("*-never-existed.json"))
    assert backup.last_backup_age_seconds() is not None  # marker still written

def test_backup_writes_a_fresh_success_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert backup.last_backup_age_seconds() is None  # nothing backed up here yet

    backup.run_backup()

    age = backup.last_backup_age_seconds()
    assert age is not None
    assert 0 <= age < 5

def test_rerunning_same_day_overwrites_not_duplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    sid = _seed_series("Rerun Test")

    backup.run_backup()
    result = backup.run_backup()  # same day - should reuse the day-folder

    files = list(result["json_dir"].glob(f"*-{sid}.json"))
    assert len(files) == 1


# ----------------------------------------------------------------------- retention
def test_keep_days_zero_disables_pruning_not_deletes_everything(tmp_path, monkeypatch):
    """A cutoff of "now" would otherwise read the backup run_backup() just
    made a moment ago as already-expired and delete it immediately."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "0")
    old = tmp_path / "storybible-20200101-000000.db"
    old.write_text("stale")
    old_time = time.time() - 20 * 86400
    os.utime(old, (old_time, old_time))

    backup.run_backup()

    remaining = {p.name for p in tmp_path.glob("storybible-*.db")}
    assert old.name in remaining  # nothing pruned, including the old one
    assert len(remaining) == 2    # the pre-existing file plus today's new one

def test_retention_prunes_old_db_backups_keeps_recent(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "14")
    old = tmp_path / "storybible-20200101-000000.db"
    old.write_text("stale")
    old_time = time.time() - 20 * 86400
    os.utime(old, (old_time, old_time))

    backup.run_backup()  # also creates today's own backup file

    remaining = {p.name for p in tmp_path.glob("storybible-*.db")}
    assert old.name not in remaining
    assert len(remaining) == 1

def test_retention_prunes_old_json_day_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_KEEP_DAYS", "14")
    old_dir = tmp_path / "json" / "20200101"
    old_dir.mkdir(parents=True)
    (old_dir / "x.json").write_text("{}")
    old_time = time.time() - 20 * 86400
    os.utime(old_dir, (old_time, old_time))

    backup.run_backup()

    remaining = {p.name for p in (tmp_path / "json").iterdir()}
    assert "20200101" not in remaining
    assert datetime.now().strftime("%Y%m%d") in remaining

def test_retention_failure_does_not_block_the_success_marker(tmp_path, monkeypatch):
    """The backup itself succeeding matters more than cleanup of old ones -
    see run_backup()'s docstring."""
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setattr(backup, "_prune_older_than", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))

    backup.run_backup()

    assert backup.last_backup_age_seconds() is not None


# ------------------------------------------------------------------- scheduling
def test_seconds_until_next_run_later_today():
    now = datetime(2026, 1, 1, 1, 0, 0)
    assert backup.seconds_until_next_run(3, now) == 2 * 3600

def test_seconds_until_next_run_already_passed_today():
    now = datetime(2026, 1, 1, 5, 0, 0)
    assert backup.seconds_until_next_run(3, now) == 22 * 3600

def test_seconds_until_next_run_exactly_now_rolls_to_tomorrow():
    now = datetime(2026, 1, 1, 3, 0, 0)
    assert backup.seconds_until_next_run(3, now) == 24 * 3600

def test_scheduler_survives_a_broken_backup_hour(monkeypatch):
    """A permanently-broken BACKUP_HOUR (bad env var) must not permanently
    kill the background scheduler task - nothing else notices or restarts
    it, so that would silently disable all future backups until a
    restart. It should log and retry instead."""
    monkeypatch.setenv("BACKUP_HOUR", "not-a-number")
    monkeypatch.setattr(backup, "_SCHEDULER_RETRY_DELAY", 0)  # don't really wait an hour

    async def drive_briefly():
        task = asyncio.create_task(backup.scheduler())
        await asyncio.sleep(0.05)  # let a few failing iterations happen
        still_running = not task.done()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return still_running

    assert asyncio.run(drive_briefly()) is True


# --------------------------------------------------------------- /api/health/backup
def test_health_backup_503_when_never_run(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    r = c.get("/api/health/backup")
    assert r.status_code == 503
    assert r.json()["ok"] is False

def test_health_backup_200_after_a_fresh_backup(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backup.run_backup()
    r = c.get("/api/health/backup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["age_seconds"] < 5

def test_health_backup_503_when_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    backup.run_backup()
    stale = time.time() - 37 * 3600
    (tmp_path / ".last_success").write_text(str(stale))

    r = c.get("/api/health/backup")

    assert r.status_code == 503
    assert r.json()["error"] == "backup is stale"

def test_health_backup_not_cached():
    r = c.get("/api/health/backup")
    assert r.headers.get("cache-control") == "no-store"
