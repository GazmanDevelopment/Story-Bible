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
    file rather than a PIPE: the app logs every request (HardeningMiddleware),
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


@pytest.fixture
def browser_page():
    """A single browser page for tests that don't need the full web/word
    walkthrough - just one plain-browser-tab session against `server`."""
    errors: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            pg = browser.new_page(viewport={"width": 360, "height": 780})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
            pg.route("**/office.js", lambda route: route.fulfill(body="", content_type="application/javascript"))
            yield pg, errors
        finally:
            browser.close()


def test_timeline_pill_filters_grey_out_not_hide(server, browser_page):
    """#97: character/place pills multi-select and grey out (never hide)
    non-matching timeline entries. Characters AND together, places OR."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.select_option("#seriesSelect", label="Series A – The Lake House")
    pg.click("#tabs >> text=Timeline")
    pg.wait_for_selector(".tl li")
    total = pg.locator(".tl li").count()
    assert total == 5, total
    assert pg.locator(".tl li.dim").count() == 0

    def pill(name):
        pg.click(f"[data-act=tl-chip]:text-is({name!r})")

    def dimmed():
        # every entry is still listed, whatever the filter
        assert pg.locator(".tl li").count() == total
        return pg.locator(".tl li.dim").count()

    pill("Mark Hale")
    assert dimmed() == 1                      # only the Kristy-only event
    pill("Betsy Marr")
    assert dimmed() == 2                      # AND: needs Mark and Betsy
    assert "3 of 5 match" in pg.inner_text(".tl-count")
    pill("The Lake House")
    assert dimmed() == 3                      # ...and at the lake
    pill("Kristy's flat")
    assert dimmed() == 2                      # places OR together
    pill("Kristy's flat")
    pill("Betsy Marr")                        # toggling a pill off again
    assert dimmed() == 3                      # Mark at the lake: only e2 and e4 match
    pg.click("[data-act=tl-clear]")
    assert dimmed() == 0
    assert pg.locator(".tl-count").count() == 0

    # a greyed-out entry is still a normal, openable entry
    pill("Kristy Dunn")
    pg.locator(".tl li.dim").first.click()
    pg.wait_for_selector("form[data-kind=events]")
    pg.click("[data-act=cancel] >> nth=0")
    pg.wait_for_selector(".tl li")

    # pills belong to one series: switching drops them
    assert pg.locator("[data-act=tl-chip].on").count() == 1
    pg.select_option("#seriesSelect", label="Series B – After Hours")
    pg.wait_for_selector("text=Night one")
    assert pg.locator("[data-act=tl-chip].on").count() == 0
    assert pg.locator(".tl li.dim").count() == 0
    assert not errors, errors


def test_save_conflict_offers_reload_or_keep_mine(server, browser_page):
    """#58: a 409 (someone else saved first) must show a real reload/keep-
    mine choice, never overwrite silently, and never leave the user's own
    edit stuck in a modal-shaped hole."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")

    # Simulate another client saving first (bumping the version server-side)
    # via a direct fetch from within the page - no second browser needed to
    # create a real race.
    pg.evaluate("""
        async () => {
            const id = S.view.id;
            const current = rec('characters', id);
            await fetch(`/api/series/${S.sid}/characters/${id}`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ data: { ...current, role: 'Changed elsewhere' }, version: current.version }),
            });
        }
    """)

    pg.fill("[data-f=role]", "My local edit")
    pg.click("[data-act=save]")
    pg.wait_for_selector("#modalOverlay:not([hidden])")
    assert "Changed by" in pg.inner_text("#modalMessage")

    # Keep mine: modal closes, the edit is untouched, nothing was overwritten.
    pg.click("[data-act=modal-cancel]")
    pg.wait_for_selector("#modalOverlay", state="hidden")
    assert pg.input_value("[data-f=role]") == "My local edit"

    # Saving again with the same stale version conflicts again - there's no
    # back door that quietly force-overwrites on a second try.
    pg.click("[data-act=save]")
    pg.wait_for_selector("#modalOverlay:not([hidden])")
    pg.click("[data-act=modal-confirm]")  # Reload this time
    pg.wait_for_selector("#modalOverlay", state="hidden")
    assert pg.input_value("[data-f=role]") == "Changed elsewhere"

    # Chromium logs its own "Failed to load resource: ...409" console error
    # for each conflict response above - that's expected noise from this
    # test's own setup, not a bug, so it's the one thing we filter out here.
    unexpected = [e for e in errors if "409" not in e]
    assert not unexpected, unexpected


