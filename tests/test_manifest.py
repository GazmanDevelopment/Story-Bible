"""
Covers what doesn't need Node/office-addin-manifest (that's CI's job, via
`npx office-addin-manifest validate` in .github/workflows/ci.yml, since it
does real semantic validation this can't) - structural checks pure Python
can do, so a plain `pytest` run also catches drift, not just CI.
"""
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import generate_manifest as gen  # noqa: E402


def test_template_is_well_formed():
    ET.parse(gen.TEMPLATE)

def test_each_environment_generates_well_formed_xml_with_no_placeholders_left(tmp_path, monkeypatch):
    monkeypatch.setattr(gen, "ROOT", tmp_path)  # write scratch output here, never into the real repo
    for env in gen.ENVIRONMENTS:
        out = gen.generate(env)
        ET.parse(out)
        assert "{{" not in out.read_text(), f"{env}: unreplaced placeholder"

def test_environments_have_distinct_ids():
    ids = {cfg["id"] for cfg in gen.ENVIRONMENTS.values()}
    assert len(ids) == len(gen.ENVIRONMENTS)

def test_version_is_four_part_and_major_at_least_1():
    version = gen._version()
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version), version
    assert int(version.split(".")[0]) >= 1  # office-addin-manifest rejects major 0 - verified by running it

def test_dev_points_at_localhost_prod_does_not():
    assert gen.ENVIRONMENTS["dev"]["base_url"] == "https://localhost:3000"
    assert "localhost" not in gen.ENVIRONMENTS["prod"]["base_url"]

def test_prod_app_domains_include_entra_login_ahead_of_10_13():
    # Written as an exact per-element match, not `x in some_string`, so this
    # isn't mistakable for the URL-substring-sanitization anti-pattern (a
    # domain check that trusts *any* position of a substring in a larger
    # string) - app_domains is a list, membership here is exact equality.
    domains: list[str] = gen.ENVIRONMENTS["prod"]["app_domains"]
    assert any(d == "https://login.microsoftonline.com" for d in domains)

def test_version_import_is_independent_of_root_used_for_output(tmp_path):
    """_version() imports `app` to read its version, and generate() writes
    output to ROOT (which tests elsewhere monkeypatch to redirect that
    output) - the two must stay independent. Run in a fresh subprocess,
    cwd'd outside the repo and with ROOT pointed elsewhere, so this can't
    pass by accident the way it would in-process (where `app` is already
    cached in sys.modules from earlier test files, and `-m pytest`'s own
    cwd-on-sys.path behavior would paper over exactly this bug)."""
    script = (
        f"import sys; sys.path.insert(0, {str(ROOT / 'scripts')!r})\n"
        "import generate_manifest as gen\n"
        f"gen.ROOT = {str(tmp_path)!r}\n"
        "print(gen._version())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(tmp_path)
    )
    assert result.returncode == 0, result.stderr
    assert re.fullmatch(r"\d+\.\d+\.\d+\.\d+", result.stdout.strip())

def test_committed_manifests_match_what_the_generator_produces_now(tmp_path, monkeypatch):
    """The same drift check CI runs (regenerate, diff against git) at the
    Python level, so a plain `pytest` also catches a hand-edited
    manifest.dev.xml/manifest.prod.xml, not just CI's npx step. Generates
    into tmp_path (not the real committed path) so this stays read-only
    against the actual repo files."""
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    for env in gen.ENVIRONMENTS:
        committed = ROOT / f"manifest.{env}.xml"
        assert committed.exists(), f"manifest.{env}.xml is not committed - run scripts/generate_manifest.py"
        fresh = gen.generate(env)
        assert committed.read_text() == fresh.read_text(), (
            f"manifest.{env}.xml doesn't match manifest.template.xml - "
            f"regenerate with: python scripts/generate_manifest.py {env}"
        )


# ------------------------------------------------ GitHub Pages copy (#61)
def test_generating_prod_also_publishes_docs_manifest(tmp_path, monkeypatch):
    """docs/manifest.xml is what GitHub Pages serves for sideloading onto a
    new laptop (Word's Upload My Add-in) - it must be an exact copy of
    manifest.prod.xml, kept in sync by generate() itself rather than a
    second hand-maintained copy that can drift."""
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    prod = gen.generate("prod")
    published = tmp_path / "docs" / "manifest.xml"
    assert published.exists()
    assert published.read_text() == prod.read_text()

def test_generating_dev_does_not_touch_the_published_copy(tmp_path, monkeypatch):
    """dev's manifest points at localhost - publishing it via Pages would
    hand out a manifest that can never work outside this machine."""
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    prod = gen.generate("prod")
    gen.generate("dev")
    published = tmp_path / "docs" / "manifest.xml"
    assert published.read_text() == prod.read_text()
    assert "localhost" not in published.read_text()

def test_committed_docs_manifest_matches_prod(tmp_path, monkeypatch):
    """Same drift check as test_committed_manifests_match_what_the_generator_
    produces_now, extended to the Pages copy - covered separately since it's
    a derived file the template doesn't touch directly."""
    monkeypatch.setattr(gen, "ROOT", tmp_path)
    fresh = gen.generate("prod")
    committed = ROOT / "docs" / "manifest.xml"
    assert committed.exists(), "docs/manifest.xml is not committed - run scripts/generate_manifest.py prod"
    assert committed.read_text() == fresh.read_text(), (
        "docs/manifest.xml doesn't match manifest.prod.xml - "
        "regenerate with: python scripts/generate_manifest.py prod"
    )

def test_docs_landing_page_links_to_the_manifest_and_the_sideload_steps():
    index = (ROOT / "docs" / "index.html").read_text()
    assert "manifest.xml" in index
    assert "Upload My Add-in" in index

def test_docs_has_nojekyll_so_pages_serves_plain_static_files():
    """Without this, GitHub Pages runs its default Jekyll build - which is
    liable to mangle a hand-written index.html/manifest.xml pair that was
    never meant to be Jekyll input."""
    assert (ROOT / "docs" / ".nojekyll").exists()
