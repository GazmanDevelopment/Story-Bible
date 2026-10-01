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


# ==================================================================== #73
import app.auth as auth  # noqa: E402


def _person(oid, name="Ada Lovelace", email="ada@example.com"):
    return auth.CurrentUser(oid=oid, email=email, display_name=name)


def _file(fb_dict, user, monkeypatch, post):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_post_issue_to_github", post)
    fb = feedback.validate_feedback(fb_dict)
    return asyncio.run(feedback.file_feedback(fb, user))


def _ok_post(captured=None):
    async def post(token, fb):
        if captured is not None:
            captured.append(fb)
        return {"number": 1, "html_url": "https://github.com/x/y/issues/1"}
    return post


def _status_error(code):
    request = httpx.Request("POST", "https://api.github.com/x")
    return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))


# ------------------------------------------- failures don't use up the allowance (F12)
@pytest.mark.parametrize("error", [
    httpx.ConnectError("no route"), httpx.ReadTimeout("slow"), _status_error(502), _status_error(401),
    _status_error(422), RuntimeError("anything else"),
])
def test_a_failed_filing_does_not_count_against_the_limit(monkeypatch, error):
    """The bug: 5 failed attempts during a GitHub outage locked people out of
    reporting for an hour."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=2, window_seconds=3600))

    async def failing(token, fb):
        raise error
    monkeypatch.setattr(feedback, "_post_issue_to_github", failing)
    for _ in range(6):
        assert c.post("/api/feedback", json=VALID_BODY).status_code == 502
    _mock_success(monkeypatch)
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 201   # not 429


def test_a_reply_we_could_not_read_keeps_its_slot(monkeypatch):
    """GitHub answered 2xx but the body wasn't JSON: the issue may exist, so
    retrying blindly could file duplicates - this one stays counted."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=1, window_seconds=3600))

    async def unreadable(token, fb):
        raise feedback.UnreadableReply("Expecting value")
    monkeypatch.setattr(feedback, "_post_issue_to_github", unreadable)
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 502
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 429


def test_a_2xx_with_the_wrong_shape_keeps_its_slot(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=1, window_seconds=3600))

    async def odd(token, fb):
        return {"unexpected": "shape"}
    monkeypatch.setattr(feedback, "_post_issue_to_github", odd)
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 502
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 429


def test_successes_still_count(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=2, window_seconds=3600))
    _mock_success(monkeypatch)
    assert [c.post("/api/feedback", json=VALID_BODY).status_code for _ in range(3)] == [201, 201, 429]


# ------------------------------------------------------- per person + overall ceiling
def test_each_person_has_their_own_allowance(monkeypatch):
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=2, window_seconds=3600))
    ada, bob = _person("oid-ada"), _person("oid-bob", name="Bob")
    for _ in range(2):
        _file(VALID_BODY, ada, monkeypatch, _ok_post())
    with pytest.raises(feedback.HTTPException) as exc:
        _file(VALID_BODY, ada, monkeypatch, _ok_post())
    assert exc.value.status_code == 429
    _file(VALID_BODY, bob, monkeypatch, _ok_post())          # Bob is unaffected by Ada using hers up


def test_a_global_ceiling_bounds_everyone_together(monkeypatch):
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=5, window_seconds=3600, global_max_calls=3))
    for i in range(3):
        _file(VALID_BODY, _person(f"oid-{i}"), monkeypatch, _ok_post())
    with pytest.raises(feedback.HTTPException) as exc:
        _file(VALID_BODY, _person("oid-new"), monkeypatch, _ok_post())
    assert exc.value.status_code == 429


def test_a_refund_frees_the_global_ceiling_too(monkeypatch):
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=5, window_seconds=3600, global_max_calls=1))

    async def failing(token, fb):
        raise httpx.ConnectError("down")
    with pytest.raises(feedback.HTTPException):
        _file(VALID_BODY, _person("oid-a"), monkeypatch, failing)
    _file(VALID_BODY, _person("oid-b"), monkeypatch, _ok_post())     # would 429 if the failed one still held the slot


