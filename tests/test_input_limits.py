"""
#71: no user-supplied input can produce a 500, an unbounded record, or
multi-second CPU - field/list/number limits, the sanitizer's bounds, the
per-series and per-person ceilings, same-series reference validation, and a
robust import.
"""
import os
import sqlite3
import tempfile
import time

if "STORYBIBLE_DB" not in os.environ:
    _fd, _db_path = tempfile.mkstemp(suffix=".db")
    os.close(_fd)
    os.environ["STORYBIBLE_DB"] = _db_path

import pytest
from fastapi.testclient import TestClient

from app import html_sanitize, main, models

c = TestClient(main.app)


def _series(name="Limits"):
    return c.post("/api/series", json={"data": {"name": name}}).json()["id"]


def _rec(sid, kind, **data):
    r = c.post(f"/api/series/{sid}/{kind}", json={"data": data})
    assert r.status_code == 200, r.text
    return r.json()["id"]


# ------------------------------------------------------ field limits (F16)
def test_text_fields_are_bounded():
    sid = _series()
    ok = c.post(f"/api/series/{sid}/characters", json={"data": {"name": "x" * models.MAX_SHORT}})
    assert ok.status_code == 200
    too_long = c.post(f"/api/series/{sid}/characters", json={"data": {"name": "x" * (models.MAX_SHORT + 1)}})
    assert too_long.status_code == 400
    assert "name" in too_long.text  # names the field


def test_long_fields_allow_a_novels_worth_but_not_more():
    sid = _series()
    assert c.post(f"/api/series/{sid}/characters",
                  json={"data": {"name": "a", "backstory": "x" * models.MAX_LONG}}).status_code == 200
    assert c.post(f"/api/series/{sid}/characters",
                  json={"data": {"name": "a", "backstory": "x" * (models.MAX_LONG + 1)}}).status_code == 400


def test_a_non_string_is_still_coerced_then_bounded():
    sid = _series()
    assert c.post(f"/api/series/{sid}/characters", json={"data": {"name": "a", "age": 42}}).status_code == 200


def test_series_fields_are_bounded():
    assert c.post("/api/series", json={"data": {"name": "x" * (models.MAX_SHORT + 1)}}).status_code == 400
    assert c.post("/api/series", json={"data": {"name": "a", "description": "x" * (models.MAX_LONG + 1)}}).status_code == 400


def test_list_sizes_are_bounded():
    too_many_fields = ["f%d" % i for i in range(models.MAX_LIST + 1)]
    assert c.post("/api/series", json={"data": {"name": "a", "character_fields": too_many_fields}}).status_code == 400
    assert c.post("/api/series", json={"data": {"name": "a", "relationship_types": too_many_fields}}).status_code == 400
    ok = c.post("/api/series", json={"data": {"name": "a", "character_fields": too_many_fields[:-1]}})
    assert ok.status_code == 200


def test_custom_fields_are_bounded():
    sid = _series()
    many = {"k%d" % i: "v" for i in range(models.MAX_CUSTOM_FIELDS + 1)}
    assert c.post(f"/api/series/{sid}/characters", json={"data": {"name": "a", "custom": many}}).status_code == 400
    long_key = {"k" * (models.MAX_SHORT + 1): "v"}
    assert c.post(f"/api/series/{sid}/characters", json={"data": {"name": "a", "custom": long_key}}).status_code == 400


def test_id_lists_and_ids_are_bounded():
    sid = _series()
    assert c.post(f"/api/series/{sid}/locations",
                  json={"data": {"name": "L", "character_ids": ["x"] * (models.MAX_IDS + 1)}}).status_code == 400
    assert c.post(f"/api/series/{sid}/locations",
                  json={"data": {"name": "L", "character_ids": ["x" * (models.MAX_ID + 1)]}}).status_code == 400


@pytest.mark.parametrize("value", [10**7, -(10**7), 10**30])
def test_numbers_are_bounded(value):
    sid = _series()
    assert c.post(f"/api/series/{sid}/events", json={"data": {"title": "e", "off_y": value}}).status_code == 400
    assert c.post(f"/api/series/{sid}/chapters", json={"data": {"number": value, "title": "c"}}).status_code == 400


def test_sensible_numbers_still_work():
    sid = _series()
    assert c.post(f"/api/series/{sid}/events", json={"data": {"title": "e", "off_y": -50, "off_m": 6, "off_d": 400}}).status_code == 200


