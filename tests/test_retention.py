"""
#113: inactive-account email notices and deletion (app/retention.py), the
mailer (app/mailer.py) and their wiring into the app's lifespan.

Each test gets its own database (main.DB_PATH pointed at a temp file), a fake
mailer and a fixed `now`, with users' last_seen backdated relative to it.
"""
import asyncio
import logging
import os
import smtplib
import sqlite3
import tempfile
import time

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import pytest
from fastapi.testclient import TestClient

from app import auth, backup, mailer, main, retention

NOW = 2_000_000_000.0
MONTH = retention.MONTH_SECONDS
DAY = 86400
ADMIN = "ret-admin"


class FakeMail:
    def __init__(self, on=True, ok=True):
        self.on, self.ok, self.sent = on, ok, []

    def enabled(self):
        return self.on

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return self.ok


@pytest.fixture(autouse=True)
def fresh_db(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "retention.db"))
    main.init_db()
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    monkeypatch.setattr(auth, "ADMIN_OIDS", {ADMIN})
    for var in ("INACTIVE_DELETE_MONTHS", "INACTIVE_NOTICE_MONTHS", "RETENTION_HOUR",
                "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(var, raising=False)


def _user(oid, months_ago, email=None, stage=0, name=None):
    last = NOW - months_ago * MONTH
    with main.db() as con:
        con.execute("INSERT INTO users (oid, email, display_name, tid, first_seen, last_seen, "
                    "inactive_notice_stage) VALUES (?,?,?,?,?,?,?)",
                    (oid, f"{oid}@example.com" if email is None else email,
                     name if name is not None else f"Name {oid}", "t", last, last, stage))
        con.execute("INSERT INTO series (id, data, updated, owner_oid) VALUES (?,?,?,?)",
                    (f"s-{oid}", '{"name": "S"}', last, oid))


def _stage(oid):
    with main.db() as con:
        row = con.execute("SELECT inactive_notice_stage FROM users WHERE oid=?", (oid,)).fetchone()
    return None if row is None else row[0]


def _exists(oid):
    return _stage(oid) is not None


def _tombstoned(oid):
    with main.db() as con:
        return con.execute("SELECT 1 FROM deleted_users WHERE oid=?", (oid,)).fetchone() is not None


def _run(mail=None, now=NOW):
    return retention.run_retention(now=now, mail=mail if mail is not None else FakeMail())


# ------------------------------------------------------------------ schedule
@pytest.mark.parametrize("months,stage", [(17.9, 0), (18.1, 1), (20.1, 2), (23.1, 3)])
def test_each_notice_is_sent_at_its_month(months, stage):
    _user("u", months)
    mail = FakeMail()
    _run(mail)
    assert _stage("u") == stage
    assert len(mail.sent) == (1 if stage else 0)


def test_notices_progress_and_each_is_sent_once():
    _user("u", 0)
    mail = FakeMail()
    last_seen = NOW
    sent_at = []
    for day in range(0, int(24 * 30.4375) - 1):
        now = last_seen + day * DAY
        before = len(mail.sent)
        _run(mail, now=now)
        _run(mail, now=now + 60)   # a second run the same day sends nothing
        if len(mail.sent) > before:
            sent_at.append(day)
    assert len(mail.sent) == 3 and _stage("u") == 3
    assert [round(d / 30.4375) for d in sent_at] == [18, 20, 23]
    assert "Final notice" not in mail.sent[0][1] and "Final notice" not in mail.sent[1][1]
    assert mail.sent[2][1].startswith("Final notice")
    assert "last email" in mail.sent[2][2]


def test_missed_stages_send_only_the_latest_notice():
    _user("u", 22)
    mail = FakeMail()
    _run(mail)
    assert len(mail.sent) == 1 and _stage("u") == 2


def test_notice_text_says_when_and_how_to_keep_the_account():
    _user("u", 18.5, name="Ada")
    mail = FakeMail()
    _run(mail)
    to, subject, body = mail.sent[0]
    deletion = retention._date(NOW - 18.5 * MONTH + 24 * MONTH)
    assert to == "u@example.com"
    assert deletion in subject and deletion in body
    assert retention._date(NOW - 18.5 * MONTH) in body
    assert body.startswith("Hello Ada,")
    assert "sign in" in body and "Download my data" in body
    assert "#" not in body   # no issue numbers in anything a user sees


def test_signing_in_resets_the_stage_and_notices_start_over():
    _user("u", 20.5, stage=2)
    with main.db() as con:
        main.upsert_user(con, auth.CurrentUser("u", "u@example.com", "U", tid="t"))
    assert _stage("u") == 0
    with main.db() as con:   # pin the sign-in time to NOW rather than the real clock
        con.execute("UPDATE users SET last_seen=? WHERE oid='u'", (NOW,))
    mail = FakeMail()
    _run(mail, now=NOW + 17.9 * MONTH)
    assert mail.sent == []
    _run(mail, now=NOW + 18.1 * MONTH)   # 18 months after that sign-in
    assert _stage("u") == 1 and len(mail.sent) == 1


def test_job_resets_a_stage_left_on_a_recently_active_account():
    _user("u", 1, stage=2)   # e.g. restored from a backup
    assert _run()["reset"] == 1
    assert _stage("u") == 0


# ------------------------------------------------------------------ deletion
def test_deleted_at_24_months_with_tombstone():
    _user("u", 24.01, stage=3)
    _user("keep", 23.9, stage=3)
    main._seen_users[(main.DB_PATH, "u")] = ("u@example.com", "Name u", "t", 0.0)
    epoch = main._block_epoch
    result = _run()
    assert result["deleted"] == 1
    assert not _exists("u") and _tombstoned("u")
    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM series WHERE owner_oid='u'").fetchone()[0] == 0
    assert _exists("keep") and not _tombstoned("keep")
    assert (main.DB_PATH, "u") not in main._seen_users
    assert main._block_epoch == epoch + 1


def test_not_deleted_if_they_sign_in_between_the_read_and_the_delete(monkeypatch):
    _user("u", 25)
    real_db = main.db

    def db_that_signs_in_first(write=False):
        if write:
            with real_db() as con:
                con.execute("UPDATE users SET last_seen=? WHERE oid='u'", (NOW,))
        return real_db(write)

    monkeypatch.setattr(main, "db", db_that_signs_in_first)
    assert _run()["deleted"] == 0
    assert _exists("u") and not _tombstoned("u")


@pytest.mark.parametrize("email", ["", "not-an-address", "x@localhost",
                                   "joe_gmail.com#EXT#@tenant.onmicrosoft.com"])
def test_no_usable_email_no_notice_but_still_deleted(email):
    _user("u", 19, email=email)
    mail = FakeMail()
    _run(mail)
    assert mail.sent == [] and _stage("u") == 0
    _run(mail, now=NOW + 6 * MONTH)
    assert mail.sent == [] and not _exists("u") and _tombstoned("u")


def test_plausible_email():
    assert retention.plausible_email("a.b@example.co.uk")
    for bad in ("", "a@b", "a b@example.com", "@example.com", "a@example.", "x#ext#@t.onmicrosoft.com"):
        assert not retention.plausible_email(bad), bad


def test_admins_are_never_noticed_or_deleted():
    _user(ADMIN, 19)
    _user("ret-ADMIN2", 30)
    auth.ADMIN_OIDS.add("ret-admin2")   # ADMIN_OIDS is stored lower-case
    mail = FakeMail()
    _run(mail)
    _run(mail, now=NOW + 10 * MONTH)
    assert mail.sent == []
    assert _exists(ADMIN) and _exists("ret-ADMIN2")


def test_blocked_accounts_get_no_notice_but_are_still_deleted():
    _user("u", 19)
    with main.db() as con:
        con.execute("INSERT INTO blocked_users (oid, blocked_at, blocked_by) VALUES ('u', 0, 'x')")
    mail = FakeMail()
    _run(mail)
    assert mail.sent == []
    _run(mail, now=NOW + 6 * MONTH)
    assert not _exists("u")
    with main.db() as con:   # deleting is no way round a block
        assert con.execute("SELECT 1 FROM blocked_users WHERE oid='u'").fetchone()


# ------------------------------------------------------- mail off / failures
def test_mail_disabled_sends_nothing_keeps_stage_and_still_deletes():
    _user("notice", 19)
    _user("old", 25)
    mail = FakeMail(on=False)
    result = _run(mail)
    assert mail.sent == [] and result["unsent"] == 1
    assert _stage("notice") == 0
    assert not _exists("old") and _tombstoned("old")


def test_failed_send_is_retried_the_next_day():
    _user("u", 19)
    mail = FakeMail(ok=False)
    _run(mail)
    assert len(mail.sent) == 1 and _stage("u") == 0
    mail.ok = True
    _run(mail, now=NOW + DAY)
    assert len(mail.sent) == 2 and _stage("u") == 1


def test_stage_not_advanced_if_they_sign_in_while_the_mail_is_sending():
    _user("u", 19)

    class SignsInWhileSending(FakeMail):
        def send(self, to, subject, body):
            with main.db() as con:
                con.execute("UPDATE users SET last_seen=?, inactive_notice_stage=0 WHERE oid='u'", (NOW,))
            return super().send(to, subject, body)

    _run(SignsInWhileSending())
    assert _stage("u") == 0


def test_one_failing_account_does_not_stop_the_rest(monkeypatch):
    """A locked database (or any error) on one account is logged and the run
    carries on, rather than leaving everyone after it until tomorrow."""
    _user("a-bad", 25)
    _user("b-old", 25)
    _user("c-notice", 19)
    real = main.delete_account_data

    def flaky(con, oid):
        if oid == "a-bad":
            raise sqlite3.OperationalError("database is locked")
        return real(con, oid)

    monkeypatch.setattr(main, "delete_account_data", flaky)
    mail = FakeMail()
    result = _run(mail)
    assert result["failed"] == 1 and result["deleted"] == 1 and result["noticed"] == 1
    assert _exists("a-bad") and not _tombstoned("a-bad")
    assert not _exists("b-old") and _stage("c-notice") == 1


# ------------------------------------------------------------ modes/settings
@pytest.mark.parametrize("mode", ["token", "none"])
def test_nothing_happens_outside_entra_mode(monkeypatch, mode):
    monkeypatch.setattr(auth, "AUTH_MODE", mode)
    _user("u", 30)
    mail = FakeMail()
    assert "skipped" in _run(mail)
    assert _exists("u") and mail.sent == []


def test_zero_delete_months_disables_the_job(monkeypatch):
    monkeypatch.setenv("INACTIVE_DELETE_MONTHS", "0")
    _user("u", 30)
    mail = FakeMail()
    assert "skipped" in _run(mail)
    assert _exists("u") and mail.sent == []


def test_custom_schedule(monkeypatch):
    monkeypatch.setenv("INACTIVE_DELETE_MONTHS", "12")
    monkeypatch.setenv("INACTIVE_NOTICE_MONTHS", " 11 , 6 ")
    _user("six", 6.5)
    _user("gone", 12.5)
    mail = FakeMail()
    _run(mail)
    assert _stage("six") == 1 and len(mail.sent) == 1 and not _exists("gone")
    assert retention.notice_months() == [6, 11]


def test_blank_notice_months_deletes_without_emails(monkeypatch):
    monkeypatch.setenv("INACTIVE_NOTICE_MONTHS", "")
    _user("u", 23.5)
    _user("old", 25)
    mail = FakeMail()
    _run(mail)
    assert mail.sent == [] and _exists("u") and not _exists("old")


@pytest.mark.parametrize("value", ["18,24", "0,18", "18,abc", "-1"])
def test_bad_notice_months_are_rejected(monkeypatch, value):
    monkeypatch.setenv("INACTIVE_NOTICE_MONTHS", value)
    with pytest.raises(ValueError):
        retention.notice_months()
    _user("old", 30)
    with pytest.raises(ValueError):   # nothing deleted on a bad config
        _run()
    assert _exists("old")


def test_startup_check_logs_bad_settings_and_disabled_mail(monkeypatch):
    records = []
    monkeypatch.setattr(main.logger, "error", lambda msg, *a: records.append(("error", msg % a)))
    monkeypatch.setattr(main.logger, "warning", lambda msg, *a: records.append(("warning", msg % a)))
    retention.startup_check()
    assert records and records[0][0] == "warning" and "mail disabled" in records[0][1]
    records.clear()
    monkeypatch.setenv("INACTIVE_NOTICE_MONTHS", "30")
    retention.startup_check()
    assert records and records[0][0] == "error" and "INACTIVE_NOTICE_MONTHS" in records[0][1]
    records.clear()
    monkeypatch.setattr(auth, "AUTH_MODE", "token")
    retention.startup_check()
    assert records == []


def test_scheduler_survives_a_bad_setting(monkeypatch):
    monkeypatch.setenv("RETENTION_HOUR", "not-an-hour")
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(retention.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(retention.scheduler())
    assert sleeps == [retention._SCHEDULER_RETRY_DELAY, retention._SCHEDULER_RETRY_DELAY]


def test_lifespan_starts_and_cancels_the_retention_job(monkeypatch):
    state = {}

    async def fake_scheduler():
        state["started"] = True
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def idle():
        await asyncio.Event().wait()

    monkeypatch.setattr(retention, "scheduler", fake_scheduler)
    monkeypatch.setattr(backup, "scheduler", idle)
    with TestClient(main.app) as client:
        client.get("/api/health")
        assert state.get("started")
    assert state.get("cancelled")


# -------------------------------------------------------------------- mailer
class FakeSMTP:
    instances = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content()))


