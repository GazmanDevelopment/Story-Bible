"""
Regenerate the screenshots used by the help guide (#81, #102).

    python scripts/make_help_screenshots.py

Starts run_demo.py (fictional sample series from samples/) against a
throwaway database, drives it with Playwright at task-pane width, and writes
PNGs to docs/help/img/. Needs `playwright install chromium` and Pillow.
Run on demand and commit the result - it is not part of CI. The sign-in
screen is not captured (it needs a real Microsoft login).
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageDraw
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
VIEW_H = 1500
OUT = ROOT / "docs" / "help" / "img"
sys.path.insert(0, str(ROOT / "tests"))
from test_ui import FAKE_OFFICE, _gdocs_page  # noqa: E402  (same Office stub and fake Apps Script the UI tests use)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sample_image(path: Path) -> None:
    """A plain illustration so no real photo ends up in the repo."""
    img = Image.new("RGB", (900, 500), "#d9c9a8")
    d = ImageDraw.Draw(img)
    d.rectangle((120, 260, 780, 440), fill="#8a2f4f")
    d.polygon([(100, 260), (450, 90), (800, 260)], fill="#2e2a24")
    d.rectangle((400, 330, 500, 440), fill="#f6f1e7")
    img.save(path)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    fd, db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    # A fixed build stamp (#144) so regenerating the screenshots never changes them just because the date did.
    env = {**os.environ, "STORYBIBLE_DB": db, "AUTH_MODE": "none",
           "BUILD_VERSION": "v0.1.0", "BUILD_DATE": "2026-01-01"}
    for var in ("STORYBIBLE_TOKEN", "ENTRA_TENANT_ID", "ENTRA_CLIENT_ID", "ALLOWED_OIDS"):
        env.pop(var, None)
    log = tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, str(ROOT / "run_demo.py"), "--port", str(port)],
                            cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    img_path = Path(tempfile.gettempdir()) / "help_sample.png"
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f"{base}/api/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("demo server did not start")
        _sample_image(img_path)
        shoot(base, img_path)
        shoot_gdocs(base)
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        log.close()
        for suffix in ("", "-wal", "-shm"):
            Path(db + suffix).unlink(missing_ok=True)
    print("wrote", len(list(OUT.glob("*.png"))), "screenshots to", OUT)


def shoot(base: str, img_path: Path) -> None:
    def snap(pg, name):
        pg.wait_for_timeout(250)
        # Tall viewport + clip to the content, not full_page: a full-page
        # capture drops the sticky header/form bar into the middle of the shot.
        h = pg.evaluate("Math.ceil(document.querySelector('#main').getBoundingClientRect().bottom + window.scrollY) + 12")
        pg.evaluate("window.scrollTo(0, 0)")
        pg.screenshot(path=str(OUT / f"{name}.png"), clip={"x": 0, "y": 0, "width": 360, "height": min(h, VIEW_H)})

    def cancel(pg):
        pg.click("[data-act=cancel] >> nth=0")
        pg.wait_for_timeout(150)
        if pg.locator("#modalOverlay:not([hidden])").count():  # unsaved-changes guard
            pg.click("[data-act=modal-confirm]")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            def page(word: bool):
                pg = browser.new_page(viewport={"width": 360, "height": VIEW_H})
                pg.route("**/office.js", lambda r: r.fulfill(
                    body=FAKE_OFFICE if word else "", content_type="application/javascript"))
                pg.goto(base)
                pg.wait_for_selector(".list li")
                return pg

            # Plain browser tab: Word-only controls are hidden.
            pg = page(False)
            snap(pg, "browser-characters")
            pg.close()

            pg = page(True)
            snap(pg, "characters")

            pg.click("text=Betsy Marr")
            pg.wait_for_selector("form[data-kind=characters]")
            snap(pg, "character")
            pg.fill("#relType", "confides in")
            pg.select_option("#relTo", label="Kristy Dunn")
            pg.click("[data-act=add-rel]")
            pg.wait_for_selector("text=Betsy Marr confides in Kristy Dunn")
            pg.locator("#relType").scroll_into_view_if_needed()
            snap(pg, "relationship")
            cancel(pg)

            pg.click("#tabs >> text=Places")
            pg.click(".list >> text=The Lake House")
            pg.wait_for_selector("form[data-kind=locations]")
            snap(pg, "place")
            cancel(pg)

            pg.click("#tabs >> text=Timeline")
            pg.wait_for_selector(".tl li")
            snap(pg, "timeline")
            pg.click("[data-act=new]")
            pg.wait_for_selector("form[data-kind=events]")
            pg.fill("[data-f=title]", "The reunion dinner")
            pg.fill("[data-f=off_y]", "2")
            pg.fill("[data-f=off_m]", "1")
            pg.fill("[data-f=off_d]", "0")
            pg.click(".chip >> text=Sally Hale")
            snap(pg, "event")
            cancel(pg)

            pg.click("#tabs >> text=Research")
            pg.click("[data-act=new]")
            pg.wait_for_selector("form[data-kind=research]")
            pg.fill("[data-f=title]", "Lake house floor plan")
            pg.click("#researchEditor .ql-editor")
            pg.keyboard.type("Layout of the ground floor, from the estate agent's brochure.")
            with pg.expect_file_chooser() as fc:
                pg.click(".ql-image")
            fc.value.set_files(str(img_path))
            pg.wait_for_selector("#researchEditor img")
            pg.click(".chip >> text=Betsy Marr")
            snap(pg, "research")
            cancel(pg)

            pg.click("#tabs >> text=Series")
            pg.wait_for_selector("text=Chapters (one document each)")
            snap(pg, "series")
            pg.click("[data-act=log-issue]")
            pg.wait_for_selector("form[data-kind=feedback]")
            pg.fill("[data-f=title]", "Example: button does nothing")
            pg.fill("[data-f=description]", "What happened, and what you expected.")
            snap(pg, "log-issue")

            # Account view (#86). The demo runs without sign-in, so fake a
            # signed-in person client-side; nothing is sent to the server.
            pg.evaluate("""() => {
              S.config = {...S.config, authMode: 'entra',
                          privacyUrl: 'https://example.com/privacy', termsUrl: 'https://example.com/terms'};
              msalAccount = {name: 'Alex Morgan', username: 'alex.morgan@example.com'};
              S.me = {oid: 'x', email: 'alex.morgan@example.com', displayName: 'Alex Morgan', isAdmin: false};
              renderHeader();
            }""")
            snap(pg, "signed-in")

            # Sharing panel (#148), also faked client-side: entra mode plus
            # a plausible member list, nothing sent to the server. 'local'
            # matches the demo series' real none-mode owner oid, so the
            # owner's (not a member's) view of the panel renders.
            pg.evaluate("""async () => {
              S.me = {...S.me, oid: 'local'};
              S.members = {owner_oid: 'local', owner_display_name: 'Alex Morgan', owner_email: 'alex.morgan@example.com',
                members: [{oid: 'friend-oid', role: 'viewer', display_name: 'Jamie Patel', email: 'jamie.patel@example.com'}]};
              S.tab = 'series'; S.view = null; render();
            }""")
            pg.wait_for_selector("text=Sharing")
            snap(pg, "sharing")

            pg.click("[data-act=open-account]")
            pg.wait_for_selector("#deleteConfirm")
            snap(pg, "account")
            pg.fill("#deleteConfirm", "alex.morgan@example.com")
            pg.locator("[data-act=delete-account]").scroll_into_view_if_needed()
            snap(pg, "delete-account")

            # First-sign-in acceptance screen (#111), also faked client-side.
            pg.evaluate("""() => {
              S.config = {...S.config, policyVersion: '1.0',
                          privacyUrl: 'https://example.com/privacy', termsUrl: 'https://example.com/terms'};
              S.view = null;
              S.me = {...S.me, policyCurrent: false};
              render();
            }""")
            pg.wait_for_selector("[data-act=accept-policy]")
            snap(pg, "policy-gate")
            pg.close()
        finally:
            browser.close()


def shoot_gdocs(base: str) -> None:
    """The pane inside the real Google Docs sidebar shell (gdocs/Sidebar.html),
    with only Apps Script faked, as the UI tests do. Runs after shoot() because
    Playwright's sync API can't be nested."""
    name = "gdocs-sidebar"
    with _gdocs_page(base) as (pg, fr, _errors):
        fr.locator(".list li").first.wait_for()
        fr.locator("[data-act=doc-link]").click()
        fr.locator("[data-act=doc-chapter]").wait_for()
        values = fr.locator("[data-act=doc-chapter] option").evaluate_all("els => els.map(e => e.value)")
        fr.locator("[data-act=doc-chapter]").select_option(next(v for v in values if v))
        pg.wait_for_function("(window.__props.storybible || '').includes('chapter_id')")
        pg.wait_for_timeout(250)
        # Same clip-to-content as snap() above; the frame fills the page from the top left.
        h = fr.locator("#main").evaluate("el => Math.ceil(el.getBoundingClientRect().bottom + window.scrollY) + 12")
        pg.screenshot(path=str(OUT / f"{name}.png"), clip={"x": 0, "y": 0, "width": 360, "height": min(h, 780)})


if __name__ == "__main__":
    main()
