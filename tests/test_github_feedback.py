import asyncio
import json
import os
import tempfile

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")  # mkstemp, not mktemp - see #28
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import httpx
import pytest
import app.github_feedback as feedback
import app.main as main
from fastapi.testclient import TestClient

c = TestClient(main.app)

VALID_BODY = {"kind": "issue", "title": "A real, specific bug title", "description": "Steps to reproduce here."}


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    """The rate limiter is a module-level singleton (deliberately, for a
    single-process app) - reset it before every test in this file so one
    test's calls can't push another test over the limit."""
    monkeypatch.setattr(
        feedback, "_rate_limiter",
        feedback._RateLimiter(feedback.RATE_LIMIT_MAX_CALLS, feedback.RATE_LIMIT_WINDOW_SECONDS),
    )

def _mock_success(monkeypatch, capture: dict | None = None):
    async def fake_post(token, fb):
        if capture is not None:
            capture["token"] = token
            capture["feedback"] = fb
        return {"number": 42, "html_url": "https://github.com/GazmanDevelopment/Story-Bible/issues/42"}
    monkeypatch.setattr(feedback, "_post_issue_to_github", fake_post)


# --------------------------------------------------------------- not configured
def test_missing_token_returns_503_and_never_calls_github(monkeypatch):
    monkeypatch.delenv("GITHUB_FEEDBACK_TOKEN", raising=False)
    def must_not_be_called(*a, **k):
        raise AssertionError("GitHub should not have been called")
    monkeypatch.setattr(feedback, "_post_issue_to_github", must_not_be_called)
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.status_code == 503


# ------------------------------------------------------------------- validation
def test_invalid_kind_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "kind": "feature-request"})
    assert r.status_code == 400

def test_blank_title_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "title": ""})
    assert r.status_code == 400

def test_whitespace_only_title_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "title": "     "})
    assert r.status_code == 400

def test_title_too_short_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "title": "ab"})
    assert r.status_code == 400

def test_title_too_long_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "title": "x" * 121})
    assert r.status_code == 400

def test_blank_description_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "description": "   "})
    assert r.status_code == 400

def test_description_too_long_rejected_with_400(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    r = c.post("/api/feedback", json={**VALID_BODY, "description": "x" * 4001})
    assert r.status_code == 400


# ------------------------------------------------------------------------ labels
def test_kind_issue_maps_to_bug_and_area_pane_labels(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    capture: dict = {}
    _mock_success(monkeypatch, capture)
    r = c.post("/api/feedback", json={**VALID_BODY, "kind": "issue"})
    assert r.status_code == 201
    assert feedback.LABELS[capture["feedback"].kind] == ["bug", "area:pane"]

def test_kind_suggestion_maps_to_enhancement_and_area_pane_labels(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    capture: dict = {}
    _mock_success(monkeypatch, capture)
    r = c.post("/api/feedback", json={**VALID_BODY, "kind": "suggestion"})
    assert r.status_code == 201
    assert feedback.LABELS[capture["feedback"].kind] == ["enhancement", "area:pane"]


# --------------------------------------------------------------------- success
def test_success_returns_number_and_html_url(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    _mock_success(monkeypatch)
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.status_code == 201
    assert r.json() == {"number": 42, "html_url": "https://github.com/GazmanDevelopment/Story-Bible/issues/42"}

def test_success_response_is_not_cached(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    _mock_success(monkeypatch)
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------- github errors
def test_github_failure_returns_502_with_generic_message(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    async def fake_post(token, fb):
        raise RuntimeError("connection reset by peer: some raw internal detail")
    monkeypatch.setattr(feedback, "_post_issue_to_github", fake_post)
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.status_code == 502
    assert "connection reset" not in r.text
    assert "some raw internal detail" not in r.text

def test_unexpected_response_shape_also_returns_502_not_500(monkeypatch):
    """A 2xx GitHub response missing the keys we expect is exactly as much
    a failure as a network error - must not fall through as an unhandled
    KeyError (a generic 500) instead of the documented 502."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    async def fake_post(token, fb):
        return {"unexpected": "shape"}  # missing "number"/"html_url"
    monkeypatch.setattr(feedback, "_post_issue_to_github", fake_post)
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.status_code == 502

def test_token_and_github_error_detail_are_logged_not_returned(monkeypatch):
    """The token must never reach the client in the response, and the
    logged line (which does record the real error, for the operator) must
    never include the token value itself."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "super-secret-pat-value")
    import io, logging
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    main.logger.addHandler(handler)
    try:
        async def fake_post(token, fb):
            raise RuntimeError("boom")
        monkeypatch.setattr(feedback, "_post_issue_to_github", fake_post)
        r = c.post("/api/feedback", json=VALID_BODY)
    finally:
        main.logger.removeHandler(handler)
    assert r.status_code == 502
    assert "super-secret-pat-value" not in r.text
    assert "super-secret-pat-value" not in buf.getvalue()


# ------------------------------------------------------------------- rate limit
def test_rate_limit_returns_429_past_the_cap(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=2, window_seconds=3600))
    _mock_success(monkeypatch)
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 201
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 201
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.status_code == 429

def test_rate_limit_checked_before_calling_github(monkeypatch):
    """A rejected-by-rate-limit call must not still hit the network."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=0, window_seconds=3600))
    def must_not_be_called(*a, **k):
        raise AssertionError("GitHub should not have been called")
    monkeypatch.setattr(feedback, "_post_issue_to_github", must_not_be_called)
    r = c.post("/api/feedback", json=VALID_BODY)
    assert r.status_code == 429


# ------------------------------------------------------ actual request shape
def test_post_issue_to_github_sends_the_right_request():
    """Every test above mocks _post_issue_to_github away entirely, so a
    typo in its URL/headers/JSON body would pass all of them undetected.
    This one actually runs it, via httpx.MockTransport (still no real
    network call) rather than a real GitHub API request."""
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["json"] = json.loads(request.content)
        return httpx.Response(201, json={"number": 99, "html_url": "https://github.com/x/y/issues/99"})

    fb = feedback.FeedbackIn(kind="issue", title="A real bug here", description="Steps to repro")
    result = asyncio.run(
        feedback._post_issue_to_github("tok123", fb, transport=httpx.MockTransport(handler))
    )

    assert result == {"number": 99, "html_url": "https://github.com/x/y/issues/99"}
    # Hardcoded, not feedback.GITHUB_API_URL: comparing against the same
    # constant the code under test uses would make a typo in that constant
    # invisible (the request and the assertion would be wrong together).
    assert captured["url"] == "https://api.github.com/repos/GazmanDevelopment/Story-Bible/issues"
    assert captured["headers"]["authorization"] == "Bearer tok123"
    assert captured["headers"]["accept"] == "application/vnd.github+json"
    assert captured["headers"]["x-github-api-version"] == "2022-11-28"
    assert captured["json"]["title"] == "A real bug here"
    assert captured["json"]["labels"] == ["bug", "area:pane"]
    assert "Steps to repro" in captured["json"]["body"]
