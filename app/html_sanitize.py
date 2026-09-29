"""
A small allowlist HTML sanitizer for the Research page's rich-text body
(#43) - the first field in this app that's ever rendered as HTML rather
than escaped as plain text (see field() in app.js, which `esc()`s every
other field). Runs server-side on every write, not just in the pane, since
the record endpoints take arbitrary JSON and the browser's Quill editor is
just one possible caller.

Stdlib-only (html.parser), rather than adding a dependency like bleach,
to keep this a small and fully auditable allowlist matching exactly the
basic formatting Quill's toolbar in app.js offers - nothing here needs to
handle the general case of arbitrary HTML.
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlparse

# Tags Quill's configured toolbar can produce. Anything else is unwrapped
# (its own tag dropped, children kept) rather than rejected outright, so a
# stray unsupported tag doesn't cost the user their actual text.
ALLOWED_TAGS = {
    "p", "br", "strong", "b", "em", "i", "u", "s",
    "h1", "h2", "h3", "blockquote", "ol", "ul", "li", "a", "img",
}
VOID_TAGS = {"br", "img"}
# Content of these is code/markup, not display text - drop the whole
# element, not just the wrapping tag, so its guts can't leak out as text.
DROP_CONTENT_TAGS = {"script", "style", "iframe", "object", "embed", "template"}

_SAFE_LINK_SCHEMES = {"http", "https", "mailto"}
# Plain http stays allowed (#71 considered dropping it and decided not to:
# the sanitizer only runs on write, so existing entries would keep an image
# the CSP/sanitizer no longer accepted until their next save, then lose it
# silently - a data-loss surprise for a small privacy gain).
_SAFE_IMG_SCHEMES = {"http", "https", "data"}

# Bounds on what the sanitizer will process (#71). Ordinary rich text has a
# few tags per paragraph; these are far above any real entry and exist so a
# hostile body can't make the parser burn seconds of CPU (2 MB of "<" took
# ~1.5 s and expanded 4x when escaped). Exceeding them is a rejected write,
# not silent truncation.
MAX_TAGS = 20_000
MAX_DEPTH = 100
# The app's own pipeline only ever produces canvas re-encoded PNG/JPEG data
# URLs (see compressImage() in app.js) - excluding image/svg+xml here (unlike
# a generic image allowlist) closes off SVG's own script/event-handler
# surface for a direct API write, which isn't otherwise restricted to what
# the browser UI can produce.
_SAFE_DATA_IMAGE_SUBTYPES = ("data:image/png", "data:image/jpeg", "data:image/gif", "data:image/webp")


def _safe_href(value: str) -> str | None:
    try:
        scheme = urlparse(value).scheme.lower()
    except ValueError:
        return None
    # Always require an explicit safe scheme, not just "absent or safe" -
    # urlparse gives an empty scheme for a protocol-relative URL
    # ("//evil.example.com/x") too, which a browser resolves against the
    # page's own scheme just like an absolute URL would, so leaving that
    # case unfiltered would defeat this allowlist entirely.
    if scheme not in _SAFE_LINK_SCHEMES:
        return None
    return value


def _safe_img_src(value: str) -> str | None:
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in _SAFE_IMG_SCHEMES:
        return None
    if scheme == "data" and not value.lower().startswith(_SAFE_DATA_IMAGE_SUBTYPES):
        return None
    return value


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._drop_depth = 0  # nesting depth inside a DROP_CONTENT_TAGS element
        self._stack: list[str] = []  # allowed, non-void tags currently open (for the depth cap)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._open(tag, attrs, self_closing=True)

    def _open(self, tag: str, attrs: list[tuple[str, str | None]], self_closing: bool) -> None:
        if self._drop_depth:
            if tag in DROP_CONTENT_TAGS:
                self._drop_depth += 1
            return
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if tag not in ALLOWED_TAGS:
            return  # unwrap: drop the tag, keep its children/text
        amap = {k: (v or "") for k, v in attrs}
        kept = ""
        if tag == "a":
            href = _safe_href(amap.get("href", ""))
            if href:
                kept = f' href="{escape(href, quote=True)}" target="_blank" rel="noopener noreferrer"'
        elif tag == "img":
            src = _safe_img_src(amap.get("src", ""))
            if not src:
                return
            alt = escape(amap.get("alt", ""), quote=True)
            kept = f' src="{escape(src, quote=True)}" alt="{alt}"'
        self.out.append(f"<{tag}{kept}>")
        if tag in VOID_TAGS:
            return
        if self_closing:
            self.out.append(f"</{tag}>")  # <p/> is an empty paragraph, not an open one
            return
        if tag in ("p", "li") and self._stack and self._stack[-1] == tag:
            self._stack.pop()  # HTML closes an open <p>/<li> when the next one starts
        self._stack.append(tag)
        if len(self._stack) > MAX_DEPTH:
            raise ValueError(f"is nested more than {MAX_DEPTH} levels deep")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag in ALLOWED_TAGS and tag not in VOID_TAGS:
            if tag in self._stack:
                # closes this tag and anything left open inside it
                del self._stack[len(self._stack) - 1 - self._stack[::-1].index(tag):]
            self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self._drop_depth:
            return
        self.out.append(escape(data, quote=False))


def sanitize_html(value: str) -> str:
    """Strip everything except the small tag/attribute allowlist above.
    Malformed markup is handled the same as HTMLParser handles it
    generally (best-effort). The one exception: input past MAX_TAGS tags or
    MAX_DEPTH levels of nesting raises ValueError (surfaced as a 400 by the
    model validator) rather than being quietly cleaned - see those constants."""
    if not value:
        return ""
    if value.count("<") > MAX_TAGS:
        raise ValueError(f"has more than {MAX_TAGS:,} tags")
    parser = _Sanitizer()
    parser.feed(value)
    parser.close()
    return "".join(parser.out)
