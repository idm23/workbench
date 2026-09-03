"""Markdown-to-HTML rendering of model-authored prose.

The interesting cases here are the sanitizer's, not the parser's: this is
untrusted model output about to be marked `Markup`-safe and dropped into a
page with no other guard between it and the browser, so what stays out
matters more than what the syntax looks like.
"""

from markupsafe import Markup

from workbench.rendering import render_markdown


def test_empty_and_none_render_as_empty():
    assert render_markdown(None) == ""
    assert render_markdown("") == ""


def test_result_is_markup_safe():
    assert isinstance(render_markdown("hello"), Markup)


def test_headings_lists_and_emphasis_become_html():
    html = render_markdown("# Title\n\n- one\n- two\n\nSome **bold** text.")

    assert "<h1>Title</h1>" in html
    assert "<ul>" in html and "<li>one</li>" in html
    assert "<strong>bold</strong>" in html


def test_fenced_code_block_is_preserved_and_escaped():
    html = render_markdown("```python\nprint('<hi>')\n```")

    assert "<pre>" in html and "<code>" in html
    # The angle brackets in the code sample are escaped text, not a tag.
    assert "&lt;hi&gt;" in html
    assert "<hi>" not in html


def test_single_newline_becomes_a_break():
    """Chat-style prose uses single line breaks between sentences that were
    never meant to run together — plain CommonMark would collapse them into
    one paragraph."""
    html = render_markdown("First line.\nSecond line.")

    assert "<br" in html


def test_a_plain_link_is_kept_with_a_safe_rel():
    html = render_markdown("See [the docs](https://example.com/docs).")

    assert 'href="https://example.com/docs"' in html
    assert "noopener" in html


def test_a_script_tag_in_the_source_is_neutralized():
    html = render_markdown("before <script>alert(1)</script> after")

    assert "<script" not in html
    assert "alert(1)" not in html or "&lt;script&gt;" in html


def test_an_inline_event_handler_is_stripped():
    """Raw HTML in the source renders as escaped literal text (html=False),
    so `onerror` surviving as inert page text is fine — what must not survive
    is an actual `<img>` tag with a live `onerror` attribute."""
    html = render_markdown('<img src=x onerror="alert(1)">')

    assert "<img" not in html


def test_a_javascript_url_is_dropped():
    """markdown-it itself refuses to turn a `javascript:` destination into a
    link (it renders as literal bracket text instead), and the sanitizer's
    `url_schemes` allowlist is the backstop if that parser behaviour ever
    changed — either way, no working link should reach the page."""
    html = render_markdown("[click me](javascript:alert(1))")

    assert "<a" not in html or 'href="javascript' not in html


def test_an_h1_from_the_model_does_not_escape_as_a_page_heading():
    """Sanity check on the allowed-tag list: nothing renders outside the
    handful of prose tags Markdown itself can produce."""
    html = render_markdown("<div class='x'>text</div>")

    assert "<div" not in html
    assert "text" in html