def test_unsaved_changes_guard_on_cancel_and_tab_switch(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")

    # No edit made - Back navigates immediately, no prompt.
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")
    pg.click("[data-act=cancel]")
    pg.wait_for_selector(".list li")

    # Edit, then try to leave via Back - guarded.
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")
    pg.fill("[data-f=role]", "Unsaved edit")
    pg.click("[data-act=cancel]")
    pg.wait_for_selector("#modalOverlay:not([hidden])")

    # Cancel the prompt itself - stay put, edit still there.
    pg.click("[data-act=modal-cancel]")
    pg.wait_for_selector("#modalOverlay", state="hidden")
    assert pg.is_visible("form[data-kind=characters]")
    assert pg.input_value("[data-f=role]") == "Unsaved edit"

    # Same guard on switching tabs, this time actually discarding.
    pg.click("#tabs >> text=Places")
    pg.wait_for_selector("#modalOverlay:not([hidden])")
    pg.click("[data-act=modal-confirm]")
    pg.wait_for_selector(".list li")
    assert "The Lake House" in pg.inner_text("main")


def test_unsaved_changes_guard_covers_series_settings_tab(server, browser_page):
    """#58 follow-up: the Series tab is a top-level tab (S.view stays null
    there), not a record "view" like Characters/Places/etc - a first pass at
    the guard only snapshotted forms rendered while S.view was set, so an
    edit to the series name and a tab switch away silently lost it."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")

    pg.click("#tabs >> text=Series")
    pg.wait_for_selector("form[data-kind=series]")
    pg.fill("[data-f=name]", "Renamed series")
    pg.click("#tabs >> text=Characters")
    pg.wait_for_selector("#modalOverlay:not([hidden])")

    # Cancel the prompt - stay on Series, edit still there.
    pg.click("[data-act=modal-cancel]")
    pg.wait_for_selector("#modalOverlay", state="hidden")
    assert pg.is_visible("form[data-kind=series]")
    assert pg.input_value("[data-f=name]") == "Renamed series"

    # Confirm this time - discards the edit and switches tabs.
    pg.click("#tabs >> text=Characters")
    pg.wait_for_selector("#modalOverlay:not([hidden])")
    pg.click("[data-act=modal-confirm]")
    pg.wait_for_selector(".list li")

    assert not errors, errors


def test_relationship_type_suggestions_are_editable_per_series(server, browser_page):
    """#63: the relationship type field was always free text (nothing
    stopped typing "enemy of" before this) - what was missing was a way to
    grow the *suggested* list itself, the same way character_fields already
    let you customize the per-series default character fields."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")

    pg.click("#tabs >> text=Series")
    pg.wait_for_selector("form[data-kind=series]")
    pg.fill("[data-f=relationship_types]", "enemy of\nrival of")
    pg.click("[data-act=save]")
    pg.wait_for_timeout(300)

    pg.click("#tabs >> text=Characters")
    pg.wait_for_selector(".list li")
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")

    options = pg.eval_on_selector_all("#relTypes option", "els => els.map(e => e.value)")
    assert options == ["enemy of", "rival of"]

    assert not errors, errors


# ------------------------------------------------------------------ #72
def _record_api(pg):
    """Every /api call the page makes from now on: (method, path, status, sent If-None-Match)."""
    calls = []
    pg.on("response", lambda r: "/api/" in r.url and calls.append((
        r.request.method, r.url.split("/api", 1)[1], r.status, "if-none-match" in r.request.headers)))
    return calls


