"""Static checks for the GitHub Pages help guide (#81): no browser needed."""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
LEGAL = [DOCS / "privacy.html", DOCS / "terms.html"]
PAGES = [DOCS / "index.html", DOCS / "help" / "index.html", *LEGAL]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
HOST_JS = (ROOT / "app" / "static" / "host.js").read_text(encoding="utf-8")
APP_HTML = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")


class _Scan(HTMLParser):
    def __init__(self):
        super().__init__()
        self.refs, self.ids, self.imgs, self.links = [], set(), [], []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        for k in ("href", "src"):
            if k in a:
                self.refs.append(a[k])
        if tag == "img":
            self.imgs.append(a)
        if tag == "link":
            self.links.append(a)


def _scan(path):
    s = _Scan()
    s.feed(path.read_text(encoding="utf-8"))
    return s


def test_relative_targets_exist():
    for page in PAGES:
        for ref in _scan(page).refs:
            if re.match(r"^(https?:|mailto:|#)", ref):
                continue
            target = (page.parent / ref.split("#")[0]).resolve()
            assert target.exists(), f"{page.name}: broken link {ref}"


def test_anchor_links_have_ids():
    for page in PAGES:
        s = _scan(page)
        for ref in s.refs:
            if ref.startswith("#"):
                assert ref[1:] in s.ids, f"{page.name}: no id for {ref}"


def test_images_have_alt_text():
    for page in PAGES:
        for img in _scan(page).imgs:
            assert img.get("alt"), f"{page.name}: image without alt {img}"


def test_guide_sections_present():
    ids = _scan(DOCS / "help" / "index.html").ids
    for sec in ("getting-started", "google-docs", "series", "timeline", "relationship-types",
                "adding", "linking", "research", "backup", "feedback", "privacy", "delete-account"):
        assert sec in ids


def test_default_relationship_types_match_app():
    m = re.search(r"const REL_TYPES = \[(.*?)\];", APP_JS, re.S)
    types = re.findall(r'"([^"]+)"', m.group(1))
    assert types
    guide = (DOCS / "help" / "index.html").read_text(encoding="utf-8")
    for t in types:
        assert t in guide, f"relationship type {t!r} missing from the guide"


def test_help_url_points_at_the_guide():
    m = re.search(r'const HELP_URL = "([^"]+)"', APP_JS)
    assert m and m.group(1) == "https://gazmandevelopment.github.io/Story-Bible/help/"
    assert (DOCS / "help" / "index.html").exists()
    assert 'id="btnHelp"' in APP_HTML
    # Word needs Office to open the system browser (host.js); app.js asks the host to.
    assert "openBrowserWindow" in HOST_JS
    assert "host.openExternal(HELP_URL)" in APP_JS


def test_favicons_present_and_referenced():
    assert (DOCS / "favicon.ico").stat().st_size > 0
    assert (DOCS / "help" / "apple-touch-icon.png").stat().st_size > 0
    for page in PAGES:
        rels = {link.get("rel") for link in _scan(page).links}
        assert {"icon", "apple-touch-icon"} <= rels, page.name
    assert 'rel="icon"' in APP_HTML
    assert 'rel="apple-touch-icon"' in APP_HTML


def test_web_app_serves_the_same_favicon_as_the_help_pages():
    """#116: the web app shows the help guide's favicon, not just the add-in icon."""
    from fastapi.testclient import TestClient

    from app import main

    assert 'href="favicon.ico"' in APP_HTML
    c = TestClient(main.app)
    r = c.get("/favicon.ico")
    assert r.status_code == 200
    assert r.content == (DOCS / "favicon.ico").read_bytes()
    r = c.get("/assets/apple-touch-icon.png")
    assert r.status_code == 200
    assert r.content == (DOCS / "help" / "apple-touch-icon.png").read_bytes()


def test_screenshots_script_covers_every_guide_image():
    """Every image the guide uses is one make_help_screenshots.py writes."""
    script = (ROOT / "scripts" / "make_help_screenshots.py").read_text(encoding="utf-8")
    imgs = _scan(DOCS / "help" / "index.html").imgs
    assert imgs
    for img in imgs:
        name = Path(img["src"]).stem
        assert (DOCS / "help" / img["src"]).exists()
        assert f'"{name}"' in script, f"{name} is not produced by the screenshots script"


