"""Unit tests for the inline-attachment renderer in harness/loop.py."""

from __future__ import annotations


def test_render_inlined_appends_fenced_block_for_markdown_kind() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "summarise this",
        [
            {
                "path": "uploads/report.pdf",
                "filename": "report.pdf",
                "inlined_text": "# Heading\n| col1 | col2 |\n| ---- | ---- |\n| a | b |",
                "inlined_render_kind": "markdown",
            }
        ],
    )
    assert out.startswith("summarise this")
    assert "**Attachment: report.pdf**" in out
    assert "parsed via markitdown/pymupdf4llm" in out
    assert "read_file(\"uploads/report.pdf\")" in out
    assert "# Heading" in out


def test_render_inlined_text_kind_omits_parser_subtitle() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "look at this",
        [
            {
                "path": "uploads/notes.md",
                "filename": "notes.md",
                "inlined_text": "# notes\nhello",
                "inlined_render_kind": "text",
            }
        ],
    )
    assert "**Attachment: notes.md**" in out
    assert "parsed via markitdown" not in out  # no parser subtitle for text
    assert "# notes" in out


def test_render_inlined_handles_multiple_attachments_in_order() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "compare these",
        [
            {
                "path": "uploads/a.pdf", "filename": "a.pdf",
                "inlined_text": "ALPHA", "inlined_render_kind": "markdown",
            },
            {
                "path": "uploads/b.pdf", "filename": "b.pdf",
                "inlined_text": "BRAVO", "inlined_render_kind": "markdown",
            },
        ],
    )
    assert out.index("ALPHA") < out.index("BRAVO")
    assert out.index("**Attachment: a.pdf**") < out.index("**Attachment: b.pdf**")


def test_render_inlined_skips_path_only_attachments() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    out = _render_inlined_attachments(
        "look at these",
        [
            {
                "path": "uploads/inlined.pdf", "filename": "inlined.pdf",
                "inlined_text": "INLINED", "inlined_render_kind": "markdown",
            },
            {
                # No inlined_text → should not appear in the rendered text.
                "path": "uploads/huge.pdf", "filename": "huge.pdf",
            },
        ],
    )
    assert "INLINED" in out
    assert "huge.pdf" not in out


def test_render_inlined_returns_content_unchanged_when_nothing_to_inline() -> None:
    from surogates.harness.loop import _render_inlined_attachments

    assert _render_inlined_attachments("hi", []) == "hi"
    assert _render_inlined_attachments("hi", None) == "hi"
    assert _render_inlined_attachments("hi", [{"path": "x", "filename": "x"}]) == "hi"


def test_rebuild_messages_inlines_attachment_into_user_message() -> None:
    from types import SimpleNamespace

    from surogates.harness.loop import AgentHarness
    from surogates.session.events import EventType

    user_event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={
            "content": "summarise this",
            "attachments": [
                {
                    "path": "uploads/r.pdf",
                    "filename": "r.pdf",
                    "inlined_text": "INLINE BODY",
                    "inlined_render_kind": "markdown",
                }
            ],
        },
        id=1,
    )

    # _rebuild_messages is a method but doesn't touch ``self`` in the
    # user-message branch -- a bare SimpleNamespace satisfies it.
    self_stub = SimpleNamespace()
    messages = AgentHarness._rebuild_messages(self_stub, [user_event])
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    text = messages[0]["content"]
    assert isinstance(text, str)
    assert "summarise this" in text
    assert "**Attachment: r.pdf**" in text
    assert "INLINE BODY" in text


def test_rebuild_messages_leaves_user_message_unchanged_when_no_attachments() -> None:
    from types import SimpleNamespace

    from surogates.harness.loop import AgentHarness
    from surogates.session.events import EventType

    user_event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={"content": "just text"},
        id=1,
    )
    messages = AgentHarness._rebuild_messages(SimpleNamespace(), [user_event])
    assert messages == [{"role": "user", "content": "just text"}]
