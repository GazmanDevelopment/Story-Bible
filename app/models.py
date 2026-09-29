"""
Pydantic models for the record kinds a series holds, plus the series
itself. These validate incoming data and fill in defaults for known
fields while keeping app/main.py's "store as JSON" design - a model here
still gets dumped straight into a single JSON column, so adding a new
field in the UI still doesn't need a migration. What this buys over the
old "just store whatever dict arrives" approach is a 400 for genuinely
malformed input instead of a 500, and predictable defaults instead of
"whatever the client happened to send, or a KeyError".

Deliberately permissive in two ways:
- Unknown top-level fields are ignored, not rejected (Pydantic's default
  `extra` behaviour) - a client sending an extra/legacy field shouldn't
  break. They are also *dropped*, not stored: a field only persists once it's
  declared on the matching model below. So "no schema change" means no
  database migration, not "no server change" - adding a field to the pane
  means adding it here too (#71). Unbounded pass-through was considered and
  rejected: it would let any client stash arbitrary data in every record.
- Scalar text fields tolerate a stray non-string (e.g. a hand-edited
  import with a numeric age) by coercing it to text - see LooseStr. ID
  fields (character_ids, location_id, chapter_id, from/to) are kept as
  plain `str`, since a non-string value there indicates real corruption
  rather than a harmless type slip, and coercing it would just produce a
  dangling reference instead of a clear 400.

Everything is size-bounded (#71) - text fields, list lengths, ids, numbers -
so no single record can be made arbitrarily large or slow to process. The
limits are generous for a novel's worth of notes; hitting one is a 400 naming
the field. Stored data already over a limit is still readable, and only fails
if it is saved again unchanged.
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints

from .html_sanitize import sanitize_html


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _as_sanitized_html(v: Any) -> str:
    text = _as_text(v)
    if len(text) > MAX_HTML:
        raise ValueError(f"must be at most {MAX_HTML:,} characters")
    clean_html = sanitize_html(text)
    if len(clean_html) > MAX_HTML:
        # Escaping can expand the input (& -> &amp; is 5x), so the cap has to
        # hold for what is actually stored and sent back, not just what came in.
        raise ValueError(f"must be at most {MAX_HTML:,} characters once cleaned")
    return clean_html


LooseStr = Annotated[str, BeforeValidator(_as_text)]

# Size limits (#71), in characters. SHORT: names, titles, single-line fields.
# MEDIUM: a paragraph. LONG: free-text notes - 200k characters is ~35,000
# words in one field.
MAX_SHORT, MAX_MEDIUM, MAX_LONG = 1_000, 10_000, 200_000
MAX_ID = 64          # ids are 12 hex characters; this leaves room for foreign ones on import
MAX_IDS = 2_000      # references in one list (characters in a scene, ...)
MAX_LIST = 200       # entries in a plain string list (character_fields, relationship_types)
MAX_CUSTOM_FIELDS = 100
MAX_NUMBER = 1_000_000

ShortStr = Annotated[str, BeforeValidator(_as_text), StringConstraints(max_length=MAX_SHORT)]
MediumStr = Annotated[str, BeforeValidator(_as_text), StringConstraints(max_length=MAX_MEDIUM)]
LongStr = Annotated[str, BeforeValidator(_as_text), StringConstraints(max_length=MAX_LONG)]
IdStr = Annotated[str, StringConstraints(max_length=MAX_ID)]
IdList = Annotated[list[IdStr], Field(max_length=MAX_IDS)]
Number = Annotated[int, Field(ge=-MAX_NUMBER, le=MAX_NUMBER)]

# For rich-text fields rendered as HTML rather than escaped as plain text
# (currently just Research.body, #43) - see app/html_sanitize.py. The length
# cap is on the raw input, checked *before* sanitising (the sanitizer does
# the tag-count and depth bounding): embedded images are data URLs of up to
# ~3.5M characters each, so this leaves room for a few of them.
MAX_HTML = 8_000_000
SanitizedHtml = Annotated[str, BeforeValidator(_as_sanitized_html)]


class SeriesIn(BaseModel):
    name: ShortStr = "Untitled series"
    description: LongStr = ""
    anchor_mode: ShortStr = "relative"  # "relative" | "date"
    anchor_date: ShortStr = ""
    anchor_label: ShortStr = ""
    character_fields: list[ShortStr] = Field(default_factory=list, max_length=MAX_LIST)
    # #63: per-series relationship-type suggestions, editable the same way
    # as character_fields above - the relationship form's type field is
    # (and always was) free text either way, so this only customizes the
    # autocomplete list, never restricts what can actually be saved.
    relationship_types: list[ShortStr] = Field(default_factory=list, max_length=MAX_LIST)


class ChapterIn(BaseModel):
    number: Number = 0
    title: ShortStr = ""


class CharacterIn(BaseModel):
    name: ShortStr = ""
    role: ShortStr = ""
    aliases: MediumStr = ""
    age: ShortStr = ""
    height: ShortStr = ""
    gender: ShortStr = ""
    hair: ShortStr = ""
    eyes: ShortStr = ""
    style: ShortStr = ""
    custom: dict[ShortStr, MediumStr] = Field(default_factory=dict, max_length=MAX_CUSTOM_FIELDS)
    preferences: LongStr = ""
    backstory: LongStr = ""
    notes: LongStr = ""


class LocationIn(BaseModel):
    name: ShortStr = ""
    place: ShortStr = ""
    description: LongStr = ""
    relevance: LongStr = ""
    character_ids: IdList = Field(default_factory=list)
    notes: LongStr = ""


class EventIn(BaseModel):
    title: ShortStr = ""
    off_y: Number = 0
    off_m: Number = 0
    off_d: Number = 0
    chapter_id: IdStr = ""
    location_id: IdStr = ""
    character_ids: IdList = Field(default_factory=list)
    description: LongStr = ""


class RelationshipIn(BaseModel):
    from_: IdStr = Field("", alias="from")  # a character id - see the ID-field note above
    type: ShortStr = ""
    to: IdStr = ""  # a character id - see the ID-field note above
    note: MediumStr = ""


class ResearchIn(BaseModel):
    title: ShortStr = ""
    body: SanitizedHtml = ""
    date_entered: ShortStr = ""
    chapter_id: IdStr = ""
    character_ids: IdList = Field(default_factory=list)
    location_ids: IdList = Field(default_factory=list)
    event_ids: IdList = Field(default_factory=list)


KIND_MODELS: dict[str, type[BaseModel]] = {
    "chapters": ChapterIn,
    "characters": CharacterIn,
    "locations": LocationIn,
    "events": EventIn,
    "relationships": RelationshipIn,
    "research": ResearchIn,
}


# Which fields hold ids of other records in the same series, and of what kind
# (#71). Validated on write so a record can't point at another series' data
# or at something of the wrong type; also what app/main.py's import scrubbing
# walks.
REF_FIELDS: dict[str, dict[str, str]] = {
    "locations": {"character_ids": "characters"},
    "events": {"chapter_id": "chapters", "location_id": "locations", "character_ids": "characters"},
    "relationships": {"from": "characters", "to": "characters"},
    "research": {
        "chapter_id": "chapters", "character_ids": "characters",
        "location_ids": "locations", "event_ids": "events",
    },
}
