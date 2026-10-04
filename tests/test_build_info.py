"""#144: the build version and date are exposed by the API and filed with every
issue/suggestion, and only ever as plain text."""
import importlib
import os
import tempfile

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import pytest
from fastapi.testclient import TestClient

import app as app_pkg
from app import github_feedback as feedback, main

c = TestClient(main.app)


def _reload_with(monkeypatch, **env):
    for k in ("BUILD_VERSION", "BUILD_DATE"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return importlib.reload(app_pkg)


@pytest.fixture(autouse=True)
def _restore_app_package(monkeypatch):
    yield
    monkeypatch.undo()
    importlib.reload(app_pkg)


def test_unstamped_source_checkout_reports_dev_and_no_date(monkeypatch):
    m = _reload_with(monkeypatch)
    assert (m.BUILD_VERSION, m.BUILD_DATE) == ("dev", "")
    assert m.build_label() == "dev"


def test_stamped_build_reports_version_and_date(monkeypatch):
    m = _reload_with(monkeypatch, BUILD_VERSION="v0.1.0-12-gabc1234", BUILD_DATE="2026-10-03")
    assert m.build_label() == "v0.1.0-12-gabc1234 (2026-10-03)"


@pytest.mark.parametrize("bad", ["<script>alert(1)</script>", "a\nb", "x" * 65, "[link](http://evil)", "@mention`"])
def test_a_stamp_with_unsafe_text_is_dropped(monkeypatch, bad):
    m = _reload_with(monkeypatch, BUILD_VERSION=bad, BUILD_DATE=bad)
    assert (m.BUILD_VERSION, m.BUILD_DATE) == ("dev", "")


def test_config_and_health_expose_the_build_stamp(monkeypatch):
    monkeypatch.setattr(main, "BUILD_VERSION", "v9.9.9")
    monkeypatch.setattr(main, "BUILD_DATE", "2026-01-02")
    cfg = c.get("/api/config").json()
    assert (cfg["buildVersion"], cfg["buildDate"]) == ("v9.9.9", "2026-01-02")
    h = c.get("/api/health").json()
    assert (h["build"], h["buildDate"]) == ("v9.9.9", "2026-01-02")


def test_the_filed_issue_body_includes_the_build(monkeypatch):
    monkeypatch.setattr(feedback, "build_label", lambda: "v9.9.9 (2026-01-02)")
    body = feedback._issue_body(feedback.FeedbackIn(kind="issue", title="Something broke", description="It broke"))
    assert "build v9.9.9 (2026-01-02)" in body
