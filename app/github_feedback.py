"""
Files a GitHub issue directly from the task pane (#22): "Log Issue" /
"Log Suggestion" post the title/description the user typed straight to
this repo's issue tracker, using a token the *server* holds - the pane's
user only needs whatever sign-in the app already requires for every other
/api/* call (see app/main.py's get_current_user). This token must
never reach the client: the pane only ever calls fetch("/api"+path), never
a third-party API directly, and nothing here is referenced by the static
files or the manifest.

Env vars:
  GITHUB_FEEDBACK_TOKEN   a fine-grained GitHub PAT, scoped to just this
                          repo, with Issues: write permission only. If
                          unset, filing returns 503 rather than crashing -
                          same defensive pattern as app/main.py's
                          _db_ok()/_data_dir_writable().

The repo is PUBLIC, so everything filed here is world-readable: the pane
says so next to the form, and the only thing added about the submitter is a
sanitized display name (never an email or object id) - see _submitter_line().

Rate limiting: a small in-process sliding window (_RateLimiter below), not
a distributed limiter - this endpoint sits behind sign-in in a single
--workers 1 container, so the real risk is an accidental double-submit or one
misbehaving account, not a stranger. Each person gets their own allowance
(so one can't use up another's), there is a global ceiling on top, and a
filing that fails before GitHub accepted it doesn't count against either.
See SECURITY.md for the wider threat model this follows.
"""
from __future__ import annotations

import itertools
import os
import threading
import re
import time
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from . import __version__, auth
from .models import LooseStr

REPO = "GazmanDevelopment/Story-Bible"
GITHUB_API_URL = f"https://api.github.com/repos/{REPO}/issues"

# Verified against the repo's actual label set (`gh label list`): plain
# `bug`/`enhancement` (GitHub defaults) plus area:pane, since anything
# filed from the task pane is by definition a pane-surfaced report.
LABELS: dict[str, list[str]] = {
    "issue": ["bug", "area:pane"],
    "suggestion": ["enhancement", "area:pane"],
}

# Env-overridable (#89). With open signup (#85) the global cap is a shared
# resource a handful of throwaway accounts could exhaust (each takes its
# per-person allowance) and lock everyone else out for the hour, so the default
# leaves room for ~12 people at the per-person cap; MAX_USERS in app/main.py
# bounds how many accounts there can be at all.
def _env_limit(name: str, default: int) -> int:
    """A positive int from the environment; blank/unset gives the default and
    0 or less is raised to 1 - unlike the main.py limits, "0" can't mean "off"
    here (this is the only guard on a public-repo write)."""
    raw = os.environ.get(name, "").strip()
    return max(1, int(raw)) if raw else default


RATE_LIMIT_MAX_CALLS = _env_limit("FEEDBACK_RATE_LIMIT_PER_PERSON", 5)     # per person, per window
RATE_LIMIT_GLOBAL_MAX_CALLS = _env_limit("FEEDBACK_RATE_LIMIT_GLOBAL", 60)  # across everyone: bounds the damage from many accounts, or one compromised one
RATE_LIMIT_WINDOW_SECONDS = 3600
OUTBOUND_TIMEOUT_SECONDS = 10  # must not block the single uvicorn worker for long


class UnreadableReply(Exception):
    """GitHub answered 2xx but the body wasn't the JSON we expected. Unlike
    every other failure, the issue may well have been created - so the
    rate-limit slot is NOT given back (retrying blindly could file duplicates).
    A dedicated type, not "some ValueError", so an unrelated error raised
    before anything was sent (a bad token that can't be encoded into a
    header, say) is never mistaken for it."""


