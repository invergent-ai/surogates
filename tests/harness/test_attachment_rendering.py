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
    assert "parsed via liteparse" in out
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
    assert "parsed via liteparse" not in out  # no parser subtitle for text
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


# ---------------------------------------------------------------------------
# Task 8 — revised _attachments_note
# ---------------------------------------------------------------------------


def test_attachments_note_returns_none_when_all_inlined() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _attachments_note
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "x",
                "attachments": [
                    {
                        "path": "uploads/a.pdf", "filename": "a.pdf",
                        "mime_type": "application/pdf", "size": 1000,
                        "inlined_text": "BODY", "inlined_render_kind": "markdown",
                    }
                ],
            },
            id=1,
        ),
    ]
    assert _attachments_note(events) is None


def test_attachments_note_lists_only_path_only_attachments() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _attachments_note
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "x",
                "attachments": [
                    {
                        "path": "uploads/inlined.pdf",
                        "filename": "inlined.pdf",
                        "mime_type": "application/pdf", "size": 1000,
                        "inlined_text": "BODY",
                        "inlined_render_kind": "markdown",
                    },
                    {
                        "path": "uploads/big.pdf",
                        "filename": "big.pdf",
                        "mime_type": "application/pdf",
                        "size": 9_000_000,
                    },
                ],
            },
            id=1,
        ),
    ]
    note = _attachments_note(events)
    assert note is not None
    assert "big.pdf" in note
    assert "inlined.pdf" not in note


def test_attachments_note_includes_skip_reason_diagnostic() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _attachments_note
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "x",
                "attachments": [
                    {
                        "path": "uploads/corrupt.pdf",
                        "filename": "corrupt.pdf",
                        "mime_type": "application/pdf",
                        "size": 1000,
                        "inline_skip_reason": "parse_error",
                    },
                ],
            },
            id=1,
        ),
    ]
    note = _attachments_note(events)
    assert note is not None
    assert "corrupt.pdf" in note
    assert "parse_error" in note
    assert "read_file" in note.lower()


# ---------------------------------------------------------------------------
# Raw user-message text used for slash-command dispatch
# ---------------------------------------------------------------------------


def test_latest_user_event_text_returns_raw_content() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _latest_user_event_text
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={"content": "/foo bar"},
            id=1,
        ),
    ]
    assert _latest_user_event_text(events) == "/foo bar"


def test_latest_user_event_text_ignores_path_only_attachments() -> None:
    """Regression: a /command with path-only attachments must still be
    detected as starting with ``/``.

    Previously the harness derived ``last_user_content`` from the rebuilt
    message, which has the attachments-note prepended; that pushed the
    ``/`` off the start and silently disabled slash-command dispatch
    whenever a message carried a path-only attachment (e.g. a PDF too
    large to inline).
    """
    from types import SimpleNamespace
    from surogates.harness.loop import _latest_user_event_text
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": "/puncte-agoa-writer creaza documentul",
                "attachments": [
                    {
                        "path": "uploads/report.pdf",
                        "filename": "report.pdf",
                        "mime_type": "application/pdf",
                        "size": 9_000_000,
                        "inline_skip_reason": "total_budget_exceeded",
                    },
                ],
            },
            id=1,
        ),
    ]
    text = _latest_user_event_text(events)
    assert text.startswith("/")
    assert text == "/puncte-agoa-writer creaza documentul"


def test_latest_user_event_text_returns_last_user_event() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _latest_user_event_text
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={"content": "first"},
            id=1,
        ),
        SimpleNamespace(
            type=EventType.LLM_RESPONSE.value,
            data={"message": {"role": "assistant", "content": "reply"}},
            id=2,
        ),
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={"content": "/clear"},
            id=3,
        ),
    ]
    assert _latest_user_event_text(events) == "/clear"


def test_latest_user_event_text_strips_outer_whitespace() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _latest_user_event_text
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={"content": "   /foo   "},
            id=1,
        ),
    ]
    assert _latest_user_event_text(events) == "/foo"


def test_latest_user_event_text_handles_list_content() -> None:
    from types import SimpleNamespace
    from surogates.harness.loop import _latest_user_event_text
    from surogates.session.events import EventType

    events = [
        SimpleNamespace(
            type=EventType.USER_MESSAGE.value,
            data={
                "content": [
                    {"type": "text", "text": "/foo bar"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            },
            id=1,
        ),
    ]
    assert _latest_user_event_text(events) == "/foo bar"


def test_latest_user_event_text_returns_empty_when_no_user_event() -> None:
    from surogates.harness.loop import _latest_user_event_text

    assert _latest_user_event_text([]) == ""
