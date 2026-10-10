"""#164: the web app is installable on a phone (manifest, icons, service worker)."""
import json
import os
import re
import tempfile
from pathlib import Path

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import app.main as main
from fastapi.testclient import TestClient

c = TestClient(main.app)
STATIC = Path(main.STATIC_DIR)
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")


def _manifest():
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    return r, r.json()


def test_manifest_is_served_as_a_web_manifest_and_revalidated():
    r, _ = _manifest()
    assert r.headers["content-type"].split(";")[0] in ("application/manifest+json", "application/json")
    assert r.headers.get("cache-control") == "no-cache"


def test_manifest_has_what_browsers_need_to_offer_install():
    _, m = _manifest()
    assert m["name"] and m["short_name"]
    assert m["display"] in ("standalone", "fullscreen", "minimal-ui")
    assert m["start_url"] == "/" and m["scope"] == "/"
    assert re.fullmatch(r"#[0-9a-fA-F]{6}", m["theme_color"])
    sizes = {(i["sizes"], i["purpose"]) for i in m["icons"]}
    assert ("192x192", "any") in sizes
    assert ("512x512", "any") in sizes
    assert ("512x512", "maskable") in sizes


def test_every_manifest_icon_is_served_at_its_declared_size():
    from PIL import Image
    import io
    _, m = _manifest()
    for icon in m["icons"]:
        r = c.get("/" + icon["src"])
        assert r.status_code == 200, icon["src"]
        assert r.headers["content-type"] == "image/png"
        w, h = Image.open(io.BytesIO(r.content)).size
        assert f"{w}x{h}" == icon["sizes"], icon["src"]


def test_index_links_the_manifest_and_theme_colour():
    assert 'rel="manifest" href="manifest.webmanifest"' in INDEX
    # credentials sent, so a cookie-based gateway in front doesn't hide the manifest
    assert 'crossorigin="use-credentials"' in INDEX
    assert 'name="theme-color"' in INDEX


def test_service_worker_is_served_from_the_root_scope_as_javascript():
    r = c.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    # no-cache so a changed worker is picked up on the next visit
    assert r.headers.get("cache-control") == "no-cache"


def test_service_worker_never_caches_or_answers_requests():
    """It must stay a pass-through: a cached /api/* or sign-in response would be
    stale data or a stale token, and a cached app.js would defeat the no-cache
    policy that makes a deploy take effect at once."""
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    assert "respondWith" not in code
    assert "caches." not in code


def test_service_worker_is_only_registered_for_the_plain_web_host():
    m = re.search(r"function registerServiceWorker\(\) \{(.*?)\n\}", APP_JS, re.S)
    assert m, "registerServiceWorker() missing from app.js"
    body = m.group(1)
    assert 'host.name !== "web"' in body        # not in Word or the Google Docs sidebar
    assert "window.parent !== window" in body   # not when framed
    assert 'register("sw.js"' in body


def test_csp_allows_the_manifest_and_worker():
    """Same-origin manifest and worker must stay allowed: either default-src
    'self' covers them (as it does today) or an explicit directive does."""
    csp = main.DEFAULT_CSP

    def allows(directive):
        for part in csp.split(";"):
            tokens = part.split()
            if tokens and tokens[0] == directive:
                return "'self'" in tokens
        return None

    for d in ("manifest-src", "worker-src"):
        explicit = allows(d)
        assert explicit if explicit is not None else allows("default-src")