def test_unknown_fields_are_dropped_not_stored():
    """F13, decided: unknown fields are ignored AND dropped (documented in
    app/models.py) - a field persists once the model declares it."""
    sid = _series()
    rid = _rec(sid, "characters", name="a", future_field="kept?")
    got = c.get(f"/api/series/{sid}/characters").json()
    assert "future_field" not in next(r for r in got if r["id"] == rid)


# ------------------------------------------------- sanitizer bounds (S1/S2)
def test_research_body_length_is_capped_before_sanitising(monkeypatch):
    monkeypatch.setattr(models, "MAX_HTML", 100)
    sid = _series()
    assert c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": "x" * 100}}).status_code == 200
    assert c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": "x" * 101}}).status_code == 400


def test_a_body_with_large_images_still_fits():
    """Images are data URLs of up to ~3.5M characters each (app.js). The field
    cap must not be the thing that rejects them: one goes through a normal
    request (5 MiB body cap), and several fit the model - which is what an
    import (20 MiB body cap) validates against."""
    img = '<img src="data:image/png;base64,' + "A" * 3_400_000 + '">'
    sid = _series()
    r = c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": "<p>x</p>" + img}})
    assert r.status_code == 200, r.text[:200]
    assert models.ResearchIn(body=img * 2).body.count("<img") == 2


def test_too_many_tags_is_rejected_fast():
    started = time.perf_counter()
    with pytest.raises(ValueError, match="tags"):
        html_sanitize.sanitize_html("<" * 2_000_000)
    assert time.perf_counter() - started < 0.5  # was ~1.5 s to process