def test_saving_does_not_reload_the_whole_bundle(server, browser_page):
    """#72: a save/add used to be followed by a full bundle download (and, for
    the series form, the full series list too). The response to the save is
    the record, so the pane patches its own state from it."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")
    calls = _record_api(pg)

    pg.fill("[data-custom='Skin']", "Freckled")
    pg.click("[data-act=save]")
    pg.wait_for_selector("#toast.show")
    assert [(m, p.split("/")[-2]) for m, p, *_ in calls] == [("PUT", "characters")], calls
    assert pg.input_value("[data-custom='Skin']") == "Freckled"        # form re-rendered from the response
    assert pg.evaluate("rec('characters', S.view.id).version") >= 2     # and local state has the new version

    calls.clear()
    pg.fill("#relType", "confides in")
    pg.select_option("#relTo", label="Kristy Dunn")
    pg.click("[data-act=add-rel]")
    pg.wait_for_selector("text=Betsy Marr confides in Kristy Dunn")
    assert [m for m, *_ in calls] == ["POST"], calls

    calls.clear()
    pg.click(".rel:has-text('confides in') [data-act=del-rel]")   # the one just added, not an existing relationship
    pg.wait_for_selector("text=Betsy Marr confides in Kristy Dunn", state="detached")
    assert [m for m, *_ in calls] == ["DELETE"], calls   # nothing references a relationship: no reload needed

    # A saved edit can be saved again straight away (the patched version is current, so no 409).
    pg.fill("[data-custom='Skin']", "Freckled and pale")
    pg.click("[data-act=save]")
    pg.wait_for_function("() => rec('characters', S.view.id).custom.Skin === 'Freckled and pale'")
    assert not [e for e in errors if "409" in e], errors


def test_a_new_record_appears_in_its_list_without_a_reload(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("#tabs >> text=Timeline")
    pg.wait_for_selector(".tl li")
    pg.wait_for_timeout(300)   # let the tab-switch revalidation settle before counting
    calls = _record_api(pg)
    pg.click("[data-act=new]")
    pg.fill("[data-f=title]", "Patched-in event")
    pg.click("[data-act=save]")
    pg.wait_for_selector("text=Patched-in event")
    assert [m for m, *_ in calls] == ["POST"], calls
    assert not errors, errors


def test_saving_series_settings_updates_the_picker_without_reloading_lists(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("#tabs >> text=Series")
    pg.wait_for_selector("form[data-kind=series]")
    calls = _record_api(pg)
    pg.fill("[data-f=name]", "Zed - renamed")
    pg.click("[data-act=save]")
    pg.wait_for_function("() => document.querySelector('#seriesSelect').selectedOptions[0].textContent.includes('Zed')")
    assert [m for m, *_ in calls] == ["PUT"], calls
    # the picker is re-sorted locally: "Zed..." now sorts after the other series
    labels = pg.eval_on_selector_all("#seriesSelect option", "os => os.map(o => o.textContent)")
    named = [label for label in labels if not label.startswith("+")]
    assert named == sorted(named, key=str.lower), named
    assert not errors, errors


def test_deleting_a_record_still_reloads_because_the_server_cascades(server, browser_page):
    """Deleting a character also strips it from every place/event that
    referenced it - server-side. The pane must pick that up (so: reload)."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("#tabs >> text=Places")
    pg.wait_for_selector(".list >> text=The Lake House")
    pg.click(".list >> text=The Lake House")
    pg.wait_for_selector("form[data-kind=locations]")
    chip = pg.locator(".chip", has_text="Betsy Marr")
    if "on" not in chip.get_attribute("class").split():   # the demo data may already have her here
        chip.click()
    pg.click("[data-act=save]")
    pg.wait_for_selector("#toast.show")
    pg.click("[data-act=cancel] >> nth=0")
    pg.wait_for_selector(".list li")
    assert "Betsy Marr" in pg.inner_text("main")

    pg.click("#tabs >> text=Characters")
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")
    pg.wait_for_timeout(300)
    calls = _record_api(pg)
    pg.click("[data-act=delete]")
    pg.click("[data-act=delete]")   # armed: the second click confirms
    pg.wait_for_selector(".list li")
    assert [m for m, *_ in calls] == ["DELETE", "GET"], calls
    pg.click("#tabs >> text=Places")
    pg.wait_for_selector(".list >> text=The Lake House")
    assert "Betsy Marr" not in pg.inner_text("main") and "?" not in pg.inner_text("main")
    assert not errors, errors


