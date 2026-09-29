"""
Checks the JavaScript libraries vendored under app/static/vendor/ (#74).

They are copied files, not packages, so neither pip-audit nor Dependabot sees
them. This script closes that gap in two steps:

1. Consistency (offline, always): the version each library's NOTICE.md claims
   must actually appear in the vendored file. Otherwise a file could be
   swapped for another version while the notice - and therefore any audit run
   from it - still says the old one.
2. `--audit` (needs npm + network): asks `npm audit` about exactly those
   versions. Any advisory that is not in KNOWN_ADVISORIES below fails the run,
   and each entry there needs a written reason, so "we looked and it does not
   apply" is a recorded decision rather than a silenced alarm.

Run by .github/workflows/security-audit.yml weekly and whenever the vendored
files change:  python scripts/check_vendored_js.py --audit
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR = ROOT / "app" / "static" / "vendor"

# name -> (npm package, folder under app/static/vendor, the vendored file, and a
# regex for the library's OWN version marker in that file - {v} is the version).
# Not just "the version string appears somewhere": a bundle can contain other
# libraries' version literals, so each pattern is the library's own banner/constant.
LIBRARIES = {
    "quill": ("quill", "quill", "quill.js", r'static version="{v}"'),
    "msal-browser": ("@azure/msal-browser", "msal", "msal-browser.min.js", r"/\*! @azure/msal-browser v{v} "),
}

# Advisories reviewed and judged not to apply. Each needs a reason that says
# WHY, and ideally names the code that makes it true - test_vendored_js.py
# guards the assumption where it can.
KNOWN_ADVISORIES = {
    "GHSA-v3m3-f69x-jf25": (
        "CVE-2025-15056: XSS in Quill 2.0.3's HTML *export* feature (getSemanticHTML). "
        "No patched release exists (2.0.3 is the latest). The pane never calls the export "
        "API: it reads the editor with quill.root.innerHTML and the server sanitizes the "
        "body on every write (app/html_sanitize.py). tests/test_vendored_js.py fails if "
        "app.js ever starts using the export API. Re-review when Quill publishes a fix."
    ),
}


def notice_version(library: str) -> str:
    """The version a library's NOTICE.md records, e.g. '(v2.0.3)' -> '2.0.3'."""
    _, folder, _, _ = LIBRARIES[library]
    text = (VENDOR / folder / "NOTICE.md").read_text(encoding="utf-8")
    m = re.search(r"\(v(\d+(?:\.\d+)+)\)", text)
    if not m:
        raise ValueError(f"{folder}/NOTICE.md doesn't record a version like '(v1.2.3)'")
    return m.group(1)


def consistency_problems() -> list[str]:
    problems = []
    for library, (_, folder, filename, marker) in LIBRARIES.items():
        try:
            version = notice_version(library)
        except (OSError, ValueError) as e:
            problems.append(f"{library}: {e}")
            continue
        body = (VENDOR / folder / filename).read_text(encoding="utf-8", errors="replace")
        if not re.search(marker.format(v=re.escape(version)), body):
            problems.append(
                f"{library}: NOTICE.md says {version} but {folder}/{filename} has no matching version marker "
                f"(expected /{marker.format(v=version)}/) - was the file replaced without updating the notice?")
    return problems


def advisory_ids(audit_json: dict) -> dict[str, str]:
    """GHSA id -> 'package: title' for every advisory in an `npm audit --json` report."""
    found: dict[str, str] = {}
    for name, vuln in (audit_json.get("vulnerabilities") or {}).items():
        for via in vuln.get("via", []):
            if not isinstance(via, dict):
                continue  # a string means "vulnerable because of another package", which has its own entry
            m = re.search(r"(GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})", via.get("url", ""))
            found[m.group(1) if m else via.get("url") or via.get("title", "unknown")] = f"{name}: {via.get('title', '')}"
    return found


def unreviewed_advisories(audit_json: dict) -> dict[str, str]:
    return {k: v for k, v in advisory_ids(audit_json).items() if k not in KNOWN_ADVISORIES}


def report_problem(report: object) -> str | None:
    """Why an `npm audit --json` result can't be trusted as a clean bill of health,
    or None if it's a genuine audit report. npm prints a JSON *error* object when
    it can't reach the registry; treating any parseable JSON as a report would let
    a failed audit look like 'no advisories' (a fail-open in a security gate)."""
    if not isinstance(report, dict):
        return "not a JSON object"
    if "error" in report:
        return f"npm reported an error: {json.dumps(report['error'])[:300]}"
    if "auditReportVersion" not in report or "vulnerabilities" not in report or "metadata" not in report:
        return "missing auditReportVersion/vulnerabilities/metadata, so it does not look like a completed audit"
    return None


def run_npm_audit() -> dict:
    npm = shutil.which("npm")
    if not npm:
        raise SystemExit("npm not found - install Node.js to run --audit")
    specs = [f"{pkg}@{notice_version(lib)}" for lib, (pkg, _, _, _) in LIBRARIES.items()]
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "package.json").write_text(
            json.dumps({"name": "vendored-js-audit", "version": "0.0.0", "private": True}), encoding="utf-8")
        common = dict(cwd=tmp, capture_output=True, text=True, timeout=300)
        # Only builds a lockfile of the exact vendored versions; nothing is installed or run.
        install = subprocess.run(
            [npm, "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund", *specs], **common)
        if install.returncode != 0:
            raise SystemExit(f"AUDIT NOT RUN - npm could not resolve {specs}:\n{install.stderr}")
        result = subprocess.run([npm, "audit", "--json"], **common)  # exits non-zero when it finds anything
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError:
            raise SystemExit(f"AUDIT NOT RUN - npm audit gave no usable output:\n{result.stdout}\n{result.stderr}")
        problem = report_problem(report)
        if problem:
            raise SystemExit(f"AUDIT NOT RUN - {problem}")
        return report


def latest_versions() -> dict[str, str]:
    npm = shutil.which("npm")
    out = {}
    for lib, (pkg, _, _, _) in LIBRARIES.items():
        r = subprocess.run([npm, "view", pkg, "version"], capture_output=True, text=True, timeout=60)
        out[lib] = r.stdout.strip() or "?"
    return out


def main(argv: list[str]) -> int:
    problems = consistency_problems()
    for library in LIBRARIES:
        try:
            print(f"{library}: vendored version {notice_version(library)}")
        except (OSError, ValueError):
            pass
    for p in problems:
        print(f"PROBLEM: {p}")
    if problems:
        return 1
    if "--audit" not in argv:
        print("OK (offline consistency check only; pass --audit to also query the npm advisory database)")
        return 0

    report = run_npm_audit()
    bad = unreviewed_advisories(report)
    known = {k: v for k, v in advisory_ids(report).items() if k in KNOWN_ADVISORIES}
    for ghsa, what in known.items():
        print(f"reviewed, not affected: {ghsa} ({what})\n    {KNOWN_ADVISORIES[ghsa]}")
    stale = [g for g in KNOWN_ADVISORIES if g not in advisory_ids(report)]
    for ghsa in stale:
        print(f"note: {ghsa} is in KNOWN_ADVISORIES but npm no longer reports it - remove it if the library was updated")
    print("latest published versions (informational): " + ", ".join(f"{k} {v}" for k, v in latest_versions().items()))
    if bad:
        for ghsa, what in bad.items():
            print(f"UNREVIEWED ADVISORY: {ghsa} - {what}")
        print("Update the vendored library, or - if it genuinely doesn't apply - record why in KNOWN_ADVISORIES.")
        return 1
    print("OK: no unreviewed advisories")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