def test_tag_count_boundary():
    assert html_sanitize.sanitize_html("<b>x</b>" * (html_sanitize.MAX_TAGS // 2)) != ""
    with pytest.raises(ValueError):
        html_sanitize.sanitize_html("<b>x</b>" * (html_sanitize.MAX_TAGS // 2 + 1))


def test_nesting_depth_is_capped():
    ok = "<b>" * html_sanitize.MAX_DEPTH + "x" + "</b>" * html_sanitize.MAX_DEPTH
    assert html_sanitize.sanitize_html(ok).count("<b>") == html_sanitize.MAX_DEPTH
    with pytest.raises(ValueError, match="nested"):
        html_sanitize.sanitize_html("<b>" * (html_sanitize.MAX_DEPTH + 1))


def test_sequential_tags_are_not_mistaken_for_deep_nesting():
    assert html_sanitize.sanitize_html("<b>x</b>" * 5000).count("<b>") == 5000


def test_hostile_bodies_over_the_api_are_a_400_not_a_slow_500():
    sid = _series()
    for body in ("<" * 1_000_000, "<b>" * 500):
        started = time.perf_counter()
        r = c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": body}})
        assert r.status_code == 400
        assert time.perf_counter() - started < 1.0


def test_plain_text_with_a_few_angle_brackets_is_fine():
    sid = _series()
    r = c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": "<p>if a &lt; b and 3 < 4 then</p>"}})
    assert r.status_code == 200


def test_all_three_image_schemes_are_still_accepted():
    """S2 considered dropping plain http and decided against it: the sanitizer
    only runs on write, so existing entries would silently lose the image at
    their next save."""
    for src in ("https://example.com/x.png", "http://example.com/x.png", "data:image/png;base64,AAAA"):
        assert f'src="{src}"' in html_sanitize.sanitize_html(f'<img src="{src}">')


def test_self_closing_and_unclosed_tags_are_not_counted_as_nesting():
    """Depth is the real open-tag depth, not a running count of start tags."""
    assert html_sanitize.sanitize_html("<p/>" * 300) == "<p></p>" * 300
    assert html_sanitize.sanitize_html("<p>x" * 300).count("<p>") == 300    # HTML closes each <p> at the next
    assert html_sanitize.sanitize_html("<li>x" * 300).count("<li>") == 300
    assert html_sanitize.sanitize_html("</b>" * 500) == "</b>" * 500          # stray end tags don't underflow the counter
    html_sanitize.sanitize_html("</b>" * 500 + "<b>" * html_sanitize.MAX_DEPTH)


def test_closing_a_tag_closes_what_was_left_open_inside_it():
    html_sanitize.sanitize_html(("<b><i>x</b>") * 500)  # each </b> also closes the dangling <i>


def test_escaping_expansion_cannot_get_past_the_cap(monkeypatch):
    monkeypatch.setattr(models, "MAX_HTML", 100)
    sid = _series()
    assert c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": "&" * 10}}).status_code == 200
    r = c.post(f"/api/series/{sid}/research", json={"data": {"title": "r", "body": "&" * 30}})  # 30 in, 150 stored
    assert r.status_code == 400 and "cleaned" in r.text


# ------------------------------------------------------------ ceilings (F16)
def test_records_per_series_are_capped(monkeypatch):
    monkeypatch.setattr(main, "MAX_RECORDS_PER_SERIES", 3)
    sid = _series()
    for i in range(3):
        _rec(sid, "characters", name=f"c{i}")
    r = c.post(f"/api/series/{sid}/locations", json={"data": {"name": "one too many"}})
    assert r.status_code == 400 and "limit" in r.text
    assert _series() and True  # other series are unaffected
    other = _series("other")
    assert c.post(f"/api/series/{other}/characters", json={"data": {"name": "fine"}}).status_code == 200


def test_the_cap_is_not_a_409(monkeypatch):
    """The pane treats 409 as a save conflict and offers to reload - a limit
    must not look like that."""
    monkeypatch.setattr(main, "MAX_RECORDS_PER_SERIES", 0)
    sid = _series()
    assert c.post(f"/api/series/{sid}/characters", json={"data": {"name": "x"}}).status_code == 400


def test_series_per_owner_are_capped(monkeypatch):
    with main.db() as con:
        owned = con.execute("SELECT COUNT(*) FROM series WHERE owner_oid='local'").fetchone()[0]
    monkeypatch.setattr(main, "MAX_SERIES_PER_OWNER", owned + 1)
    assert c.post("/api/series", json={"data": {"name": "last allowed"}}).status_code == 200
    assert c.post("/api/series", json={"data": {"name": "one too many"}}).status_code == 400
    assert c.post("/api/import", json={"series": {"name": "import too many"}}).status_code == 400


def test_import_over_the_record_cap_is_refused(monkeypatch):
    monkeypatch.setattr(main, "MAX_RECORDS_PER_SERIES", 2)
    bundle = {"series": {"name": "big"}, "characters": [{"id": f"c{i}", "name": "x"} for i in range(3)]}
    assert c.post("/api/import", json=bundle).status_code == 400


# ------------------------------------------- same-series references (F14)
def test_valid_references_are_accepted():
    sid = _series()
    ch = _rec(sid, "characters", name="A")
    loc = _rec(sid, "locations", name="L", character_ids=[ch])
    chap = _rec(sid, "chapters", number=1, title="One")
    _rec(sid, "events", title="E", chapter_id=chap, location_id=loc, character_ids=[ch])
    _rec(sid, "relationships", **{"from": ch, "to": ch, "type": "self"})
    _rec(sid, "research", title="R", chapter_id=chap, character_ids=[ch], location_ids=[loc])


def test_reference_to_another_series_is_rejected():
    a, b = _series("A"), _series("B")
    foreign = _rec(a, "characters", name="in A")
    r = c.post(f"/api/series/{b}/locations", json={"data": {"name": "L", "character_ids": [foreign]}})
    assert r.status_code == 400
    assert "character_ids" in r.text


def test_reference_of_the_wrong_kind_is_rejected():
    sid = _series()
    loc = _rec(sid, "locations", name="L")
    assert c.post(f"/api/series/{sid}/events", json={"data": {"title": "e", "character_ids": [loc]}}).status_code == 400
    assert c.post(f"/api/series/{sid}/events", json={"data": {"title": "e", "chapter_id": loc}}).status_code == 400


@pytest.mark.parametrize("kind,field", [
    ("locations", "character_ids"), ("events", "character_ids"), ("events", "chapter_id"),
    ("events", "location_id"), ("relationships", "from"), ("relationships", "to"),
    ("research", "chapter_id"), ("research", "character_ids"), ("research", "location_ids"), ("research", "event_ids"),
])
def test_every_reference_field_is_checked(kind, field):
    sid = _series()
    ghost = ["ghost"] if field.endswith("_ids") else "ghost"
    r = c.post(f"/api/series/{sid}/{kind}", json={"data": {"title": "t", "name": "n", field: ghost}})
    assert r.status_code == 400, f"{kind}.{field} accepted an id that doesn't exist"


def test_updating_to_a_bad_reference_is_rejected():
    sid = _series()
    ch = _rec(sid, "characters", name="A")
    loc = c.post(f"/api/series/{sid}/locations", json={"data": {"name": "L", "character_ids": [ch]}}).json()
    r = c.put(f"/api/series/{sid}/locations/{loc['id']}",
              json={"data": {"name": "L", "character_ids": ["ghost"]}, "version": loc["version"]})
    assert r.status_code == 400


def test_a_stale_edit_still_gets_a_409_not_a_400_about_a_deleted_reference():
    """Deleting a character bumps the version of everything that referenced
    it - so a form open on the old version must hit the conflict prompt, not
    a baffling 400 about a reference the user didn't touch."""
    sid = _series()
    ch = _rec(sid, "characters", name="A")
    ev = c.post(f"/api/series/{sid}/events", json={"data": {"title": "E", "character_ids": [ch]}}).json()
    assert c.delete(f"/api/series/{sid}/characters/{ch}").status_code == 200
    r = c.put(f"/api/series/{sid}/events/{ev['id']}",
              json={"data": {"title": "E", "character_ids": [ch]}, "version": ev["version"]})
    assert r.status_code == 409


def test_empty_references_are_fine():
    sid = _series()
    _rec(sid, "events", title="E", chapter_id="", location_id="", character_ids=[])


# ------------------------------------------------------ import robustness
@pytest.mark.parametrize("bad_id", [{"a": 1}, ["x"], 5, None, True, "", "x" * 65])
def test_import_rejects_bad_ids_with_a_400_never_a_500(bad_id):
    r = c.post("/api/import", json={"series": {"name": "s"}, "characters": [{"id": bad_id, "name": "x"}]})
    assert r.status_code == 400


def test_import_remaps_references_and_scrubs_broken_ones():
    bundle = {
        "series": {"name": "Scrub"},
        "characters": [{"id": "old-c1", "name": "A"}, {"id": "old-c2", "name": "B"}],
        "locations": [{"id": "old-l1", "name": "L", "character_ids": ["old-c1", "ghost", "old-l1"]}],
        "events": [{"id": "old-e1", "title": "E", "character_ids": ["old-c2"], "location_id": "ghost"}],
        "relationships": [
            {"id": "old-r1", "from": "old-c1", "to": "old-c2", "type": "friend of"},
            {"id": "old-r2", "from": "old-c1", "to": "ghost", "type": "knows"},
        ],
    }
    r = c.post("/api/import", json=bundle)
    assert r.status_code == 200, r.text
    out = r.json()
    # ghost in a location's list (1) + a wrong-kind id there (1) + ghost location_id (1)
    # + the relationship with a missing end (1 dropped ref + 1 skipped record)
    assert out["dropped_references"] == 5
    got = c.get(f"/api/series/{out['id']}/bundle").json()
    new_ids = {r["id"] for k in ("characters", "locations", "events") for r in got[k]}
    assert not any(i.startswith("old-") for i in new_ids)
    loc = got["locations"][0]
    assert len(loc["character_ids"]) == 1 and loc["character_ids"][0] in {c_["id"] for c_ in got["characters"]}
    assert got["events"][0]["location_id"] == ""
    assert len(got["relationships"]) == 1  # the one with a missing end was skipped


def test_a_clean_import_reports_no_dropped_references():
    r = c.post("/api/import", json={"series": {"name": "Clean"}, "characters": [{"id": "a", "name": "x"}]})
    assert r.json()["dropped_references"] == 0


def test_a_bad_record_leaves_no_half_imported_series():
    before = len(c.get("/api/series").json())
    bundle = {"series": {"name": "Half"}, "characters": [{"id": "a", "name": "x"}, {"id": "b", "name": "y" * 5000}]}
    assert c.post("/api/import", json=bundle).status_code == 400
    assert len(c.get("/api/series").json()) == before


def test_import_validates_before_it_takes_the_write_lock(monkeypatch):
    """F17: validating a big bundle must not hold SQLite's single write lock.
    Every validate_record call checks that another connection can still get
    the write lock right now."""
    real = main.validate_record
    checked = []

    def probing_validate(kind, data):
        con = sqlite3.connect(main.DB_PATH, timeout=0.05)
        try:
            con.execute("BEGIN IMMEDIATE")  # raises 'database is locked' if a writer is open
            con.rollback()
            checked.append(kind)
        finally:
            con.close()
        return real(kind, data)

    monkeypatch.setattr(main, "validate_record", probing_validate)
    bundle = {"series": {"name": "Lock"}, "characters": [{"id": "a", "name": "x"}, {"id": "b", "name": "y"}]}
    assert c.post("/api/import", json=bundle).status_code == 200
    assert checked == ["characters", "characters"]


# --------------------------------------- review follow-ups (races, legacy data)
def test_a_record_that_already_has_a_dangling_link_can_still_be_saved():
    """Only NEW links must resolve - old data from before this check may hold
    dangling ones, and the user must still be able to fix a typo in it."""
    sid = _series()
    ch = _rec(sid, "characters", name="A")
    loc = c.post(f"/api/series/{sid}/locations", json={"data": {"name": "L", "character_ids": [ch]}}).json()
    with main.db(write=True) as con:  # simulate a legacy record pointing at something that isn't there
        row = con.execute("SELECT data FROM records WHERE id=?", (loc["id"],)).fetchone()
        import json as _json
        d = _json.loads(row["data"]); d["character_ids"] = [ch, "long-gone"]
        con.execute("UPDATE records SET data=?, version=version+1 WHERE id=?", (_json.dumps(d), loc["id"]))
    cur = c.get(f"/api/series/{sid}/locations").json()[0]
    fixed = c.put(f"/api/series/{sid}/locations/{loc['id']}",
                  json={"data": {"name": "L (renamed)", "character_ids": [ch, "long-gone"]}, "version": cur["version"]})
    assert fixed.status_code == 200, fixed.text
    added = c.put(f"/api/series/{sid}/locations/{loc['id']}",
                  json={"data": {"name": "L", "character_ids": [ch, "long-gone", "brand-new-ghost"]}, "version": fixed.json()["version"]})
    assert added.status_code == 400


def _assert_write_lock_held(con):
    other = sqlite3.connect(main.DB_PATH, timeout=0.05)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
    finally:
        other.close()


def test_the_reference_and_quota_checks_run_inside_the_write_lock(monkeypatch):
    """A check followed by a write is only safe if nothing can write in
    between. Each check asserts another connection cannot take the write lock
    at that moment."""
    seen = []
    real_refs, real_quota = main.check_refs, main.check_series_quota

    def refs(con, *a, **k):
        _assert_write_lock_held(con); seen.append("refs")
        return real_refs(con, *a, **k)

    def quota(con, *a, **k):
        _assert_write_lock_held(con); seen.append("quota")
        return real_quota(con, *a, **k)

    monkeypatch.setattr(main, "check_refs", refs)
    monkeypatch.setattr(main, "check_series_quota", quota)
    sid = _series()
    ch = _rec(sid, "characters", name="A")
    loc = c.post(f"/api/series/{sid}/locations", json={"data": {"name": "L", "character_ids": [ch]}}).json()
    c.put(f"/api/series/{sid}/locations/{loc['id']}", json={"data": {"name": "L2", "character_ids": [ch]}, "version": loc["version"]})
    c.post("/api/import", json={"series": {"name": "lock check"}})
    assert seen.count("refs") >= 2 and seen.count("quota") == 2  # the series created here, and the import


def test_the_record_quota_count_runs_inside_the_write_lock(monkeypatch):
    real = main.check_refs
    monkeypatch.setattr(main, "MAX_RECORDS_PER_SERIES", 5)
    sid = _series()
    seen = []
    monkeypatch.setattr(main, "check_refs", lambda con, *a, **k: (_assert_write_lock_held(con), seen.append(1), real(con, *a, **k))[2])
    _rec(sid, "characters", name="x")
    assert seen == [1]  # the count and the ref check share one locked transaction


def test_import_name_suffix_never_pushes_the_name_past_the_limit():
    name = "N" * models.MAX_SHORT
    first = c.post("/api/import", json={"series": {"name": name}}).json()
    second = c.post("/api/import", json={"series": {"name": name}}).json()
    assert len(second["name"]) <= models.MAX_SHORT and second["name"].endswith(" (imported)")
    got = c.get(f"/api/series/{second['id']}/bundle").json()["series"]
    assert c.put(f"/api/series/{second['id']}", json={"data": {"name": second["name"]}, "version": got["version"]}).status_code == 200
    assert first["name"] == name


def test_import_scrubs_overlong_and_junk_foreign_references_instead_of_failing():
    bundle = {
        "series": {"name": "Legacy refs"},
        "characters": [{"id": "a", "name": "x"}],
        "locations": [{"id": "l", "name": "L", "character_ids": ["a", "z" * 300, {"a": 1}, 7, None]}],
        "events": [{"id": "e", "title": "E", "location_id": {"nested": "junk"}, "chapter_id": "y" * 300}],
    }
    r = c.post("/api/import", json=bundle)
    assert r.status_code == 200, r.text
    assert r.json()["dropped_references"] == 6  # 4 junk list entries + 2 bad scalars
    got = c.get(f"/api/series/{r.json()['id']}/bundle").json()
    assert len(got["locations"][0]["character_ids"]) == 1


def test_import_error_names_the_record_that_failed():
    bundle = {"series": {"name": "Names the culprit"}, "characters": [{"id": "ok", "name": "fine"}, {"id": "bad-one", "name": "y" * 5000}]}
    r = c.post("/api/import", json=bundle)
    assert r.status_code == 400
    assert "characters record 'bad-one'" in r.text and "name" in r.text
