# Headless UI test. Start the server first: python run_demo.py  (fresh data/ folder)
# Then: python tests/test_ui.py web   or   python tests/test_ui.py word  (simulated Word host)
import asyncio, sys
from playwright.async_api import async_playwright
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
errors = []
async def main(word):
    async with async_playwright() as p:
        b = await p.chromium.launch()
        pg = await b.new_page(viewport={"width": 360, "height": 780})
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and errors.append(m.text))
        async def office(route):
            await route.fulfill(body=FAKE_OFFICE if word else "", content_type="application/javascript")
        await pg.route("**/office.js", office)
        await pg.goto("http://localhost:8765/")
        await pg.wait_for_selector(".list li")
        tag = "word" if word else "web"
        await pg.screenshot(path=f"screenshots/{tag}_1_chars.png", full_page=True)
        await pg.click("text=Betsy Marr")
        await pg.wait_for_selector("form[data-kind=characters]")
        await pg.screenshot(path=f"screenshots/{tag}_2_char.png", full_page=True)
        # add relationship
        await pg.fill("#relType", "confides in")
        await pg.select_option("#relTo", label="Kristy Dunn")
        await pg.click("[data-act=add-rel]")
        await pg.wait_for_selector("text=Betsy Marr confides in Kristy Dunn")
        # edit + save a custom field
        await pg.fill("[data-custom='Skin']", "Pale, freckled shoulders")
        await pg.click("[data-act=save]")
        await pg.wait_for_timeout(300)
        assert await pg.input_value("[data-custom='Skin']") == "Pale, freckled shoulders"
        await pg.click("[data-act=cancel] >> nth=0")
        await pg.click("#tabs >> text=Timeline")
        await pg.wait_for_selector(".tl li")
        txt = await pg.inner_text("main")
        assert "14 Feb 2019" in txt and "14 Feb 2024" in txt, txt
        assert "Mark Hale 39" in txt, txt   # 34 + 5 years
        assert "Mark Hale 27" in txt, txt   # -7 years wedding
        await pg.screenshot(path=f"screenshots/{tag}_3_timeline.png", full_page=True)
        # new event with preview
        await pg.click("[data-act=new]")
        await pg.fill("[data-f=title]", "Test event")
        await pg.fill("[data-f=off_y]", "1"); await pg.fill("[data-f=off_m]", "0"); await pg.fill("[data-f=off_d]", "15")
        prev = await pg.inner_text("#whenPreview")
        assert "29 Feb 2020" in prev, prev
        await pg.click(".chip >> text=Sally Hale")
        await pg.click("[data-act=save]")
        await pg.wait_for_selector("text=Test event")
        # filter by chapter
        await pg.select_option("[data-act=tl-chapter]", label="Ch 2")
        n = await pg.locator(".tl li").count(); assert n == 2, n
        # places + series tabs
        await pg.click("#tabs >> text=Places"); await pg.wait_for_selector(".list >> text=The Lake House")
        await pg.click("#tabs >> text=Series"); await pg.wait_for_selector("text=Chapters (one Word doc each)")
        await pg.screenshot(path=f"screenshots/{tag}_4_series.png", full_page=True)
        # switch series
        await pg.select_option("#seriesSelect", label="Series B – After Hours")
        await pg.click("#tabs >> text=Timeline"); await pg.wait_for_selector("text=Night one")
        if word:
            await pg.click("[data-act=doc-link]")
            await pg.wait_for_selector("[data-act=doc-chapter]")
            await pg.select_option("[data-act=doc-chapter]", label="Ch 1 – Check-in")
            s = await pg.evaluate("window.__settings")
            assert s["storybible"]["chapter_id"], s
            await pg.select_option("#seriesSelect", label="Series A – The Lake House")
            await pg.click("#btnFind")
            await pg.wait_for_selector("h2:text('Betsy Marr')")
            await pg.click("text=Insert name at cursor")
            assert await pg.evaluate("window.__inserted") == ["Betsy Marr"]
            await pg.screenshot(path=f"screenshots/{tag}_5_find.png", full_page=True)
        await b.close()
    print(tag, "OK")

asyncio.run(main(sys.argv[1] == "word"))
print("errors:", errors)