def test_legal_pages_have_version_and_contact():
    """The privacy policy and terms show a version and give the contact address."""
    for page in LEGAL:
        text = page.read_text(encoding="utf-8")
        assert re.search(r"Version \d+\.\d+, effective \d{1,2} \w+ \d{4}", text), page.name
        assert "gazman.development@gmail.com" in text, page.name
        assert not re.search(r"#\d+", text), f"{page.name}: issue number in user-facing text"


def test_default_policy_version_matches_the_legal_pages():
    """#111: POLICY_VERSION is what people accept; it must be the version the
    pages show. Bump both together (and the pages' date) when the text changes."""
    import os
    from app import main
    assert "POLICY_VERSION" not in os.environ, "unset POLICY_VERSION to test the default"
    for page in LEGAL:
        m = re.search(r"Version (\d+\.\d+), effective", page.read_text(encoding="utf-8"))
        assert m and m.group(1) == main.POLICY_VERSION, page.name


def test_guide_describes_the_policy_acceptance_screen():
    guide = (DOCS / "help" / "index.html").read_text(encoding="utf-8")
    assert "Accept and continue" in guide and "policy-gate.png" in guide


def test_privacy_policy_covers_required_topics():
    text = (DOCS / "privacy.html").read_text(encoding="utf-8").lower()
    for needle in ("uk gdpr", "gareth huscroft", "admin dashboard", "github", "local storage",
                   "14 days", "18, 20 and 23 months", "24 months", "no usable email",
                   "information commissioner", "72 hours", "at least 16", "erasure",
                   "portability", "restrict", "object", "rectification"):
        assert needle in text, f"privacy policy lacks {needle!r}"


def test_terms_cover_required_topics():
    text = (DOCS / "terms.html").read_text(encoding="utf-8").lower()
    for needle in ("acceptable use", "without any promise", "block", "keep ownership"):
        assert needle in text, f"terms lack {needle!r}"


def test_guide_and_readme_link_to_legal_pages():
    guide = (DOCS / "help" / "index.html").read_text(encoding="utf-8")
    assert 'href="../privacy.html"' in guide and 'href="../terms.html"' in guide
    assert "being finalised" not in guide
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/privacy.html" in readme and "docs/terms.html" in readme


def _guide():
    return (DOCS / "help" / "index.html").read_text(encoding="utf-8")


def test_guide_describes_google_docs_and_matches_the_menu():
    """The Google Docs section quotes the add-on's real menu labels (gdocs/Code.gs)."""
    guide = _guide()
    code = (ROOT / "gdocs" / "Code.gs").read_text(encoding="utf-8")
    assert re.search(r'<h2 id="google-docs">', guide) and "gdocs-sidebar.png" in guide
    assert "<strong>Story Bible</strong>" in guide   # the menu's title (createMenu)
    assert "createMenu('Story Bible')" in code
    for label in re.findall(r"addItem\('([^']+)'", code):
        assert f"<strong>{label}</strong>" in guide, f"menu item {label!r} not in the guide"
    for feature in ("Find", "Insert name at cursor", "Link to this series"):
        assert feature in guide


def test_guide_images_declare_their_real_size():
    """width/height on each <img> match the PNG, so the page doesn't jump while
    images load and a screenshot that changed size can't go unnoticed."""
    import struct

    for img in _scan(DOCS / "help" / "index.html").imgs:
        data = (DOCS / "help" / img["src"]).read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", img["src"]
        w, h = struct.unpack(">II", data[16:24])
        assert (int(img["width"]), int(img["height"])) == (w, h), f"{img['src']} is {w}x{h}"


def test_guide_has_no_stale_word_only_wording_or_issue_numbers():
    guide = _guide()
    assert "Word only" not in guide and "Word-only" not in guide   # Find etc. now also work in Google Docs
    assert not re.search(r"#\d+", guide), "issue number in user-facing text"
    index = (DOCS / "index.html").read_text(encoding="utf-8")
    assert 'href="help/#google-docs"' in index