@pytest.fixture
def smtp_env(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_USER", "sender@example.com")
    monkeypatch.setenv("SMTP_PASSWORD", "hunter2-app-password")
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *a, **k: FakeSMTP(*a, **k))
    return monkeypatch


def test_mailer_starttls_on_587(smtp_env):
    assert mailer.enabled()
    assert mailer.send("to@example.com", "Subj", "Body text")
    s = FakeSMTP.instances[-1]
    assert (s.host, s.port, s.timeout) == ("smtp.example.com", 587, mailer.TIMEOUT_SECONDS)
    assert s.calls[0] == "starttls"
    assert s.calls[1] == ("login", "sender@example.com", "hunter2-app-password")
    assert s.calls[2][:4] == ("send", "sender@example.com", "to@example.com", "Subj")
    assert s.calls[2][4].strip() == "Body text"


def test_mailer_ssl_on_465_and_custom_from(smtp_env):
    ssl_calls = []
    smtp_env.setenv("SMTP_PORT", "465")
    smtp_env.setenv("SMTP_FROM", "Story Bible <sender@example.com>")
    smtp_env.setattr(smtplib, "SMTP_SSL", lambda *a, **k: ssl_calls.append(a) or FakeSMTP(*a, **k))
    assert mailer.send("to@example.com", "S", "B")
    s = FakeSMTP.instances[-1]
    assert ssl_calls and s.port == 465 and "starttls" not in s.calls
    assert s.calls[1][1] == "Story Bible <sender@example.com>"


