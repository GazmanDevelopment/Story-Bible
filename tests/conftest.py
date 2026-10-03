"""
Suite-wide test setup.

ENABLE_API_DOCS: app/main.py serves Swagger/ReDoc/openapi.json only when this
is set (off by default since #68). tests/test_openapi.py exercises them, and
pytest loads this file before importing any test module - so setting it here
is early enough to reach app.main's import-time read. The "off by default"
behaviour is tested in a subprocess instead (tests/test_public_surface.py),
since the flag is read once at import.
"""
import os

# Assigned, not setdefault: a stray ENABLE_API_DOCS=false in the developer's
# shell would otherwise fail tests/test_openapi.py for an unrelated reason.
os.environ["ENABLE_API_DOCS"] = "true"


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_write_rate_limit(monkeypatch):
    """The per-person write limiter (#89) is in-process state that would
    otherwise carry across tests, and the suite's many rapid writes as one test
    person would trip it. tests/test_abuse_limits.py installs its own."""
    from app import main
    monkeypatch.setattr(main, "_write_limiter", None)
    monkeypatch.setattr(main, "_lookup_limiter", None)  # #131; tests/test_ownership.py installs its own


@pytest.fixture(autouse=True)
def policy_gate_open(monkeypatch):
    """Server-side policy enforcement (#130) 403s anyone who hasn't accepted the
    current privacy policy, and almost no test is about that - they sign in as an
    arbitrary person and then exercise something else. So by default everyone
    counts as accepted. tests/test_policy_acceptance.py overrides this fixture
    (same name) to test the real gate."""
    from app import main
    monkeypatch.setattr(main, "policy_accepted", lambda user: True)
