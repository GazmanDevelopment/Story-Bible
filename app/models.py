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
  break.
- Scalar text fields tolerate a stray non-string (e.g. a hand-edited
  import with a numeric age) by coercing it to text - see LooseStr. ID
  fields (character_ids, location_id, chapter_id, from/to) are kept as
  plain `str`, since a non-string value there indicates real corruption
  rather than a harmless type slip, and coercing it would just produce a
  dangling reference instead of a clear 400.
"""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, Field

from .html_sanitize import sanitize_html


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else str(v)


def _as_sanitized_html(v: Any) -> str:
    return sanitize_html(_as_text(v))


LooseStr = Annotated[str, BeforeValidator(_as_text)]
# For rich-text fields rendered as HTML rather than escaped as plain text
# (currently just Research.body, #43) - see app/html_sanitize.py.
SanitizedHtml = Annotated[str, BeforeValidator(_as_sanitized_html)]


class SeriesIn(BaseModel):
    name: LooseStr = "Untitled series"
    description: LooseStr = ""
    anchor_mode: LooseStr = "relative"  # "relative" | "date"
    anchor_date: LooseStr = ""
    anchor_label: LooseStr = ""
    character_fields: list[LooseStr] = Field(default_factory=list)


class ChapterIn(BaseModel):
    number: int = 0
    title: LooseStr = ""


class CharacterIn(BaseModel):
    name: LooseStr = ""
    role: LooseStr = ""
    aliases: LooseStr = ""
    age: LooseStr = ""
    height: LooseStr = ""
    gender: LooseStr = ""
    hair: LooseStr = ""
    eyes: LooseStr = ""
    style: LooseStr = ""
    custom: dict[str, LooseStr] = Field(default_factory=dict)
    preferences: LooseStr = ""
    backstory: LooseStr = ""
    notes: LooseStr = ""


class LocationIn(BaseModel):
    name: LooseStr = ""
    place: LooseStr = ""
    description: LooseStr = ""
    relevance: LooseStr = ""
    character_ids: list[str] = Field(default_factory=list)
    notes: LooseStr = ""


class EventIn(BaseModel):
    title: LooseStr = ""
    off_y: int = 0
    off_m: int = 0
    off_d: int = 0
    chapter_id: str = ""
    location_id: str = ""
    character_ids: list[str] = Field(default_factory=list)
    description: LooseStr = ""


class RelationshipIn(BaseModel):
    from_: str = Field("", alias="from")  # a character id - see the ID-field note above
    type: LooseStr = ""
    to: str = ""  # a character id - see the ID-field note above
    note: LooseStr = ""


class ResearchIn(BaseModel):
    title: LooseStr = ""
    body: SanitizedHtml = ""
    date_entered: LooseStr = ""
    chapter_id: str = ""
    character_ids: list[str] = Field(default_factory=list)
    location_ids: list[str] = Field(default_factory=list)
    event_ids: list[str] = Field(default_factory=list)


KIND_MODELS: dict[str, type[BaseModel]] = {
    "chapters": ChapterIn,
    "characters": CharacterIn,
    "locations": LocationIn,
    "events": EventIn,
    "relationships": RelationshipIn,
    "research": ResearchIn,
}