def test_the_shipped_limiter_has_a_per_person_and_an_overall_cap():
    assert feedback.RATE_LIMIT_MAX_CALLS == 5 and feedback.RATE_LIMIT_GLOBAL_MAX_CALLS == 60
    fresh = feedback._RateLimiter(feedback.RATE_LIMIT_MAX_CALLS, feedback.RATE_LIMIT_WINDOW_SECONDS,
                                  feedback.RATE_LIMIT_GLOBAL_MAX_CALLS)
    assert fresh.max_calls == 5 and fresh.global_max_calls == 60


def test_concurrent_requests_cannot_overshoot_the_cap(monkeypatch):
    """reserve() takes the slot before the awaited network call, so a burst
    of simultaneous submissions can't all pass a check-then-act gap."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=3, window_seconds=3600))

    async def slow_ok(token, fb):
        await asyncio.sleep(0.01)
        return {"number": 1, "html_url": "https://github.com/x/y/issues/1"}
    monkeypatch.setattr(feedback, "_post_issue_to_github", slow_ok)

    async def burst():
        fb = feedback.validate_feedback(VALID_BODY)
        results = await asyncio.gather(
            *[feedback.file_feedback(fb, _person("oid-x")) for _ in range(10)], return_exceptions=True)
        return [r for r in results if isinstance(r, dict)], [r for r in results if isinstance(r, feedback.HTTPException)]
    ok, denied = asyncio.run(burst())
    assert len(ok) == 3 and len(denied) == 7 and all(d.status_code == 429 for d in denied)


def test_the_limiter_expires_old_entries_and_does_not_keep_idle_people(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(feedback.time, "monotonic", lambda: now[0])
    rl = feedback._RateLimiter(max_calls=1, window_seconds=60)
    assert rl.reserve("a") and rl.reserve("a") is None
    now[0] += 61
    assert rl.reserve("b")           # pruning happens on every reserve
    assert "a" not in rl._by_key      # the idle person's entry is gone, not accumulating forever
    assert rl.reserve("a")


def test_refunding_twice_or_a_stale_ticket_is_harmless():
    rl = feedback._RateLimiter(max_calls=1, window_seconds=60)
    t = rl.reserve("a")
    rl.refund("a", t)
    rl.refund("a", t)
    rl.refund("nobody", (0.0, 0))
    assert rl.reserve("a") and rl.reserve("a") is None


# ---------------------------------------------------------- who filed it (public repo)
def _body_for(name, monkeypatch, mode="entra", user=None):
    monkeypatch.setattr(auth, "AUTH_MODE", mode)
    captured = []
    _file(VALID_BODY, user or _person("oid-body", name=name), monkeypatch, _ok_post(captured))
    return feedback._issue_body(captured[0])


def test_the_display_name_is_added_in_entra_mode(monkeypatch):
    body = _body_for("Ada Lovelace", monkeypatch)
    assert body.rstrip().endswith("Submitted by Ada Lovelace")


def test_email_and_oid_never_go_into_a_public_issue(monkeypatch):
    user = _person("oid-secret-123", name="Ada Lovelace", email="ada.private@example.com")
    body = _body_for("Ada Lovelace", monkeypatch, user=user)
    assert "ada.private@example.com" not in body and "oid-secret-123" not in body and "@example.com" not in body


@pytest.mark.parametrize("mode", ["none", "token"])
def test_no_name_is_added_without_a_real_identity(monkeypatch, mode):
    """none/token mode has a synthetic identity ('Local' / 'Shared token') - not a person."""
    user = auth.LOCAL_USER if mode == "none" else auth.SHARED_USER
    assert "Submitted by" not in _body_for("x", monkeypatch, mode=mode, user=user)


@pytest.mark.parametrize("hostile,expected_present,forbidden", [
    ("Ada\n---\n## Injected heading", "Ada - Injected heading", ["\n---\n", "##", "---"]),
    ("__Admin__ _italic_ ***", "Admin italic", ["_", "*"]),
    ("Ada ....... ------", "Ada . -", []),
    ("Ada @octocat @everyone", "Ada octocat everyone", ["@"]),
    ("Ada `code` <b>bold</b> <script>alert(1)</script>", "Ada code bboldb scriptalert1script", ["<", ">", "`"]),
    ("[click](http://evil.example)", "clickhttpevil.example", ["](", "[", "]"]),
    ("Ada\r\nInjected: header", "Ada Injected header", ["\r", "\n---"]),
])
def test_a_hostile_display_name_cannot_inject_markdown_or_mentions(hostile, expected_present, forbidden):
    line = feedback._submitter_line(hostile)
    for bad in forbidden:
        assert bad not in line, (hostile, line)
    assert line.startswith("\nSubmitted by ") and "\n" not in line[1:]     # exactly one extra line
    assert expected_present in line


def test_names_from_other_scripts_survive_and_long_names_are_capped():
    assert "José Müller-Ñu" in feedback._submitter_line("José Müller-Ñu")
    assert "李小龍" in feedback._submitter_line("李小龍")
    assert len(feedback._submitter_line("N" * 500)) <= len("\nSubmitted by ") + 80


def test_a_name_that_cleans_to_nothing_adds_no_line():
    assert feedback._submitter_line("") == "" and feedback._submitter_line("@@@```<>") == ""


def test_the_client_cannot_choose_who_it_was_submitted_by():
    fb = feedback.validate_feedback({**VALID_BODY, "submitted_by": "The Boss"})
    assert fb.submitted_by == ""


def test_the_route_names_and_rate_limits_the_signed_in_person(monkeypatch):
    """End to end through /api/feedback: a dependency override stands in for a
    real Entra token (those paths are covered in tests/test_ownership.py)."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(auth, "AUTH_MODE", "entra")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=1, window_seconds=3600))
    captured = []
    monkeypatch.setattr(feedback, "_post_issue_to_github", _ok_post(captured))
    who = {"user": _person("oid-1", name="Ada Lovelace")}
    main.app.dependency_overrides[main.get_current_user] = lambda: who["user"]
    try:
        assert c.post("/api/feedback", json=VALID_BODY).status_code == 201
        assert captured[0].submitted_by == "Ada Lovelace"
        assert c.post("/api/feedback", json=VALID_BODY).status_code == 429     # Ada's allowance is used up...
        who["user"] = _person("oid-2", name="Bob")
        assert c.post("/api/feedback", json=VALID_BODY).status_code == 201     # ...Bob's is not
        assert captured[1].submitted_by == "Bob"
    finally:
        main.app.dependency_overrides.clear()


