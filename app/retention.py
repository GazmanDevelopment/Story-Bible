"""
Inactive-account retention (#113, decided in #100): an account nobody has
signed in to for INACTIVE_DELETE_MONTHS (default 24) is deleted, and its owner
is emailed beforehand at each of INACTIVE_NOTICE_MONTHS (default 18, 20, 23).

Runs once a day at RETENTION_HOUR from app/main.py's lifespan, beside the
backup scheduler, and only in AUTH_MODE=entra - in none/token mode there is one
shared bible and nothing here may delete it. Run once by hand with
`python -m app.retention`.

users.inactive_notice_stage records how many notices have gone out (0 = none),
so each is sent once. Signing in resets it (upsert_user in app/main.py), and so
does this job if it finds a row back inside the first notice age.

- Administrators (ADMIN_OIDS) are never noticed or deleted.
- Deletion uses delete_account_data, so it writes the same tombstone as a
  person deleting their own account (docs/BACKUP.md, docs/RESTORE.md). It
  happens whether or not any notice could be sent: no usable email, mail
  disabled, a blocked account.
- A notice's stage is only recorded once the mail server accepts it, so a
  failed send is tried again the next day. If several stages were missed
  (the server was down, say) only the latest is sent.

Env vars (read on each call, like app/backup.py):
  INACTIVE_DELETE_MONTHS   months without a sign-in before deletion (default 24, 0 = off)
  INACTIVE_NOTICE_MONTHS   comma-separated months to email at (default 18,20,23);
                           each must be below INACTIVE_DELETE_MONTHS. Blank = no emails.
  RETENTION_HOUR           local hour (0-23) the daily run starts (default 4, after the backup)
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime
from types import ModuleType
from typing import Any

from . import mailer as mailer_mod

MONTH_SECONDS = 30.4375 * 86400   # an average month; the schedule only needs to be roughly right

# Good enough to skip blanks and obvious non-addresses, not a full RFC 5322
# check. A guest's "#EXT#" user principal name looks like an address but has
# no mailbox behind it.
_PLAUSIBLE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")


def delete_months() -> int:
    return int(os.environ.get("INACTIVE_DELETE_MONTHS", 24))


def notice_months() -> list[int]:
    """INACTIVE_NOTICE_MONTHS as a sorted list. Raises ValueError if any is
    not a whole number between 1 and INACTIVE_DELETE_MONTHS - 1."""
    raw = os.environ.get("INACTIVE_NOTICE_MONTHS", "18,20,23")
    months = sorted({int(m) for m in raw.split(",") if m.strip()})
    limit = delete_months()
    bad = [m for m in months if not 0 < m < limit]
    if limit > 0 and bad:
        raise ValueError(f"INACTIVE_NOTICE_MONTHS {bad} must be between 1 and {limit - 1} "
                         "(below INACTIVE_DELETE_MONTHS)")
    return months


def retention_hour() -> int:
    return int(os.environ.get("RETENTION_HOUR", 4))


def plausible_email(address: str) -> bool:
    return bool(address) and "#EXT#" not in address.upper() and bool(_PLAUSIBLE_EMAIL.match(address))


def _date(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return f"{d.day} {d:%B %Y}"


def notice_text(name: str, email: str, last_seen: float, delete_at: float, now: float,
                final: bool) -> tuple[str, str]:
    """(subject, body) of one notice. Plain text, no links: the add-in is
    opened from Word, not from a web address."""
    days = max(1, round((delete_at - now) / 86400))
    when = _date(delete_at)
    subject = (f"Final notice: your Story Bible account will be deleted on {when}" if final
               else f"Your Story Bible account will be deleted on {when}")
    greeting = f"Hello {name}," if name else "Hello,"
    lines = [
        greeting,
        "",
        f"Nobody has signed in to the Story Bible account for {email} since {_date(last_seen)}.",
        f"Accounts that are not used for {delete_months()} months are deleted, so this account "
        f"and every series it owns will be deleted on {when}, in about {days} days.",
        "",
        "To keep your account, open the Story Bible add-in in Word and sign in before then. "
        "That is all you need to do.",
        "",
        "To keep a copy of your work instead, sign in, open your account page and click "
        "\"Download my data\".",
        "",
    ]
    if final:
        lines += ["This is the last email we will send about this.", ""]
    lines += ["If you no longer want the account, you can ignore this email.", "", "Story Bible"]
    return subject, "\n".join(lines)


def _process_one(u: dict[str, Any], now: float, notices: list[int], delete_age: float,
                 first_age: float, mail: Any, mail_on: bool, blocked: set[str]) -> str | None:
    """Delete, reset or notice one (non-admin) account. Returns the counts
    key for what happened, or None for nothing to do."""
    from . import main as app_main
    oid = u["oid"]
    age = now - u["last_seen"]
    if age >= delete_age:
        with app_main.db(write=True) as con:
            # Re-read under the write lock: they may have signed in since.
            row = con.execute("SELECT last_seen FROM users WHERE oid=?", (oid,)).fetchone()
            if not row or now - row["last_seen"] < delete_age:
                return None
            app_main.delete_account_data(con, oid)
        app_main._block_epoch += 1
        app_main._seen_users.pop((app_main.DB_PATH, oid), None)
        app_main._policy_accepted.pop((app_main.DB_PATH, oid), None)
        app_main.logger.info("retention: deleted inactive account %s", app_main._sanitize_for_log(oid))
        return "deleted"
    if age < first_age:
        if not u["inactive_notice_stage"]:
            return None
        with app_main.db() as con:
            con.execute("UPDATE users SET inactive_notice_stage=0 WHERE oid=?", (oid,))
        return "reset"
    due = sum(1 for m in notices if age >= m * MONTH_SECONDS)
    if due <= u["inactive_notice_stage"] or oid in blocked or not plausible_email(u["email"]):
        return None
    if not mail_on:
        return "unsent"
    subject, body = notice_text(u["display_name"], u["email"], u["last_seen"],
                                u["last_seen"] + delete_age, now, final=due == len(notices))
    if not mail.send(u["email"], subject, body):
        app_main.logger.warning("retention: notice %d for %s not sent, will retry tomorrow",
                                due, app_main._sanitize_for_log(oid))
        return "unsent"
    with app_main.db() as con:
        # Only if they haven't signed in meanwhile (which reset the stage).
        con.execute("UPDATE users SET inactive_notice_stage=? WHERE oid=? AND last_seen=?",
                    (due, oid, u["last_seen"]))
    return "noticed"


def run_retention(now: float | None = None, mail: ModuleType | Any = mailer_mod) -> dict[str, Any]:
    """One pass over every account. `now` and `mail` (anything with enabled()
    and send(to, subject, body)) are injectable for tests. Returns counts."""
    from . import auth
    from . import main as app_main

    if auth.AUTH_MODE != "entra":
        return {"skipped": "accounts are only deleted in AUTH_MODE=entra"}
    limit = delete_months()
    if limit <= 0:
        return {"skipped": "INACTIVE_DELETE_MONTHS=0"}
    notices = notice_months()
    now = time.time() if now is None else now
    delete_age = limit * MONTH_SECONDS
    first_age = notices[0] * MONTH_SECONDS if notices else delete_age
    mail_on = mail.enabled()
    counts = {"noticed": 0, "deleted": 0, "reset": 0, "unsent": 0, "failed": 0}

    with app_main.db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT oid, email, display_name, last_seen, inactive_notice_stage FROM users")]
        blocked = {r[0] for r in con.execute("SELECT oid FROM blocked_users")}

    for u in rows:
        if u["oid"].lower() in auth.ADMIN_OIDS:
            continue
        try:
            outcome = _process_one(u, now, notices, delete_age, first_age, mail, mail_on, blocked)
        except Exception:
            # One bad account (a locked database, say) mustn't hold up everyone
            # else's notices and deletions until tomorrow.
            app_main.logger.exception("retention: account %s failed, will retry tomorrow",
                                      app_main._sanitize_for_log(u["oid"]))
            outcome = "failed"
        if outcome:
            counts[outcome] += 1

    app_main.logger.info("retention: %d notices sent, %d not sent, %d accounts deleted, %d reset, %d failed",
                         counts["noticed"], counts["unsent"], counts["deleted"], counts["reset"],
                         counts["failed"])
    return counts


def startup_check() -> None:
    """Logged once at startup: a bad setting, or mail being off while the
    job is on. Never raises - the scheduler keeps retrying and logging."""
    from . import auth
    from . import main as app_main
    if auth.AUTH_MODE != "entra":
        return
    try:
        if delete_months() <= 0:
            return
        notices = notice_months()
        retention_hour()
    except ValueError as e:
        app_main.logger.error("inactive-account retention is misconfigured: %s", e)
        return
    if notices:
        mailer_mod.warn_if_disabled()


_SCHEDULER_RETRY_DELAY = 3600


async def scheduler() -> None:
    """Runs forever (until cancelled), like backup.scheduler: sleep until the
    next RETENTION_HOUR and run in a worker thread. A failure that stops the
    whole run (a bad setting, say) is logged and the loop carries on, so the
    next attempt is the following day's RETENTION_HOUR; the hour's pause just
    stops a broken schedule from spinning. A failure on one account is
    handled inside run_retention and doesn't stop the others."""
    from . import backup
    from . import main as app_main
    while True:
        try:
            await asyncio.sleep(backup.seconds_until_next_run(retention_hour()))
            await asyncio.to_thread(run_retention)
        except Exception:
            app_main.logger.exception("scheduled inactive-account retention failed")
            await asyncio.sleep(_SCHEDULER_RETRY_DELAY)


if __name__ == "__main__":
    print(run_retention())
