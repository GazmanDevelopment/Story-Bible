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