@pytest.mark.parametrize("missing", ["SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD"])
def test_mailer_disabled_without_full_settings(smtp_env, missing):
    smtp_env.delenv(missing)
    assert not mailer.enabled()
    assert mailer.send("to@example.com", "S", "B") is False
    assert FakeSMTP.instances == []


def test_mailer_bad_port_disables_rather_than_crashes(smtp_env):
    smtp_env.setenv("SMTP_PORT", "five-eight-seven")
    assert not mailer.enabled()
    assert mailer.send("to@example.com", "S", "B") is False


@pytest.mark.parametrize("error", [smtplib.SMTPAuthenticationError(535, b"bad hunter2-app-password"),
                                   TimeoutError("timed out"), ConnectionRefusedError()])
def test_mailer_failure_returns_false_and_never_logs_the_password(smtp_env, error, caplog):
    def boom(self, user, password):
        raise error

    smtp_env.setattr(FakeSMTP, "login", boom)
    main.logger.propagate = True
    try:
        with caplog.at_level(logging.WARNING, logger="storybible"):
            assert mailer.send("victim@example.com", "S", "B") is False
    finally:
        main.logger.propagate = False
    assert "mail failed" in caplog.text
    assert "hunter2" not in caplog.text and "victim@example.com" not in caplog.text


def test_mailer_refuses_header_injection(smtp_env):
    assert mailer.send("to@example.com\r\nBcc: everyone@example.com", "S", "B") is False
    assert FakeSMTP.instances == []
