"""
Covers scripts/generate_third_party_notices.py (see its own docstring for
the licensing rationale). The interesting behaviour to pin down here isn't
the exact package list (that's real, external dependency data, and would
make this test brittle against upstream churn) but the two things that
would otherwise be easy to get wrong silently: a marker-gated extra
requirements.txt never actually requests leaking in (jinja2/pyyaml, only
reachable via fastapi's "standard"/"all" extras), and the committed file
actually matching a fresh regeneration.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_third_party_notices as gen  # noqa: E402


def test_collects_at_least_the_five_direct_dependencies():
    names = {name.lower() for name, _license, _url in gen.collect()}
    for direct in ("fastapi", "uvicorn", "pydantic", "pyjwt", "httpx"):
        assert direct in names, f"{direct} missing from the collected dependency graph"


def test_never_includes_dev_only_tools():
    """pytest/Playwright are requirements-dev.txt-only and never ship in
    the Docker image (Dockerfile only ever installs requirements.txt) -
    the generator walks requirements.txt alone, so they must never appear
    even though they're installed alongside it in this test's own venv."""
    names = {name.lower() for name, _license, _url in gen.collect()}
    assert "pytest" not in names
    assert "playwright" not in names


def test_never_includes_extras_requirements_txt_does_not_request():
    """jinja2/pyyaml/python-multipart/etc. are only reachable through
    fastapi's "standard"/"all" extras - requirements.txt pins plain
    `fastapi==...`, no extras, so pulling these in would mean the marker/
    extra evaluation is broken, not that the app actually depends on them."""
    names = {name.lower() for name, _license, _url in gen.collect()}
    for extra_only in ("jinja2", "pyyaml", "python-multipart", "email-validator"):
        assert extra_only not in names, f"{extra_only} should be gated behind an extra requirements.txt never requests"


def test_pyjwts_crypto_extra_is_still_included():
    """The one extra requirements.txt *does* request (`pyjwt[crypto]`) -
    the opposite failure mode from the test above: under-following a real
    edge instead of over-following a fake one."""
    names = {name.lower() for name, _license, _url in gen.collect()}
    assert "cryptography" in names


def test_every_row_has_a_real_license_not_the_unknown_fallback():
    for name, license_, _url in gen.collect():
        assert license_ and license_ != "See package metadata", f"{name} has no usable license metadata"


def test_committed_notices_file_matches_a_fresh_regeneration():
    """Same drift check CI runs (regenerate, diff against git) at the
    Python level, so a plain `pytest` also catches a hand-edited or
    stale THIRD_PARTY_NOTICES.md, not just CI's own diff step."""
    committed = ROOT / "THIRD_PARTY_NOTICES.md"
    assert committed.exists(), "THIRD_PARTY_NOTICES.md is not committed - run scripts/generate_third_party_notices.py"
    assert committed.read_text() == gen.generate(), (
        "THIRD_PARTY_NOTICES.md is stale - regenerate with: "
        "python scripts/generate_third_party_notices.py"
    )
