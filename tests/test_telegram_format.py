"""Markdown → Telegram-HTML rendering tests."""

from surogates.channels.platforms.telegram_format import render_html, render_plain


def test_escapes_html_entities():
    assert render_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_bold_and_italic():
    assert render_html("**bold** and *ital*") == "<b>bold</b> and <i>ital</i>"
    assert render_html("__bold__ and _ital_") == "<b>bold</b> and <i>ital</i>"


def test_snake_case_not_italicized():
    assert render_html("use tool_name here") == "use tool_name here"


def test_inline_code_escaped_and_wrapped():
    assert render_html("run `a < b`") == "run <code>a &lt; b</code>"


def test_fenced_block():
    out = render_html("```python\nx = 1 < 2\n```")
    assert out == "<pre>x = 1 &lt; 2</pre>"


def test_code_content_not_styled():
    out = render_html("`**not bold**`")
    assert out == "<code>**not bold**</code>"


def test_link_rendering():
    out = render_html("see [docs](https://example.com/a?b=1&c=2)")
    assert out == 'see <a href="https://example.com/a?b=1&amp;c=2">docs</a>'


def test_non_http_link_untouched():
    out = render_html("[x](javascript:alert(1))")
    assert "<a" not in out


def test_heading_becomes_bold_line():
    assert render_html("## Summary\nbody") == "<b>Summary</b>\nbody"


def test_bullets_become_dots():
    assert render_html("- one\n- two") == "• one\n• two"


def test_render_plain_strips_markers():
    text = "**bold** `code` [docs](https://e.com) ## h\n- item"
    out = render_plain(text)
    assert "**" not in out and "`" not in out and "](" not in out
    assert "docs (https://e.com)" in out
    assert "• item" in out
