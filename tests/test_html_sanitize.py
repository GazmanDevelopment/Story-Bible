"""
Unit tests for app/html_sanitize.py - the allowlist sanitizer that runs on
the Research page's body field (#43), the first place in this app that
renders user-entered content as HTML instead of escaping it as plain text.
"""
from app.html_sanitize import sanitize_html


def test_allowed_formatting_tags_pass_through():
    html = "<p>Hello <strong>bold</strong> <em>italic</em> <u>underline</u> <s>strike</s></p>"
    assert sanitize_html(html) == html


def test_headers_blockquote_and_lists_pass_through():
    html = "<h1>T</h1><h2>T</h2><h3>T</h3><blockquote>Q</blockquote><ul><li>a</li></ul><ol><li>b</li></ol>"
    assert sanitize_html(html) == html


def test_br_is_kept_as_a_void_tag():
    assert sanitize_html("a<br>b") == "a<br>b"


def test_script_tag_and_its_content_are_dropped():
    assert sanitize_html("<p>keep</p><script>alert(1)</script>") == "<p>keep</p>"


def test_style_tag_and_its_content_are_dropped():
    assert sanitize_html("<style>body{color:red}</style><p>keep</p>") == "<p>keep</p>"


def test_iframe_and_content_are_dropped():
    assert sanitize_html('<iframe src="evil"></iframe><p>keep</p>') == "<p>keep</p>"


def test_unknown_wrapping_tags_are_unwrapped_not_content():
    # <div>/<span>/<font> aren't in the toolbar this app offers, but a
    # direct API write with them shouldn't lose the user's actual words.
    assert sanitize_html('<div class="x">kept text</div>') == "kept text"
    assert sanitize_html("<font color=red>kept</font>") == "kept"


def test_onclick_and_other_event_handler_attributes_are_stripped():
    out = sanitize_html('<p onclick="evil()" onmouseover="evil()">safe</p>')
    assert out == "<p>safe</p>"


def test_style_and_class_attributes_are_stripped_from_allowed_tags():
    assert sanitize_html('<p style="color:red" class="x">t</p>') == "<p>t</p>"


def test_link_with_http_href_gets_target_and_rel_added():
    out = sanitize_html('<a href="https://example.com">go</a>')
    assert out == '<a href="https://example.com" target="_blank" rel="noopener noreferrer">go</a>'


def test_link_with_mailto_href_is_kept():
    out = sanitize_html('<a href="mailto:a@b.com">mail</a>')
    assert 'href="mailto:a@b.com"' in out


def test_link_with_javascript_href_drops_the_href_but_keeps_the_text():
    # the <a> tag itself is allowed (it's in the toolbar) - only the unsafe
    # href attribute is stripped, leaving inert non-clickable text behind.
    out = sanitize_html('<a href="javascript:alert(1)">click</a>')
    assert "javascript:" not in out
    assert out == "<a>click</a>"


def test_image_with_http_src_is_kept():
    out = sanitize_html('<img src="https://example.com/x.png" alt="pic">')
    assert out == '<img src="https://example.com/x.png" alt="pic">'


def test_image_with_data_image_src_is_kept():
    out = sanitize_html('<img src="data:image/png;base64,AAAA">')
    assert 'src="data:image/png;base64,AAAA"' in out


def test_image_with_data_text_html_src_is_dropped():
    out = sanitize_html('<img src="data:text/html,<script>alert(1)</script>">')
    assert "<img" not in out


def test_image_with_data_svg_src_is_dropped():
    # SVG can carry its own <script>/onload - the app's own pipeline never
    # produces this (canvas re-encodes to png/jpeg only), so it's excluded
    # even though it's technically an "image/*" data URL.
    svg = "data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+PC9zdmc+"
    assert "<img" not in sanitize_html(f'<img src="{svg}">')


def test_image_with_data_gif_or_webp_src_is_kept():
    assert 'src="data:image/gif;base64,AAAA"' in sanitize_html('<img src="data:image/gif;base64,AAAA">')
    assert 'src="data:image/webp;base64,AAAA"' in sanitize_html('<img src="data:image/webp;base64,AAAA">')


def test_image_data_src_scheme_check_is_case_insensitive():
    out = sanitize_html('<img src="DATA:IMAGE/PNG;base64,AAAA">')
    assert "<img" in out


def test_image_with_javascript_src_is_dropped():
    assert "<img" not in sanitize_html('<img src="javascript:alert(1)">')


def test_attribute_values_are_escaped_against_quote_breakout():
    out = sanitize_html('<a href=\'https://example.com/"><script>alert(1)</script>\'>x</a>')
    assert "<script>" not in out
    assert "&quot;" in out


def test_text_is_html_escaped_on_output():
    assert sanitize_html("<p>1 &lt; 2 & 3 > 0</p>") == "<p>1 &lt; 2 &amp; 3 &gt; 0</p>"


def test_empty_and_none_like_input():
    assert sanitize_html("") == ""


def test_malformed_markup_does_not_raise():
    sanitize_html("<p><strong>unclosed")
    sanitize_html("<<<not real html>>>")
