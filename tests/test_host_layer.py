"""
Host abstraction: everything the pane does to "the document beside it" goes
through app/static/host.js, so a new host (e.g. a Google Docs sidebar) is one
more entry there and app.js never calls Office.js directly. These are static
checks; the behaviour in a real browser is covered by tests/test_ui.py.
"""
import os
import re
import tempfile
from pathlib import Path

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

from fastapi.testclient import TestClient

from app import main

c = TestClient(main.app)
STATIC = Path(main.STATIC_DIR)
APP_JS = (STATIC / "app.js").read_text(encoding="utf-8")
HOST_JS = (STATIC / "host.js").read_text(encoding="utf-8")
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "styles.css").read_text(encoding="utf-8")


def test_host_js_is_served_and_loaded_between_office_js_and_app_js():
    assert c.get("/host.js").status_code == 200
    order = [INDEX.index(s) for s in ("office.js", 'src="host.js"', 'src="app.js"')]
    assert order == sorted(order), "host.js must load after office.js and before app.js"


GDOCS = (STATIC / "gdocs.html").read_text(encoding="utf-8")


def test_gdocs_page_is_index_minus_office_js():
    # Office.js blanks any page it finds itself framed in (it assumes an Office
    # host), and the Google Docs sidebar frames the pane - so that page must not
    # load it. Everything else must stay identical to the Word page.
    assert c.get("/gdocs.html").status_code == 200
    assert "appsforoffice" not in GDOCS
    strip_comments = lambda s: re.sub(r"<!--.*?-->", "", s, flags=re.S)  # noqa: E731
    strip_office = lambda s: re.sub(r'\s*<script src="https://appsforoffice[^>]*></script>', "", s)  # noqa: E731
    norm = lambda s: re.sub(r"\s+", " ", strip_comments(strip_office(s)).replace(' data-host="gdocs"', ""))  # noqa: E731
    assert norm(GDOCS) == norm(INDEX), "gdocs.html drifted from index.html"
    assert 'data-host="gdocs"' in GDOCS and 'data-host' not in INDEX
    # the same scripts, in the same order
    scripts = lambda s: re.findall(r'<script src="([^"]+)"', s)  # noqa: E731
    assert [x for x in scripts(INDEX) if "appsforoffice" not in x] == scripts(GDOCS)


def test_app_js_does_not_call_office_directly():
    # The only Office.js code lives in host.js (auth-dialog.js is the dialog
    # page's own script and is out of scope here).
    assert not re.search(r"\bOffice\.(context|HostType|onReady|EventType|AsyncResultStatus)|\bWord\.run", APP_JS), \
        "app.js must go through `host`"
    assert "inWord" not in APP_JS


def test_host_exposes_the_documented_interface():
    for member in ("name", "hasDocument", "init", "nestedAuth", "openAuthDialog",
                   "readDocLink", "saveDocLink", "getSelection", "insertText", "openExternal"):
        assert re.search(rf"\b{member}\b", HOST_JS), member
    # app.js only calls members that exist
    used = set(re.findall(r"\bhost\.(\w+)", APP_JS))
    assert used, "app.js should use host"
    for member in used:
        assert re.search(rf"\b{member}\b", HOST_JS), f"host.{member} used in app.js but not defined in host.js"


def test_document_only_controls_are_gated_on_the_host_class():
    assert "doc-only" in INDEX and "word-only" not in INDEX
    assert "body.has-doc .doc-only" in CSS
    assert "in-word" not in CSS and "in-word" not in APP_JS


# ---------------------------------------------------------------- Google Docs shell
GDOCS_DIR = Path(__file__).resolve().parent.parent / "gdocs"
SIDEBAR = (GDOCS_DIR / "Sidebar.html").read_text(encoding="utf-8")
CODE_GS = (GDOCS_DIR / "Code.gs").read_text(encoding="utf-8")


def test_sidebar_routes_match_the_pane_and_code_gs():
    # message type -> Apps Script function, as declared in the shell's ROUTES
    routes = dict(re.findall(r"^\s+(\w+):\s+\{ fn: '(\w+)'", SIDEBAR, re.M))
    assert routes, "no ROUTES found in Sidebar.html"
    # the shell allows exactly the requests the pane makes - no more, no fewer
    assert set(routes) == set(re.findall(r'call\("(\w+)"', HOST_JS))
    for fn in routes.values():
        assert re.search(rf"^function {fn}\(", CODE_GS, re.M), f"{fn} missing from Code.gs"


def test_sidebar_only_answers_its_own_pane():
    assert "e.origin !== SERVER_ORIGIN" in SIDEBAR and "e.source !== frame.contentWindow" in SIDEBAR
    assert "hasOwnProperty" in SIDEBAR   # a type like "constructor" must not count as a route
    # replies go to the server's origin, never "*"
    assert "SERVER_ORIGIN);" in SIDEBAR and 'postMessage(' in SIDEBAR and '"*"' not in SIDEBAR and "'*'" not in SIDEBAR
    # the URL is serialised into JS, never spliced into HTML
    assert "<?!= JSON.stringify(serverUrl) ?>" in SIDEBAR
    assert "<?= serverUrl" not in SIDEBAR and "<?!= serverUrl" not in SIDEBAR


def test_sidebar_frames_the_page_without_office_js():
    assert "'/gdocs.html'" in SIDEBAR and "index.html" not in SIDEBAR.replace("not index.html", "")


def test_menu_offers_sidebar_and_new_tab():
    assert "addItem('Open sidebar', 'showSidebar')" in CODE_GS
    assert "addItem('Open in New Tab', 'openInNewTab')" in CODE_GS
    assert re.search(r"^function openInNewTab\(", CODE_GS, re.M)
    new_tab = (GDOCS_DIR / "NewTab.html").read_text(encoding="utf-8")
    assert "<?!= JSON.stringify(serverUrl) ?>" in new_tab
    assert "window.open(URL_, '_blank', 'noopener')" in new_tab   # not handed an opener back to the Doc


def test_only_the_routed_functions_are_public_in_code_gs():
    # google.script.run can call any function without a trailing underscore, so
    # the public surface of Code.gs must be exactly the menu entry points plus
    # the four the shell routes to.
    public = set(re.findall(r"^function (\w+)\(", CODE_GS, re.M))
    helpers = {n for n in public if n.endswith("_")}
    allowed = {"onOpen", "onInstall", "showSidebar", "openInNewTab",
               "getSelectionText", "insertAtCursor", "getDocLink", "setDocLink"}
    assert public - helpers == allowed, public - helpers ^ allowed
