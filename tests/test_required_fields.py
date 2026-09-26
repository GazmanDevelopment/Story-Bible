"""
#50: fields the app actually rejects as empty (character/place name, event
title, feedback title + description - see the `toast(...)` validation in
save()/save-feedback in app.js) must be visually flagged with a red
asterisk, not just called out after the fact via a toast once you hit Save.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_JS = (ROOT / "app" / "static" / "app.js").read_text()
STYLES_CSS = (ROOT / "app" / "static" / "styles.css").read_text()

REQUIRED_FIELD_CALLS = [
    'field("name", "Name", c.name, "text", "", true)',
    'field("name", "Name", l.name, "text", \'placeholder="e.g. The lake house"\', true)',
    'field("title", "What happens", e.title, "text", "", true)',
    'field("title", "Title", "", "text", \'maxlength="120" placeholder="Short summary"\', true)',
    'field("description", "Description", "", "textarea", '
    '\'rows="6" maxlength="4000" placeholder="What happened, or what you\\\'d like to see"\', true)',
]

# Fields that must NOT be flagged: nothing in save()/save-feedback rejects
# them when empty, so an asterisk there would be misleading.
NOT_REQUIRED_FIELD_CALLS = [
    'field("role", "Role", c.role, "text", \'placeholder="e.g. Protagonist"\')',
    'field("description", "Description", l.description, "textarea", \'rows="4"\')',
    'field("name", "Series name", s.name)',
]


def test_field_helper_renders_a_red_asterisk_when_required():
    assert re.search(
        r'function field\([^)]*required = false\)', APP_JS
    ), "field() must default required to false"
    assert '<span class="req">*</span>' in APP_JS


def test_req_marker_is_the_same_red_as_the_danger_button():
    assert re.search(r"button\.danger\s*\{[^}]*color:\s*var\(--warn\)", STYLES_CSS)
    assert re.search(r"\.req\s*\{[^}]*color:\s*var\(--warn\)", STYLES_CSS)


def test_every_field_the_save_handler_requires_is_flagged():
    for call in REQUIRED_FIELD_CALLS:
        assert call in APP_JS, f"expected a required-marked call: {call}"


def test_fields_the_save_handler_does_not_require_stay_unflagged():
    for call in NOT_REQUIRED_FIELD_CALLS:
        assert call in APP_JS, f"expected an unmarked call: {call}"


def test_save_validation_still_matches_the_flagged_fields():
    """Guards against the flags and the actual validation drifting apart."""
    assert '!data.name?.trim()' in APP_JS
    assert '!data.title?.trim()' in APP_JS
    assert '!description) { toast("Description is required")' in APP_JS
