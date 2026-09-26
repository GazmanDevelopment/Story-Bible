"""
Headless UI test (#15): a full walkthrough of the task pane in a real
browser (Playwright), against a real running server with a fresh, seeded
temporary database - run in both "web" (plain browser tab) and "word"
(simulated Office.js host) modes.

This used to be a standalone script needing a manually-started server and
`sys.argv` (`python tests/test_ui.py web`) - it's a real pytest test now,
so it runs in the same `pytest tests/` invocation as everything else (see
CLAUDE.md and .github/workflows/ci.yml). It still needs the Playwright
browser installed once: `playwright install chromium`.

Playwright needs a real socket to point a browser at, not an ASGI test
client, so the `server` fixture below runs an actual `run_demo.py` (the
same seeding-from-samples/ demo entry point used for local dev) as a
subprocess, on a free port, against its own temporary database - fully
independent of whatever DB the rest of the pytest session is using.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SCREENSHOTS = ROOT / "screenshots"

FAKE_OFFICE = """
window.__settings = {};
window.__sel = 'Bets';
window.__inserted = [];
window.Office = { HostType: {Word: 'Word'},
  onReady(cb){ setTimeout(()=>cb({host:'Word'}),0); },
  context: { document: { settings: {
    get:k=>window.__settings[k], set:(k,v)=>{window.__settings[k]=v}, saveAsync:cb=>cb&&cb() } } } };
window.Word = { run: async fn => fn({ document: { getSelection: () => ({
  text: window.__sel, load(){}, insertText(t){ window.__inserted.push(t) } }) }, sync: async()=>{} }) };
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_healthy(base_url: str, proc: subprocess.Popen, log_path: Path, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"server exited early (code {proc.returncode}):\n{log_path.read_text(errors='replace')}"
            )
        try:
            with urllib.request.urlopen(f"{base_url}/api/health", timeout=1) as r:
                if r.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.2)
    raise RuntimeError(
        f"server did not become healthy within {timeout}s:\n{log_path.read_text(errors='replace')}"
    )


