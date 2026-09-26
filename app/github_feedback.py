"""
Files a GitHub issue directly from the task pane (#22): "Log Issue" /
"Log Suggestion" post the title/description the user typed straight to
this repo's issue tracker, using a token the *server* holds - the pane's
user only needs whatever STORYBIBLE_TOKEN the app already requires for
every other /api/* call (see app/main.py's check_token). This token must
never reach the client: the pane only ever calls fetch("/api"+path), never
a third-party API directly, and nothing here is referenced by the static
files or the manifest.

Env vars:
  GITHUB_FEEDBACK_TOKEN   a fine-grained GitHub PAT, scoped to just this
                          repo, with Issues: write permission only. If
                          unset, filing returns 503 rather than crashing -
                          same defensive pattern as app/main.py's
                          _db_ok()/_data_dir_writable().

Rate limiting: a small in-process sliding window (_RateLimiter below), not
a distributed limiter - this endpoint already sits behind STORYBIBLE_TOKEN
in a single --workers 1 container with two known users, so the real risk
here is an accidental client-side double-submit, not abuse from a
stranger. See SECURITY.md for the wider threat model this follows.
"""
from __future__ import annotations

import os
import time
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError, field_validator

from . import __version__
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

RATE_LIMIT_MAX_CALLS = 5
RATE_LIMIT_WINDOW_SECONDS = 3600
OUTBOUND_TIMEOUT_SECONDS = 10  # must not block the single uvicorn worker for long


class FeedbackIn(BaseModel):
    kind: Literal["issue", "suggestion"]
    title: LooseStr
    description: LooseStr = ""

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
        return FeedbackIn(**data)
    except ValidationError as e:
        raise HTTPException(400, str(e))


class _RateLimiter:
    """A plain in-process sliding window - see the module docstring for
    why a distributed limiter would be overkill here."""

    def __init__(self, max_calls: int, window_seconds: float):
        self.max_calls = max_calls
        self.window_seconds = window_seconds
        self._timestamps: list[float] = []

    def allow(self) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self.max_calls:
            return False
        self._timestamps.append(now)
        return True


_rate_limiter = _RateLimiter(RATE_LIMIT_MAX_CALLS, RATE_LIMIT_WINDOW_SECONDS)


def _issue_body(feedback: FeedbackIn) -> str:
    submitted_at = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    # No client IP or identity yet - once Entra sign-in (M2) lands, add
    # "submitted by <email>" here the same way created_by/updated_by get
    # added to records (#12).
    return f"{feedback.description}\n\n---\nFiled from the Story Bible task pane · v{__version__} · {submitted_at}"


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
        return r.json()


async def file_feedback(feedback: FeedbackIn) -> dict[str, Any]:
    """Raises HTTPException on every failure path (503 not configured, 429
    rate limited, 502 GitHub unreachable/errored); returns
    {"number", "html_url"} on success. Never lets the token or GitHub's raw
    error body reach the client - only the log."""
    from . import main as app_main  # lazy: avoids a main<->github_feedback import cycle

    token = os.environ.get("GITHUB_FEEDBACK_TOKEN", "").strip()
    if not token:
        raise HTTPException(503, "Feedback filing isn't configured on this server")
    if not _rate_limiter.allow():
        raise HTTPException(429, "Too many feedback submissions - try again later")
    try:
        result = await _post_issue_to_github(token, feedback)
        # Inside the try, not after it: an unexpected response shape (a
        # missing key) is exactly as much "GitHub call failed" as a network
        # error or non-2xx status - this function's contract is to raise
        # HTTPException on every failure path, not sometimes let a
        # KeyError through to a generic 500 instead.
        return {"number": result["number"], "html_url": result["html_url"]}
    except Exception as e:
        app_main.logger.error("feedback: GitHub API call failed: %s", e)
        raise HTTPException(502, "Could not reach GitHub. Try again later.")
