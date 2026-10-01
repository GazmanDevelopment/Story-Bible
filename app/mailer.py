"""
Outgoing email (#113), for the inactive-account notices in app/retention.py.

Plain stdlib smtplib, configured from the environment:
  SMTP_HOST      the mail server, e.g. smtp.gmail.com
  SMTP_PORT      587 (STARTTLS, the default) or 465 (implicit TLS)
  SMTP_USER      the account to authenticate as
  SMTP_PASSWORD  its password - for Gmail an app password (deploy/README.md)
  SMTP_FROM      the From header (default: SMTP_USER)

Mail is disabled unless SMTP_HOST, SMTP_USER and SMTP_PASSWORD are all set:
send() then returns False without trying, and warn_if_disabled() logs it once
at startup. Settings are read on each call, like app/backup.py, so tests can
monkeypatch the environment.

SMTP_PASSWORD must never be logged or returned by the API. Failures are logged
by exception type only (smtplib's own messages can echo server responses), and
without the recipient's address.
"""
from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage

TIMEOUT_SECONDS = 15


def _port() -> int:
    """SMTP_PORT, or 0 if it isn't a number (which leaves mail disabled
    rather than crashing startup over a typo)."""
    try:
        return int(os.environ.get("SMTP_PORT", "").strip() or 587)
    except ValueError:
        return 0


def _settings() -> dict[str, str | int]:
    user = os.environ.get("SMTP_USER", "").strip()
    return {
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": _port(),
        "user": user,
        "password": os.environ.get("SMTP_PASSWORD", "").strip(),
        "from": os.environ.get("SMTP_FROM", "").strip() or user,
    }


def enabled() -> bool:
    s = _settings()
    return bool(s["host"] and s["user"] and s["password"] and s["port"] > 0)


def warn_if_disabled() -> None:
    from . import main as app_main  # lazy: app.main imports app.retention, which imports this
    if not enabled():
        app_main.logger.warning(
            "mail disabled: SMTP_HOST, SMTP_USER and SMTP_PASSWORD are not all set (or SMTP_PORT "
            "is not a number), so "
            "inactive-account notices will not be sent (accounts are still deleted on schedule).")


def send(to: str, subject: str, body: str) -> bool:
    """Send one plain-text email. True only if the server accepted it."""
    from . import main as app_main
    if not enabled():
        return False
    s = _settings()
    try:
        msg = EmailMessage()
        msg["From"] = s["from"]
        msg["To"] = to   # raises ValueError on a CR/LF, so no header injection
        msg["Subject"] = subject
        msg.set_content(body)
        if s["port"] == 465:
            smtp = smtplib.SMTP_SSL(s["host"], s["port"], timeout=TIMEOUT_SECONDS,
                                    context=ssl.create_default_context())
        else:
            smtp = smtplib.SMTP(s["host"], s["port"], timeout=TIMEOUT_SECONDS)
        with smtp:
            if s["port"] != 465:
                smtp.starttls(context=ssl.create_default_context())
            smtp.login(s["user"], s["password"])
            smtp.send_message(msg)
    except (OSError, ValueError, smtplib.SMTPException) as e:
        # OSError covers timeouts, refused connections and TLS errors. The
        # address is left out: the caller logs whose notice it was, by oid.
        app_main.logger.warning("mail failed: %s", type(e).__name__)
        return False
    return True
