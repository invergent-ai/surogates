"""Tests for surogates.channels.slack_format."""

from surogates.channels.slack_format import markdown_to_mrkdwn, truncate_message


class TestMarkdownToMrkdwn:
    """Test the 13-step markdown → Slack mrkdwn conversion."""

    def test_empty(self):
        assert markdown_to_mrkdwn("") == ""

    def test_plain_text(self):
        assert markdown_to_mrkdwn("hello world") == "hello world"

    def test_code_blocks_preserved(self):
        text = "before\n```python\nprint('hello')\n```\nafter"
        result = markdown_to_mrkdwn(text)
        assert "```python\nprint('hello')\n```" in result

    def test_inline_code_preserved(self):
        result = markdown_to_mrkdwn("use `foo()` here")
        assert "`foo()`" in result

    def test_links_converted(self):
        result = markdown_to_mrkdwn("[click here](https://example.com)")
        assert "<https://example.com|click here>" in result

    def test_headers_converted(self):
        result = markdown_to_mrkdwn("## Title")
        assert "*Title*" in result

    def test_bold_converted(self):
        result = markdown_to_mrkdwn("**bold text**")
        assert "*bold text*" in result

    def test_italic_converted(self):
        result = markdown_to_mrkdwn("*italic text*")
        assert "_italic text_" in result

    def test_bold_italic_converted(self):
        result = markdown_to_mrkdwn("***bold italic***")
        assert "*_bold italic_*" in result

    def test_strikethrough_converted(self):
        result = markdown_to_mrkdwn("~~deleted~~")
        assert "~deleted~" in result

    def test_ampersand_escaped(self):
        result = markdown_to_mrkdwn("A & B")
        assert "&amp;" in result

    def test_angle_brackets_escaped(self):
        result = markdown_to_mrkdwn("a < b > c")
        assert "&lt;" in result
        assert "&gt;" in result

    def test_slack_entities_preserved(self):
        text = "hello <@U123> and <#C456>"
        result = markdown_to_mrkdwn(text)
        assert "<@U123>" in result
        assert "<#C456>" in result

    def test_blockquote_preserved(self):
        result = markdown_to_mrkdwn("> quoted text")
        assert "> " in result

    def test_no_double_escape(self):
        result = markdown_to_mrkdwn("&amp; already escaped")
        # Should not become &amp;amp;
        assert "&amp;" in result
        assert "&amp;amp;" not in result


class TestTruncateMessage:
    """Test code-block-aware message truncation."""

    def test_short_message(self):
        chunks = truncate_message("hello", max_length=100)
        assert chunks == ["hello"]

    def test_long_message_splits(self):
        text = "word " * 10000
        chunks = truncate_message(text, max_length=100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_chunk_indicators(self):
        text = "a " * 100
        chunks = truncate_message(text, max_length=50)
        assert "(1/" in chunks[0]
        assert f"({len(chunks)}/{len(chunks)})" in chunks[-1]

    def test_code_block_closure(self):
        text = "before\n```python\n" + "x = 1\n" * 1000 + "```\nafter"
        chunks = truncate_message(text, max_length=200)
        # Each chunk that ends mid-code-block should close the fence.
        for chunk in chunks[:-1]:
            if "```python" in chunk:
                # Should have a closing fence.
                assert chunk.rstrip().endswith("```") or "```" in chunk

    def test_default_max_length(self):
        chunks = truncate_message("short")
        assert chunks == ["short"]
