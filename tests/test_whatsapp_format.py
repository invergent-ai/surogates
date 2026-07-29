"""Tests for the markdown → WhatsApp markup transcoder.

Written BEFORE the implementation module exists (TDD).
"""

from __future__ import annotations

from surogates.channels.platforms.whatsapp_format import render_whatsapp


# ---------------------------------------------------------------------------
# Emphasis conversion
# ---------------------------------------------------------------------------


class TestEmphasis:
    def test_double_asterisk_becomes_single(self):
        assert render_whatsapp("**bold**") == "*bold*"

    def test_double_underscore_becomes_single_asterisk(self):
        assert render_whatsapp("__bold__") == "*bold*"

    def test_single_asterisk_italic_becomes_underscore(self):
        assert render_whatsapp("*italic*") == "_italic_"

    def test_double_tilde_becomes_single(self):
        assert render_whatsapp("~~struck~~") == "~struck~"

    def test_triple_asterisk_becomes_bold_italic(self):
        # The reference degrades this to "**bold italic**" (a stray asterisk on
        # each side). We emit valid WhatsApp nesting instead.
        assert render_whatsapp("***both***") == "*_both_*"

    def test_plain_text_unchanged(self):
        assert render_whatsapp("just words") == "just words"

    def test_empty_string(self):
        assert render_whatsapp("") == ""


# ---------------------------------------------------------------------------
# Headers and links
# ---------------------------------------------------------------------------


class TestHeadersAndLinks:
    def test_header_becomes_bold(self):
        assert render_whatsapp("# Title") == "*Title*"

    def test_deep_header_becomes_bold(self):
        assert render_whatsapp("###### Deep") == "*Deep*"

    def test_header_only_at_line_start(self):
        assert render_whatsapp("not # a header") == "not # a header"

    def test_link_becomes_text_then_url(self):
        assert render_whatsapp("[docs](https://x.dev)") == "docs (https://x.dev)"

    def test_image_link_drops_bang(self):
        # The reference emits "!alt (url)". We drop the bang.
        assert render_whatsapp("![alt](https://x.dev/i.png)") == "alt (https://x.dev/i.png)"

    def test_url_containing_parenthesis_is_preserved(self):
        # The [^)]+ capture stops at the first ')', but the uncaptured tail
        # passes through literally, so the output is byte-identical.
        src = "[wiki](https://x.dev/a(b))"
        assert "https://x.dev/a(b)" in render_whatsapp(src)


# ---------------------------------------------------------------------------
# Code protection — the sentinel technique
# ---------------------------------------------------------------------------


class TestCodeProtection:
    def test_fenced_block_contents_untouched(self):
        src = "```\n**not bold**\n```"
        assert "**not bold**" in render_whatsapp(src)

    def test_inline_code_contents_untouched(self):
        assert render_whatsapp("`**raw**`") == "`**raw**`"

    def test_text_outside_fence_still_converted(self):
        src = "**yes**\n```\n**no**\n```\n**yes**"
        out = render_whatsapp(src)
        assert out.startswith("*yes*")
        assert out.endswith("*yes*")
        assert "**no**" in out

    def test_eleven_fences_restore_in_order(self):
        # Guards the trailing-\x00 sentinel delimiter: without it, restoring
        # placeholder 1 corrupts placeholder 11.
        src = "\n".join(f"`c{i}`" for i in range(12))
        out = render_whatsapp(src)
        for i in range(12):
            assert f"`c{i}`" in out
        assert "\x00" not in out