@pytest.fixture
def server(tmp_path):
    """A real run_demo.py instance (seeded from samples/*.json) on a free
    port, backed by its own temporary database - independent of whatever
    DB the rest of this pytest session has open. Log output goes to a real
    file rather than a PIPE: the app logs every request (hardening_middleware),
    and a subprocess writing to a PIPE nobody drains can deadlock once the
    OS pipe buffer fills."""
    # mkstemp(), not mktemp(): TOCTOU race between naming the file and
    # creating it (CodeQL py/insecure-temporary-file) - same reasoning as
    # every other test file's DB fixture in this repo.
    fd, db_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    port = _free_port()
    log_path = tmp_path / "server.log"
    # Explicit AUTH_MODE=none rather than inheriting the parent shell's
    # environment wholesale: a developer with STORYBIBLE_TOKEN exported
    # locally (e.g. from testing the token-auth deploy path) would
    # otherwise silently flip the spawned server into AUTH_MODE=token,
    # and every call this walkthrough makes (no X-Token header) would 401.
    env = {**os.environ, "STORYBIBLE_DB": db_path, "AUTH_MODE": "none"}
    for var in ("STORYBIBLE_TOKEN", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ALLOWED_OIDS"):
        env.pop(var, None)
    try:
        with open(log_path, "wb") as log_file:
            proc = subprocess.Popen(
                [sys.executable, str(ROOT / "run_demo.py"), "--port", str(port)],
                cwd=ROOT, env=env, stdout=log_file, stderr=subprocess.STDOUT,
            )
            try:
                _wait_until_healthy(f"http://127.0.0.1:{port}", proc, log_path)
                yield f"http://127.0.0.1:{port}"
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(db_path + suffix).unlink(missing_ok=True)


@pytest.mark.parametrize("word", [False, True], ids=["web", "word"])
def test_task_pane_walkthrough(server, word):
    SCREENSHOTS.mkdir(exist_ok=True)
    tag = "word" if word else "web"
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 360, "height": 780})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: m.type == "error" and errors.append(m.text))

            def office(route):
                # Matches the real CDN URL too - office.js is never actually
                # fetched over the network in either mode, just faked here
                # ("word") or fulfilled empty so window.Office stays
                # undefined ("web", matching a plain browser tab).
                route.fulfill(body=FAKE_OFFICE if word else "", content_type="application/javascript")

            pg.route("**/office.js", office)

            pg.goto(server)
            pg.wait_for_selector(".list li")
            pg.screenshot(path=str(SCREENSHOTS / f"{tag}_1_chars.png"), full_page=True)
            pg.click("text=Betsy Marr")
            pg.wait_for_selector("form[data-kind=characters]")
            pg.screenshot(path=str(SCREENSHOTS / f"{tag}_2_char.png"), full_page=True)

            # add relationship
            pg.fill("#relType", "confides in")
            pg.select_option("#relTo", label="Kristy Dunn")
            pg.click("[data-act=add-rel]")
            pg.wait_for_selector("text=Betsy Marr confides in Kristy Dunn")

            # edit + save a custom field
            pg.fill("[data-custom='Skin']", "Pale, freckled shoulders")
            pg.click("[data-act=save]")
            pg.wait_for_timeout(300)
            assert pg.input_value("[data-custom='Skin']") == "Pale, freckled shoulders"
            pg.click("[data-act=cancel] >> nth=0")
            pg.click("#tabs >> text=Timeline")
            pg.wait_for_selector(".tl li")
            txt = pg.inner_text("main")
            assert "14 Feb 2019" in txt and "14 Feb 2024" in txt, txt
            assert "Mark Hale 39" in txt, txt   # 34 + 5 years
            assert "Mark Hale 27" in txt, txt   # -7 years wedding
            pg.screenshot(path=str(SCREENSHOTS / f"{tag}_3_timeline.png"), full_page=True)

            # new event with preview
            pg.click("[data-act=new]")
            pg.fill("[data-f=title]", "Test event")
            pg.fill("[data-f=off_y]", "1")
            pg.fill("[data-f=off_m]", "0")
            pg.fill("[data-f=off_d]", "15")
            prev = pg.inner_text("#whenPreview")
            assert "29 Feb 2020" in prev, prev
            pg.click(".chip >> text=Sally Hale")
            pg.click("[data-act=save]")
            pg.wait_for_selector("text=Test event")

            # filter by chapter
            pg.select_option("[data-act=tl-chapter]", label="Ch 2")
            n = pg.locator(".tl li").count()
            assert n == 2, n

            # places + series tabs
            pg.click("#tabs >> text=Places")
            pg.wait_for_selector(".list >> text=The Lake House")
            pg.click("#tabs >> text=Series")
            pg.wait_for_selector("text=Chapters (one Word doc each)")
            pg.screenshot(path=str(SCREENSHOTS / f"{tag}_4_series.png"), full_page=True)

            # switch series
            pg.select_option("#seriesSelect", label="Series B – After Hours")
            pg.click("#tabs >> text=Timeline")
            pg.wait_for_selector("text=Night one")

            if word:
                pg.click("[data-act=doc-link]")
                pg.wait_for_selector("[data-act=doc-chapter]")
                pg.select_option("[data-act=doc-chapter]", label="Ch 1 – Check-in")
                s = pg.evaluate("window.__settings")
                assert s["storybible"]["chapter_id"], s
                pg.select_option("#seriesSelect", label="Series A – The Lake House")
                pg.click("#btnFind")
                pg.wait_for_selector("h2:text('Betsy Marr')")
                pg.click("text=Insert name at cursor")
                assert pg.evaluate("window.__inserted") == ["Betsy Marr"]
                pg.screenshot(path=str(SCREENSHOTS / f"{tag}_5_find.png"), full_page=True)
        finally:
            browser.close()

    assert not errors, errors