# ------------------------------------------------ review follow-ups (#73)
@pytest.mark.parametrize("display_name,email,oid", [
    ("ada@contoso.com", "ada@contoso.com", "oid-1"),                      # auth.py falls back to preferred_username (an email)
    ("11111111-2222-3333-4444-555555555555", "", "11111111-2222-3333-4444-555555555555"),   # ...then to the object id
    ("oid-plain", "", "oid-plain"),
    ("Ada (ada@contoso.com)", "ada@contoso.com", "oid-2"),                # an email tucked inside a name
    ("", "ada@contoso.com", "oid-3"),
    ("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE", "", "other"),                # looks like a GUID even if it isn't this user's
])
def test_an_email_or_object_id_fallback_is_never_published(monkeypatch, display_name, email, oid):
    user = auth.CurrentUser(oid=oid, email=email, display_name=display_name)
    body = _body_for(display_name, monkeypatch, user=user)
    assert "Submitted by" not in body


def test_a_real_name_is_still_published(monkeypatch):
    assert "Submitted by Ada Lovelace" in _body_for("Ada Lovelace", monkeypatch)


def test_a_pipeline_identity_is_never_named(monkeypatch):
    pipe = auth.CurrentUser(oid="pipe-oid", email="", display_name="Review pipeline", is_pipeline=True)
    assert "Submitted by" not in _body_for("x", monkeypatch, user=pipe)


