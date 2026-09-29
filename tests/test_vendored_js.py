"""
#74: the JavaScript vendored under app/static/vendor/ is invisible to pip-audit
and Dependabot, so scripts/check_vendored_js.py checks it. These tests cover
that script and the assumptions its reviewed-advisory list rests on.
"""
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_vendored_js", ROOT / "scripts" / "check_vendored_js.py")
check = importlib.util.module_from_spec(_spec)
sys.modules["check_vendored_js"] = check
_spec.loader.exec_module(check)


def test_each_notice_records_a_version_and_the_vendored_file_really_is_that_version():
    assert check.consistency_problems() == []
    assert check.notice_version("quill") == "2.0.3"
    assert check.notice_version("msal-browser") == "3.30.0"


def test_a_file_swapped_without_updating_its_notice_is_caught(tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    (vendor / "quill").mkdir(parents=True)
    (vendor / "msal").mkdir(parents=True)
    (vendor / "quill" / "NOTICE.md").write_text("Quill (v2.0.3) vendored.", encoding="utf-8")
    (vendor / "quill" / "quill.js").write_text('var v = "9.9.9";', encoding="utf-8")   # a different version's file
    (vendor / "msal" / "NOTICE.md").write_text("MSAL (v3.30.0) vendored.", encoding="utf-8")
    (vendor / "msal" / "msal-browser.min.js").write_text('var v = "3.30.0";', encoding="utf-8")
    monkeypatch.setattr(check, "VENDOR", vendor)
    problems = check.consistency_problems()
    assert len(problems) == 1 and "quill" in problems[0] and "2.0.3" in problems[0]


def test_a_notice_with_no_version_is_reported(tmp_path, monkeypatch):
    vendor = tmp_path / "vendor"
    for folder, fname in (("quill", "quill.js"), ("msal", "msal-browser.min.js")):
        (vendor / folder).mkdir(parents=True)
        (vendor / folder / "NOTICE.md").write_text("no version here", encoding="utf-8")
        (vendor / folder / fname).write_text("", encoding="utf-8")
    monkeypatch.setattr(check, "VENDOR", vendor)
    assert len(check.consistency_problems()) == 2


SAMPLE_REPORT = {"vulnerabilities": {"quill": {"via": [
    {"source": 1, "name": "quill", "title": "Quill is vulnerable to XSS via HTML export feature",
     "url": "https://github.com/advisories/GHSA-v3m3-f69x-jf25", "severity": "low"}]}}}


def test_a_reviewed_advisory_passes_and_an_unknown_one_fails():
    assert check.unreviewed_advisories(SAMPLE_REPORT) == {}
    other = {"vulnerabilities": {"@azure/msal-browser": {"via": [
        {"title": "Something new", "url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc", "severity": "high"}]}}}
    assert list(check.unreviewed_advisories(other)) == ["GHSA-aaaa-bbbb-cccc"]


def test_a_transitive_string_entry_and_an_empty_report_are_handled():
    assert check.advisory_ids({}) == {}
    assert check.advisory_ids({"vulnerabilities": {"x": {"via": ["quill"]}}}) == {}   # "via: another package" has its own entry


def test_every_reviewed_advisory_has_a_real_reason():
    assert check.KNOWN_ADVISORIES
    for ghsa, reason in check.KNOWN_ADVISORIES.items():
        assert re.fullmatch(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", ghsa)
        assert len(reason) > 80 and ("not" in reason or "never" in reason), f"{ghsa}: the reason should say why it doesn't apply"


def test_the_quill_advisory_assumption_still_holds():
    """CVE-2025-15056 is only reachable through Quill's HTML *export* API. The
    reviewed-advisory entry says the pane never uses it; this fails the build
    if that stops being true, so the exception can't outlive its reason."""
    js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for api in ("getSemanticHTML", "getHTML", "getSemantic"):
        assert api not in js, f"app.js now uses Quill's {api}: re-review GHSA-v3m3-f69x-jf25 (CVE-2025-15056)"
    assert "quill.root.innerHTML" in js       # what the pane actually does, sanitized server-side on write


def test_the_script_passes_offline():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_vendored_js.py")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "quill: vendored version" in out.stdout


@pytest.mark.skipif(not __import__("shutil").which("npm"), reason="needs npm and network")
def test_the_live_audit_finds_nothing_unreviewed():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_vendored_js.py"), "--audit"],
                         capture_output=True, text=True, timeout=300, cwd=ROOT)
    if "ENOTFOUND" in out.stderr or "network" in out.stderr.lower():
        pytest.skip("no network")
    assert out.returncode == 0, out.stdout + out.stderr
