"""Static checks for the GitHub Pages help guide (#81): no browser needed."""
import re
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PAGES = [DOCS / "index.html", DOCS / "help" / "index.html"]
APP_JS = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
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
    for sec in ("getting-started", "series", "timeline", "relationship-types",
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
    assert "openBrowserWindow" in APP_JS


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