@pytest.mark.parametrize("mode", ["none", "token", "entra"])
def test_a_pre_set_submitted_by_is_never_trusted(monkeypatch, mode):
    """Only file_feedback decides the attribution, whatever the object it was
    handed already carried."""
    monkeypatch.setattr(auth, "AUTH_MODE", mode)
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    captured = []
    monkeypatch.setattr(feedback, "_post_issue_to_github", _ok_post(captured))
    fb = feedback.FeedbackIn(kind="issue", title="A real bug here", description="d", submitted_by="The Boss")
    user = auth.LOCAL_USER if mode == "none" else auth.SHARED_USER if mode == "token" else _person("oid-x", name="Ada Lovelace")
    asyncio.run(feedback.file_feedback(fb, user))
    assert "The Boss" not in feedback._issue_body(captured[0])
    assert captured[0].submitted_by == ("Ada Lovelace" if mode == "entra" else "")


def test_underscores_and_repeated_punctuation_are_stripped_from_the_name():
    assert feedback._submitter_line("__Admin__") == "\nSubmitted by Admin"
    assert feedback._submitter_line("Mary-Jane O'Neil Jr.") == "\nSubmitted by Mary-Jane O'Neil Jr."   # ordinary punctuation survives


# ---- refunds hinge on a dedicated signal, not on an exception's base class
def test_an_unreadable_2xx_is_reported_by_a_dedicated_exception():
    def handler(request):
        return httpx.Response(201, content=b"<html>not json</html>")
    fb = feedback.FeedbackIn(kind="issue", title="A real bug here", description="d")
    with pytest.raises(feedback.UnreadableReply):
        asyncio.run(feedback._post_issue_to_github("tok", fb, transport=httpx.MockTransport(handler)))


def test_a_non_2xx_is_an_http_error_not_an_unreadable_reply():
    def handler(request):
        return httpx.Response(500, json={"message": "server error"})
    fb = feedback.FeedbackIn(kind="issue", title="A real bug here", description="d")
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(feedback._post_issue_to_github("tok", fb, transport=httpx.MockTransport(handler)))


def test_an_unrelated_value_error_before_anything_was_sent_is_refunded(monkeypatch):
    """e.g. a token that can't be encoded into a header raises a ValueError
    subclass before any request goes out - that must not eat the allowance."""
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    monkeypatch.setattr(feedback, "_rate_limiter", feedback._RateLimiter(max_calls=1, window_seconds=3600))

    async def not_sent(token, fb):
        raise UnicodeEncodeError("ascii", "é", 0, 1, "ordinal not in range")
    monkeypatch.setattr(feedback, "_post_issue_to_github", not_sent)
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 502
    _mock_success(monkeypatch)
    assert c.post("/api/feedback", json=VALID_BODY).status_code == 201


def test_a_cancelled_request_gives_its_slot_back(monkeypatch):
    monkeypatch.setenv("GITHUB_FEEDBACK_TOKEN", "fake-token")
    limiter = feedback._RateLimiter(max_calls=1, window_seconds=3600, global_max_calls=1)
    monkeypatch.setattr(feedback, "_rate_limiter", limiter)

    async def hangs(token, fb):
        await asyncio.sleep(60)

    monkeypatch.setattr(feedback, "_post_issue_to_github", hangs)

    async def scenario():
        fb = feedback.validate_feedback(VALID_BODY)
        task = asyncio.ensure_future(feedback.file_feedback(fb, _person("oid-c")))
        await asyncio.sleep(0.05)              # it has reserved its slot and is waiting on "GitHub"
        assert limiter.reserve("oid-c") is None
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task                          # cancellation propagates; it is not swallowed into a 502
        return limiter.reserve("oid-c")

    assert asyncio.run(scenario()) is not None  # and both the personal and the global slot were freed


def test_the_global_ceiling_tradeoff_is_documented():
    docs = open(os.path.join(os.path.dirname(__file__), "..", "docs", "FEEDBACK.md"), encoding="utf-8").read()
    assert "20 per hour across everyone" in docs and "use it up" in docs
