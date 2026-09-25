"""Generates the two sample series as import-ready JSON bundles in samples/."""
import json
from pathlib import Path

FIELDS = ["Build", "Skin", "Tattoos", "Piercings",
          "Distinguishing marks", "Voice & mannerisms"]


def bundle(series, chapters, characters, locations, events, rels):
    return {"series": {"id": "s", **series}, "chapters": chapters, "characters": characters,
            "locations": locations, "events": events,
            "relationships": [{"id": f"r{i}", "from": a, "type": t, "to": b, "note": n}
                              for i, (a, t, b, n) in enumerate(rels)]}


A = bundle(
    {"name": "Series A – The Lake House", "anchor_mode": "date", "anchor_date": "2019-02-14",
     "anchor_label": "Valentine's party", "character_fields": FIELDS,
     "description": "Five years in the life of a marriage, a mistress and the friend who sees it all."},
    [{"id": "ch1", "number": 1, "title": "The Party"},
     {"id": "ch2", "number": 2, "title": "Summer at the Lake"},
     {"id": "ch3", "number": 3, "title": "Five Years On"}],
    [
        {"id": "mark", "name": "Mark Hale", "role": "Protagonist", "age": "34", "gender": "Male",
         "height": "185 cm", "hair": "Dark brown, short", "eyes": "Grey", "style": "Smart casual",
         "aliases": "Mark, Hale",
         "custom": {"Build": "Swimmer's build, broad shoulders",
                    "Tattoos": "None", "Distinguishing marks": "Scar through left eyebrow",
                    "Voice & mannerisms": "Quiet, rubs his jaw when lying"},
         "preferences": "(your detail here)",
         "backstory": "Architect. Married Sally at 27. Restless since the business took off."},
        {"id": "sally", "name": "Sally Hale", "role": "Wife", "age": "32", "gender": "Female",
         "height": "165 cm", "hair": "Honey blonde, shoulder length", "eyes": "Blue", "style": "Natural",
         "custom": {"Build": "Petite, runner",
                    "Tattoos": "Small swallow, right hip", "Piercings": "Ears only"},
         "preferences": "(your detail here)",
         "backstory": "Primary-school teacher. Knows more than she lets on."},
        {"id": "betsy", "name": "Betsy Marr", "role": "Mistress", "age": "29", "gender": "Female",
         "height": "172 cm", "hair": "Jet black, undercut", "eyes": "Green", "style": "Goth",
         "aliases": "Bets",
         "custom": {"Build": "Tall, curvy", "Tattoos": "Full sleeve, left arm (moths & roses)",
                    "Piercings": "Septum, tongue"},
         "preferences": "(your detail here)",
         "backstory": "Tattoo artist Mark hires to design a mural for the lake house."},
        {"id": "kristy", "name": "Kristy Dunn", "role": "Friend", "age": "33", "gender": "Female",
         "height": "160 cm", "hair": "Red curls", "eyes": "Hazel", "style": "Boho",
         "custom": {"Voice & mannerisms": "Loud laugh, talks with her hands"},
         "backstory": "Sally's best friend since uni. Hosts the Valentine's party."},
    ],
    [
        {"id": "lake", "name": "The Lake House", "place": "Lake Macquarie, NSW",
         "description": "Timber house on stilts, jetty, outdoor shower.",
         "relevance": "Mark's project and hideaway; where the affair starts.",
         "character_ids": ["mark", "betsy", "sally"]},
        {"id": "kflat", "name": "Kristy's flat", "place": "Newtown, Sydney",
         "description": "Warehouse conversion, fairy lights, too many plants.",
         "relevance": "Neutral ground; the party and later confessions.", "character_ids": ["kristy", "sally"]},
    ],
    [
        {"id": "e0", "title": "Mark and Sally marry", "off_y": -7, "off_m": 0, "off_d": 0,
         "character_ids": ["mark", "sally"], "chapter_id": "", "location_id": ""},
        {"id": "e1", "title": "Valentine's party – Mark meets Betsy", "off_y": 0, "off_m": 0, "off_d": 0,
         "character_ids": ["mark", "sally", "betsy", "kristy"], "chapter_id": "ch1", "location_id": "kflat"},
        {"id": "e2", "title": "First night at the lake house", "off_y": 0, "off_m": 4, "off_d": 10,
         "character_ids": ["mark", "betsy"], "chapter_id": "ch2", "location_id": "lake"},
        {"id": "e3", "title": "Kristy sees Mark's car at the lake", "off_y": 0, "off_m": 5, "off_d": 2,
         "character_ids": ["kristy"], "chapter_id": "ch2", "location_id": "lake"},
        {"id": "e4", "title": "Reunion at the lake house", "off_y": 5, "off_m": 0, "off_d": 0,
         "character_ids": ["mark", "sally", "betsy", "kristy"], "chapter_id": "ch3", "location_id": "lake"},
    ],
    [("mark", "married to", "sally", "7 years at story start"),
     ("betsy", "mistress of", "mark", "From ch 2"),
     ("kristy", "best friend of", "sally", "Since uni"),
     ("kristy", "flirts with", "mark", "")],
)

B = bundle(
    {"name": "Series B – After Hours", "anchor_mode": "relative", "anchor_label": "Night one",
     "character_fields": FIELDS, "description": "One long weekend at a boutique hotel."},
    [{"id": "b1", "number": 1, "title": "Check-in"}],
    [{"id": "dana", "name": "Dana", "role": "Protagonist", "age": "38", "hair": "Silver pixie",
      "eyes": "Brown", "style": "Tailored", "custom": {}},
     {"id": "jules", "name": "Jules", "role": "Night manager", "age": "31", "hair": "Brown, tied back",
      "eyes": "Blue", "style": "Uniform", "custom": {}}],
    [{"id": "hotel", "name": "The Carrington", "place": "Blue Mountains, NSW",
      "relevance": "Whole story takes place here", "character_ids": ["dana", "jules"]}],
    [{"id": "x1", "title": "Dana checks in late", "off_y": 0, "off_m": 0, "off_d": 0,
      "character_ids": ["dana", "jules"], "chapter_id": "b1", "location_id": "hotel"},
     {"id": "x2", "title": "Checkout", "off_y": 0, "off_m": 0, "off_d": 3,
      "character_ids": ["dana"], "chapter_id": "", "location_id": "hotel"}],
    [("jules", "flirts with", "dana", "")],
)

out = Path(__file__).parent / "samples"
out.mkdir(exist_ok=True)
(out / "series_a_lake_house.json").write_text(json.dumps(A, indent=2))
(out / "series_b_after_hours.json").write_text(json.dumps(B, indent=2))
print("wrote", list(out.iterdir()))