class FeedbackIn(BaseModel):
    kind: Literal["issue", "suggestion"]
    title: LooseStr
    description: LooseStr = ""
    # Set by the server from the signed-in person, never read from the request
    # (validate_feedback strips it): otherwise a caller could file an issue
    # "submitted by" someone else.
    submitted_by: str = ""

    @field_validator("title")
    @classmethod
    def _title_bounds(cls, v: str) -> str:
        v = v.strip()
        if not (3 <= len(v) <= 120):
            raise ValueError("title must be 3-120 characters")
        return v

    @field_validator("description")
    @classmethod
    def _description_bounds(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("description is required")
        if len(v) > 4000:
            raise ValueError("description must be 4000 characters or fewer")
        return v


def validate_feedback(data: dict) -> FeedbackIn:
    try:
        return FeedbackIn(**{k: v for k, v in data.items() if k != "submitted_by"})
    except ValidationError as e:
        raise HTTPException(400, str(e))


class _RateLimiter:
    """A plain in-process sliding window, per key (a person) plus an overall
    ceiling - see the module docstring for why a distributed limiter would
    be overkill here.

    reserve() takes a slot up front (so two concurrent requests can't both
    squeeze under the cap), and refund() gives it back if the filing turns
    out not to have happened - a GitHub outage must not lock people out of
    reporting for an hour."""

    def __init__(self, max_calls: int, window_seconds: float, global_max_calls: int | None = None):
        self.max_calls = max_calls
        self.global_max_calls = global_max_calls
        self.window_seconds = window_seconds
        self._by_key: dict[str, list[tuple[float, int]]] = {}
        self._all: list[tuple[float, int]] = []
        self._seq = itertools.count()
        # Sync FastAPI routes run on a threadpool (main.py's write limiter), so
        # the prune/append/refund sequences must not interleave (#89).
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._all = [e for e in self._all if e[0] > cutoff]
        for key in list(self._by_key):
            kept = [e for e in self._by_key[key] if e[0] > cutoff]
            if kept:
                self._by_key[key] = kept
            else:
                del self._by_key[key]  # don't keep an entry per person who ever filed

    def reserve(self, key: str = "") -> tuple[float, int] | None:
        """A ticket to pass to refund(), or None if `key` (or everyone) is at the cap."""
        with self._lock:
            now = time.monotonic()
            self._prune(now)
            mine = self._by_key.get(key, [])
            if len(mine) >= self.max_calls:
                return None
            if self.global_max_calls is not None and len(self._all) >= self.global_max_calls:
                return None
            ticket = (now, next(self._seq))
            self._by_key.setdefault(key, []).append(ticket)
            self._all.append(ticket)
            return ticket

    def person_full(self, key: str = "") -> bool:
        """True if `key`'s own allowance is used up (as opposed to only the overall ceiling)."""
        with self._lock:
            self._prune(time.monotonic())
            return len(self._by_key.get(key, [])) >= self.max_calls

    def refund(self, key: str, ticket: tuple[float, int]) -> None:
        with self._lock:
            if ticket in self._all:
                self._all.remove(ticket)
            mine = self._by_key.get(key)
            if mine and ticket in mine:
                mine.remove(ticket)
                if not mine:
                    del self._by_key[key]


_rate_limiter = _RateLimiter(RATE_LIMIT_MAX_CALLS, RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_GLOBAL_MAX_CALLS)


_GUID = re.compile(r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


def _public_name(user: auth.CurrentUser | None) -> str:
    """The name that may go into a public issue, or "" if there isn't a real
    one. auth.resolve_entra_user falls back to preferred_username (a UPN, i.e.
    an email address) and then to the object id when a token has no `name`
    claim - publishing either would defeat the point of naming only a display
    name, so anything that is (or looks like) an email or an id is dropped."""
    if user is None or auth.AUTH_MODE != "entra" or user.is_pipeline:
        return ""
    name = (user.display_name or "").strip()
    if not name or name in (user.email, user.oid) or "@" in name or _GUID.match(name):
        return ""
    return name


def _submitter_line(name: str) -> str:
    """The issue is public, so only a display name goes in - and it comes from
    the sign-in token, i.e. it is user-controlled text headed for markdown:
    keep letters, digits, spaces and a little punctuation, drop everything
    else (no newlines, backticks, @mentions, links, HTML), and cap the length."""
    # Whitespace first (a newline becomes a space, not a deletion that glues
    # two words together), then the character filter, then tidy again.
    cleaned = re.sub(r"\s+", " ", name or "")
    cleaned = re.sub(r"[^\w .'\-]", "", cleaned)
    cleaned = cleaned.replace("_", " ")                       # _emphasis_ / __bold__
    cleaned = re.sub(r"([-.'])\1{2,}", r"\1", cleaned)         # runs of one punctuation mark
    cleaned = re.sub(r" +", " ", cleaned).strip()[:80]
    return f"\nSubmitted by {cleaned}" if cleaned else ""


def _issue_body(feedback: FeedbackIn) -> str:
    submitted_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    # Display name only, never email/oid: the repo is public (#73). No client IP either.
    return (f"{feedback.description}\n\n---\nFiled from the Story Bible task pane · v{__version__} · {submitted_at}"
            f"{_submitter_line(feedback.submitted_by)}")


async def _post_issue_to_github(
    token: str, feedback: FeedbackIn, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    """The seam higher-level tests mock (file_feedback's failure/rate-limit/
    label paths don't need a real request built) - but `transport` lets a
    focused test inject an httpx.MockTransport and exercise this function's
    own request construction (URL, headers, JSON body) directly, so a typo
    there isn't invisible to every test the way it would be if this whole
    function were always mocked away. None (the default) means a real
    network call, as normal in production."""
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_SECONDS, transport=transport) as client:
        r = await client.post(
            GITHUB_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json={
                "title": feedback.title,
                "body": _issue_body(feedback),
                "labels": LABELS[feedback.kind],
            },
        )
        r.raise_for_status()
        try:
            return r.json()
        except ValueError as e:
            raise UnreadableReply(str(e)) from e


async def file_feedback(feedback: FeedbackIn, user: auth.CurrentUser | None = None) -> dict[str, Any]:
    """Raises HTTPException on every failure path (503 not configured, 429
    rate limited, 502 GitHub unreachable/errored); returns
    {"number", "html_url"} on success. Never lets the token or GitHub's raw
    error body reach the client - only the log.

    `user` keys the rate limit (one person can't use up another's allowance)
    and, in Entra mode only, supplies the display name for the "Submitted by"
    line. In none/token mode there is no real identity to name."""
    from . import main as app_main  # lazy: avoids a main<->github_feedback import cycle

    token = os.environ.get("GITHUB_FEEDBACK_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Feedback filing isn't configured on this server")
    key = user.oid if user else ""
    # Always assigned (to "" when there's no real name), never inherited: the
    # object may have come from anywhere, and only this function decides who
    # a filing is attributed to.
    feedback.submitted_by = _public_name(user)
    ticket = _rate_limiter.reserve(key)
    if ticket is None:
        if _rate_limiter.person_full(key):
            raise HTTPException(429, "Too many feedback submissions - try again later")
        raise HTTPException(429, "Feedback is very busy right now - try again later")
    try:
        result = await _post_issue_to_github(token, feedback)
    except BaseException as e:  # BaseException: a cancelled request must give its slot back too
        # The call to GitHub itself failed (network, timeout, or a non-2xx
        # answer) or was cancelled: nothing was filed, so this attempt doesn't
        # count. (A timeout can in theory have filed one - the per-hour cap
        # makes that duplicate cheap.) The exception is UnreadableReply: GitHub
        # said yes, so the issue may well exist, and the slot stays used.
        if not isinstance(e, UnreadableReply):
            _rate_limiter.refund(key, ticket)
        if not isinstance(e, Exception):
            raise  # cancellation / shutdown: propagate, don't turn it into a 502
        app_main.logger.error("feedback: GitHub API call failed: %s", e)
        raise HTTPException(502, "Could not reach GitHub. Try again later.")
    try:
        # An unexpected response shape (a missing key) is exactly as much
        # "GitHub call failed" as a network error - this function's contract
        # is to raise HTTPException on every failure path, not sometimes let
        # a KeyError through to a generic 500 instead.
        return {"number": result["number"], "html_url": result["html_url"]}
    except Exception as e:
        app_main.logger.error("feedback: unexpected GitHub response: %s", e)
        raise HTTPException(502, "Could not reach GitHub. Try again later.")