def test_an_unchanged_series_revalidates_with_a_304(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    assert pg.evaluate("!!S.bEtag")   # the initial bundle load recorded its ETag
    calls = _record_api(pg)
    pg.click("#tabs >> text=Places")
    pg.wait_for_selector(".list li")
    pg.wait_for_timeout(400)
    gets = [c for c in calls if c[0] == "GET"]
    assert gets and gets[0][2:] == (304, True), calls
    assert not errors, errors


def test_other_peoples_changes_show_up_on_the_next_tab_switch(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.evaluate("""async () => {
        await fetch(`/api/series/${S.sid}/characters`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ data: { name: 'Added By Someone Else' } }) });
    }""")
    assert "Added By Someone Else" not in pg.inner_text("main")   # not pushed; the pane hasn't looked yet
    pg.click("#tabs >> text=Places")
    pg.wait_for_selector(".list li")
    pg.click("#tabs >> text=Characters")
    pg.wait_for_selector("text=Added By Someone Else")
    assert not errors, errors


def test_background_refresh_never_swaps_data_under_an_open_form(server, browser_page):
    """The safety property: a form holds the version it was opened at, and a
    save from it must 409 if someone else changed the record meanwhile. If a
    background refresh replaced the data under the form, the save would send
    the new version with stale field values and silently overwrite them."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")
    pg.evaluate("""async () => {
        const c = rec('characters', S.view.id);
        await fetch(`/api/series/${S.sid}/characters/${c.id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ data: { ...c, role: 'Changed elsewhere' }, version: c.version }) });
    }""")
    pg.fill("[data-f=role]", "My unsaved edit")
    calls = _record_api(pg)
    pg.evaluate("lastRevalidate = 0; revalidateBundle()")
    pg.wait_for_timeout(400)
    assert not [c for c in calls if c[0] == "GET"], calls          # didn't even ask
    assert pg.input_value("[data-f=role]") == "My unsaved edit"
    pg.click("[data-act=save]")
    pg.wait_for_selector("#modalOverlay:not([hidden])")            # the conflict is still caught
    assert "Changed by" in pg.inner_text("#modalMessage")
    unexpected = [e for e in errors if "409" not in e]
    assert not unexpected, unexpected


def test_background_refresh_skips_the_series_settings_form_too(server, browser_page):
    """The Series tab is a form with S.view null - it must be protected as well."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("#tabs >> text=Series")
    pg.wait_for_selector("form[data-kind=series]")
    pg.wait_for_timeout(300)
    pg.fill("[data-f=name]", "Half-typed name")
    calls = _record_api(pg)
    pg.evaluate("lastRevalidate = 0; revalidateBundle()")
    pg.wait_for_timeout(400)
    assert not calls, calls
    assert pg.input_value("[data-f=name]") == "Half-typed name"
    assert not errors, errors


def test_a_refresh_that_lands_after_a_form_opened_is_discarded(server, browser_page):
    """The race: a background refresh is already in flight when the user opens
    a record. When the answer arrives it must NOT replace the data under the
    now-open form - otherwise Save would send the refreshed version with the
    stale field values and silently overwrite the other person's edit."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    original = pg.evaluate("rec('characters', S.b.characters.find(c => c.name === 'Betsy Marr').id).version")
    pg.evaluate("""async () => {
        const c = S.b.characters.find(x => x.name === 'Betsy Marr');
        await fetch(`/api/series/${S.sid}/characters/${c.id}`, {
            method: 'PUT', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ data: { ...c, role: 'Changed elsewhere' }, version: c.version }) });
    }""")
    held = []
    pg.route("**/api/series/*/bundle", lambda route: held.append(route))
    pg.evaluate("() => { lastRevalidate = 0; revalidateBundle(); }")   # fire and forget: the response is held
    for _ in range(50):
        if held:
            break
        pg.wait_for_timeout(100)
    assert held, "the background refresh never went out"

    pg.click("text=Betsy Marr")                      # the form opens from the OLD data while the refresh is in flight
    pg.wait_for_selector("form[data-kind=characters]")
    pg.fill("[data-f=role]", "My edit")
    held[0].continue_()                              # ...now the (newer) answer arrives
    pg.wait_for_timeout(500)

    assert pg.evaluate("rec('characters', S.view.id).version") == original     # not swapped in
    assert pg.input_value("[data-f=role]") == "My edit"
    pg.unroute("**/api/series/*/bundle")
    pg.click("[data-act=save]")
    pg.wait_for_selector("#modalOverlay:not([hidden])")                        # so the conflict is still caught
    assert "Changed by" in pg.inner_text("#modalMessage")
    unexpected = [e for e in errors if "409" not in e]
    assert not unexpected, unexpected


def test_leaving_a_form_looks_for_other_peoples_changes(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("text=Betsy Marr")
    pg.wait_for_selector("form[data-kind=characters]")
    pg.evaluate("""async () => {
        await fetch(`/api/series/${S.sid}/characters`, {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ data: { name: 'Added While You Were Editing' } }) });
        lastRevalidate = 0;
    }""")
    pg.click("[data-act=cancel]")
    pg.wait_for_selector("text=Added While You Were Editing")
    assert not errors, errors


# ------------------------------------------------------------------ #73
def test_feedback_form_warns_that_it_is_posted_publicly(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.click("#tabs >> text=Series")
    pg.wait_for_selector("form[data-kind=series]")
    pg.click("[data-act=log-issue]")
    pg.wait_for_selector("form[data-kind=feedback]")
    notice = pg.inner_text("[data-role=public-notice]")
    assert "public GitHub issue" in notice and "anyone can read it" in notice
    assert "story text" in notice
    assert "display name" not in notice     # no real identity outside Entra mode: nothing is added
    assert pg.is_visible("[data-role=public-notice]")
    assert not errors, errors


def test_feedback_notice_mentions_the_display_name_in_entra_mode(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    html = pg.evaluate("""() => { S.config = { ...S.config, authMode: "entra" }; return feedbackForm("suggestion"); }""")
    assert "public GitHub issue" in html and "Your display name is added to it." in html
    assert not errors, errors


def test_admin_button_and_user_list_only_for_admins(server, browser_page):
    """#82. Entra mode can't run in this suite, so this drives the renderers
    with state set by hand; the server's own 404 for non-admins is covered in
    tests/test_admin_users.py."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    bar = lambda: pg.evaluate("""() => { renderHeader(); return $("#authBar").innerHTML; }""")
    pg.evaluate("""() => { S.config = { ...S.config, authMode: "entra" };
        msalAccount = { name: "Me" }; S.me = { oid: "me", isAdmin: false }; }""")
    assert "Admin" not in bar()
    pg.evaluate("""() => { S.me = { oid: "me", isAdmin: true }; }""")
    assert 'data-act="open-admin"' in bar()
    pg.evaluate("""() => { S.adminUsers = { total: 2, users: [
        { oid: "me", display_name: "Me", email: "me@x.com", first_seen: 1, last_seen: 2, series_owned: 1,
          series_shared: 0, records: { characters: 2, events: 0 }, bytes_used: 2048, blocked: false },
        { oid: "u2", display_name: "<img src=x onerror=alert(1)>", email: "b@x.com", first_seen: 1, last_seen: 2,
          series_owned: 0, series_shared: 1, records: {}, bytes_used: 0, blocked: true, blocked_reason: "spam" }] };
        S.view = { kind: "admin", id: null }; render(); }""")
    html = pg.inner_html("#main")
    assert "Users (2)" in html and "2 characters" in html and "(blocked: spam)" in html
    assert "<img" not in html                                   # names are escaped
    assert pg.locator("[data-act=admin-block]").count() == 0    # you can't block yourself; u2 is already blocked
    assert pg.locator("[data-act=admin-unblock]").count() == 1
    assert not errors, errors


def test_admin_view_buttons_call_the_api_and_reload(server, browser_page):
    """#82: drives the real click handlers with api() and the confirm modal stubbed."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.evaluate("""() => {
        window.calls = [];
        const users = [{ oid: "a/b", display_name: "Ann", email: "a@x.com", first_seen: 1, last_seen: 2, series_owned: 0,
                         series_shared: 0, records: {}, bytes_used: 0, blocked: false }];
        window.api = async (path, method = "GET", body) => {
            window.calls.push([method, path, body]);
            if (path === "/admin/users") return { total: 1, users };
            return {};
        };
        window.showModal = async () => true;
        S.config = { ...S.config, authMode: "entra" }; msalAccount = { name: "Me" }; S.me = { oid: "me", isAdmin: true };
        renderHeader();
    }""")
    pg.click("[data-act=open-admin]")
    pg.wait_for_selector("[data-act=admin-block]")
    pg.click("[data-act=admin-block]")
    pg.wait_for_function("window.calls.some(c => c[0] === 'PUT')")
    calls = pg.evaluate("window.calls")
    assert ["PUT", "/admin/users/a%2Fb/block", {"reason": ""}] in calls     # oid is URL-encoded
    assert calls[-1][1] == "/admin/users"                                   # list reloaded afterwards
    assert not errors, errors


def test_admin_view_shows_an_error_instead_of_loading_forever(server, browser_page):
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.evaluate("""() => {
        window.api = async () => { throw new Error("boom"); };
        S.config = { ...S.config, authMode: "entra" }; msalAccount = { name: "Me" }; S.me = { oid: "me", isAdmin: true };
        renderHeader();
    }""")
    pg.click("[data-act=open-admin]")
    pg.wait_for_selector("text=Couldn't load the user list: boom")
    assert pg.locator("[data-act=open-admin]").count() >= 1                 # "Try again"
    assert not errors, errors


def test_delete_account_needs_the_typed_email_then_signs_out(server, browser_page):
    """#86: drives the Account view with api() stubbed; the server side is in
    tests/test_delete_account.py."""
    pg, errors = browser_page
    pg.goto(server)
    pg.wait_for_selector(".list li")
    pg.evaluate("""() => {
        window.calls = [];
        window.api = async (path, method = "GET", body) => { window.calls.push([method, path, body]); return {}; };
        S.config = { ...S.config, authMode: "entra" };
        msalAccount = { name: "Me" }; S.me = { oid: "me", email: "Me@X.com", isAdmin: false };
        renderHeader();
    }""")
    pg.click("[data-act=open-account]")
    pg.wait_for_selector("#deleteConfirm")
    pg.click("[data-act=delete-account]")                                   # nothing typed
    pg.fill("#deleteConfirm", "someone@else.com")
    pg.click("[data-act=delete-account]")                                   # wrong email
    assert pg.evaluate("window.calls") == []
    pg.fill("#deleteConfirm", "me@x.com")                                   # case-insensitive
    pg.click("[data-act=delete-account]")
    pg.wait_for_function("window.calls.length === 1")
    assert pg.evaluate("window.calls") == [["DELETE", "/me", {"confirm": "me@x.com"}]]
    pg.wait_for_function("S.me === null && S.view === null")
    assert not errors, errors


ENTRA_CONFIG = {"authMode": "entra", "tenantId": "t-home", "clientId": "cid",
                "authority": "https://login.microsoftonline.com/common",
                "privacyUrl": "https://example.com/privacy", "termsUrl": "https://example.com/terms"}


def _boot_with_config(pg, server, cfg):
    """Serve `cfg` as /api/config, stub MSAL (no network) and re-run boot();
    returns the config object MSAL was created with."""
    pg.route("**/api/config", lambda route: route.fulfill(json=cfg))
    pg.goto(server)
    pg.wait_for_function("typeof initAuth === 'function' && typeof msal !== 'undefined'")
    return pg.evaluate("""async () => {
        let seen = null;
        msal.createNestablePublicClientApplication = async (c) => { seen = c; return { getAllAccounts: () => [] }; };
        msal.PublicClientApplication = function (c) { seen = c; this.initialize = async () => {}; this.getAllAccounts = () => []; };
        await boot();
        return { msalConfig: seen, main: document.querySelector("#main").innerHTML };
    }""")


def test_msal_uses_the_authority_from_config(server, browser_page):
    """#91: the pane no longer builds its own tenant authority string."""
    pg, errors = browser_page
    out = _boot_with_config(pg, server, ENTRA_CONFIG)
    assert out["msalConfig"]["auth"]["authority"] == "https://login.microsoftonline.com/common"
    assert out["msalConfig"]["auth"]["clientId"] == "cid"
    assert not errors, errors


def test_msal_falls_back_to_the_tenant_authority_for_an_older_server(server, browser_page):
    pg, errors = browser_page
    cfg = {k: v for k, v in ENTRA_CONFIG.items() if k not in ("authority", "privacyUrl", "termsUrl")}
    out = _boot_with_config(pg, server, cfg)
    assert out["msalConfig"]["auth"]["authority"] == "https://login.microsoftonline.com/t-home"
    assert "legal-links" not in out["main"]
    assert not errors, errors


def test_signed_out_pane_shows_privacy_and_terms_links(server, browser_page):
    """#100/#91: visible before anyone's first sign-in; https only, escaped."""
    pg, errors = browser_page
    out = _boot_with_config(pg, server, ENTRA_CONFIG)
    assert "Sign in to continue" in out["main"]
    assert 'href="https://example.com/privacy"' in out["main"] and 'href="https://example.com/terms"' in out["main"]
    assert 'rel="noopener noreferrer"' in out["main"]
    bad = dict(ENTRA_CONFIG, privacyUrl="javascript:alert(1)", termsUrl="")
    pg.unroute("**/api/config")
    out = _boot_with_config(pg, server, bad)
    assert "legal-links" not in out["main"] and "javascript:" not in out["main"]
    assert not errors, errors


def test_sign_in_dialog_uses_config_authority_and_shows_links(server, browser_page):
    pg, errors = browser_page
    pg.route("**/api/config", lambda route: route.fulfill(json=ENTRA_CONFIG))
    pg.route("**/office.js", lambda route: route.fulfill(
        body="window.Office={onReady:(f)=>setTimeout(f,0),context:{ui:{messageParent:(m)=>{window.__msg=m}}}};",
        content_type="application/javascript"))
    pg.add_init_script("""window.msal = { PublicClientApplication: function (c) { window.__cfg = c;
        this.initialize = async () => {}; this.handleRedirectPromise = async () => null; window.__redirected = false; this.loginRedirect = async () => { window.__redirected = true; }; } };""")
    pg.route("**/vendor/msal/msal-browser.min.js", lambda route: route.fulfill(body="", content_type="application/javascript"))
    pg.goto(server + "/auth-dialog.html")
    pg.wait_for_function("window.__cfg")
    assert pg.evaluate("window.__cfg.auth.authority") == "https://login.microsoftonline.com/common"
    # it waits for the person to see the links and click before leaving for Entra
    pg.wait_for_selector("#continue")
    assert pg.evaluate("window.__redirected") is False
    pg.click("#continue")
    pg.wait_for_function("window.__redirected === true")
    hrefs = pg.eval_on_selector_all("#legal a", "els => els.map(e => e.href)")
    assert hrefs == ["https://example.com/privacy", "https://example.com/terms"]
    assert not errors, errors
