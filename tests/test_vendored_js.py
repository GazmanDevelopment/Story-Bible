"""
#74: the JavaScript vendored under app/static/vendor/ is invisible to pip-audit
and Dependabot, so scripts/check_vendored_js.py checks it. These tests cover
that script and the assumptions its reviewed-advisory list rests on.
"""
import importlib.util
import os
import re
import shutil
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
    (vendor / "quill" / "quill.js").write_text('static version="9.9.9";', encoding="utf-8")   # a different version's file
    (vendor / "msal" / "NOTICE.md").write_text("MSAL (v3.30.0) vendored.", encoding="utf-8")
    (vendor / "msal" / "msal-browser.min.js").write_text('/*! @azure/msal-browser v3.30.0 2025-08-05 */', encoding="utf-8")
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


def test_a_stray_version_literal_is_not_mistaken_for_the_libraries_own_marker(tmp_path, monkeypatch):
    """Bundles contain other packages' version strings; the check wants the
    library's own banner/constant, so '"2.0.3"' appearing somewhere is not enough."""
    vendor = tmp_path / "vendor"
    for folder, fname, body in (("quill", "quill.js", 'var other = "2.0.3";'),
                                ("msal", "msal-browser.min.js", 'const dep = "3.30.0";')):
        (vendor / folder).mkdir(parents=True)
        (vendor / folder / "NOTICE.md").write_text("Lib (v2.0.3) / (v3.30.0)".split(" / ")[0 if folder == "quill" else 1], encoding="utf-8")
        (vendor / folder / fname).write_text(body, encoding="utf-8")
    monkeypatch.setattr(check, "VENDOR", vendor)
    assert len(check.consistency_problems()) == 2


def _app_sources():
    static = ROOT / "app" / "static"
    return [p for p in static.rglob("*") if p.suffix in (".js", ".html") and "vendor" not in p.relative_to(static).parts]


def test_the_quill_advisory_assumption_still_holds():
    """CVE-2025-15056 is only reachable through Quill's HTML *export* API. The
    reviewed-advisory entry says the pane never uses it; this fails the build
    if any of the app's own scripts or pages starts to, so the exception can't
    outlive its reason."""
    sources = _app_sources()
    assert len(sources) >= 4 and any(p.name == "app.js" for p in sources)   # the scan is actually looking at something
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for api in ("getSemanticHTML", "getHTML"):
            assert api not in text, f"{path.name} now uses Quill's {api}: re-review GHSA-v3m3-f69x-jf25 (CVE-2025-15056)"


# ---- the audit must fail closed
GOOD_REPORT = {"auditReportVersion": 2, "vulnerabilities": {}, "metadata": {"vulnerabilities": {"total": 0}}}


def test_a_genuine_report_is_accepted_even_with_no_findings():
    assert check.report_problem(GOOD_REPORT) is None
    assert check.report_problem({**GOOD_REPORT, "vulnerabilities": SAMPLE_REPORT["vulnerabilities"]}) is None


@pytest.mark.parametrize("bad", [
    {"message": "request to https://registry.npmjs.org failed", "error": {"code": "ENOAUDIT", "summary": "no"}},
    {"error": {"code": "E404"}},
    {},
    {"vulnerabilities": {}},                    # looks plausible but isn't a completed audit
    [], "text", None,
])
def test_npm_error_output_is_never_read_as_a_clean_audit(bad):
    assert check.report_problem(bad)


def test_run_npm_audit_stops_when_npm_returns_an_error_object(monkeypatch):
    """The fail-open the review found: registry unreachable -> valid JSON error -> 'no advisories'."""
    calls = iter([subprocess.CompletedProcess([], 0, "", ""),
                  subprocess.CompletedProcess([], 1, '{"error": {"code": "ENOTFOUND"}}', "")])
    monkeypatch.setattr(check.shutil, "which", lambda n: "npm")
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: next(calls))
    with pytest.raises(SystemExit) as exc:
        check.run_npm_audit()
    assert "AUDIT NOT RUN" in str(exc.value)


def test_run_npm_audit_stops_when_the_lockfile_step_fails(monkeypatch):
    monkeypatch.setattr(check.shutil, "which", lambda n: "npm")
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1, "", "ENOTFOUND"))
    with pytest.raises(SystemExit) as exc:
        check.run_npm_audit()
    assert "AUDIT NOT RUN" in str(exc.value)


def test_the_script_passes_offline():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_vendored_js.py")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "quill: vendored version" in out.stdout


@pytest.mark.skipif(
    not (os.environ.get("RUN_LIVE_AUDIT") and shutil.which("npm")),
    reason="opt-in: needs npm and network, and would fail on a newly published advisory - "
           "the weekly .github/workflows/security-audit.yml runs this; set RUN_LIVE_AUDIT=1 to run it here",
)
def test_the_live_audit_finds_nothing_unreviewed():
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "check_vendored_js.py"), "--audit"],
                         capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
