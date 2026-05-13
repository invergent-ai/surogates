"""Tests for ``_attachments_note`` in the harness loop.

The helper mirrors :func:`surogates.harness.loop._view_context_note`: it
reads the latest ``user.message`` event's ``data.attachments`` list and
builds a deterministic per-turn system note that names every attached
file by path, MIME type, size, and original filename.  The note is not
persisted -- it is recomputed each iteration from the durable event log
so retries are deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import _attachments_note, _view_context_note
from surogates.session.events import EventType


def _user_event(data: dict, event_id: int = 1):
    return SimpleNamespace(
        type=EventType.USER_MESSAGE,
        data=data,
        eventId=event_id,
    )


def _generic_event(event_type: EventType, event_id: int = 1):
    return SimpleNamespace(type=event_type, data={}, eventId=event_id)


def test_attachments_note_returns_none_when_no_user_message():
    assert _attachments_note([]) is None


def test_attachments_note_returns_none_when_only_non_user_events():
    events = [
        _generic_event(EventType.LLM_REQUEST),
        _generic_event(EventType.LLM_RESPONSE),
    ]
    assert _attachments_note(events) is None


def test_attachments_note_returns_none_when_attachments_absent():
    assert _attachments_note([_user_event({"content": "hi"})]) is None


def test_attachments_note_returns_none_when_attachments_empty():
    assert _attachments_note(
        [_user_event({"content": "hi", "attachments": []})],
    ) is None


def test_attachments_note_returns_none_when_attachments_not_a_list():
    assert _attachments_note(
        [_user_event({"content": "hi", "attachments": "oops"})],
    ) is None


def test_attachments_note_renders_path_mime_size_and_filename():
    note = _attachments_note([_user_event({
        "content": "summarize",
        "attachments": [
            {
                "path": "uploads/1715600000-report.pdf",
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "size": 12_300_000,
            },
        ],
    })])
    assert note is not None
    assert "uploads/1715600000-report.pdf" in note
    assert "application/pdf" in note
    assert "12.3 MB" in note
    assert "report.pdf" in note


def test_attachments_note_defaults_mime_to_octet_stream():
    note = _attachments_note([_user_event({
        "attachments": [
            {"path": "uploads/x.bin", "filename": "x.bin", "size": 10},
        ],
    })])
    assert note is not None
    assert "application/octet-stream" in note


def test_attachments_note_handles_missing_size():
    note = _attachments_note([_user_event({
        "attachments": [
            {"path": "uploads/x.bin", "filename": "x.bin"},
        ],
    })])
    assert note is not None
    assert "unknown size" in note


def test_attachments_note_renders_byte_units_below_one_kb():
    note = _attachments_note([_user_event({
        "attachments": [
            {"path": "uploads/tiny", "filename": "tiny", "size": 250},
        ],
    })])
    assert note is not None
    assert "250 B" in note


def test_attachments_note_renders_kb_below_one_mb():
    note = _attachments_note([_user_event({
        "attachments": [
            {"path": "uploads/notes.txt", "filename": "notes.txt", "size": 4200},
        ],
    })])
    assert note is not None
    assert "4.2 KB" in note


def test_attachments_note_renders_gb_above_one_gb():
    note = _attachments_note([_user_event({
        "attachments": [
            {
                "path": "uploads/big.bin",
                "filename": "big.bin",
                "size": 2_400_000_000,
            },
        ],
    })])
    assert note is not None
    assert "2.4 GB" in note


def test_attachments_note_skips_malformed_items_but_keeps_good_ones():
    note = _attachments_note([_user_event({
        "attachments": [
            "not-a-dict",
            {"filename": "no-path.txt"},
            {"path": "uploads/no-filename"},
            {"path": "uploads/good.txt", "filename": "good.txt", "size": 4},
        ],
    })])
    assert note is not None
    assert "good.txt" in note
    assert "no-path" not in note
    assert "no-filename" not in note


def test_attachments_note_returns_none_when_all_items_malformed():
    note = _attachments_note([_user_event({
        "attachments": [{"path": "uploads/no-name"}, "junk"],
    })])
    assert note is None


def test_attachments_note_only_reads_latest_user_message():
    """Older user messages with attachments must not leak into this turn."""
    events = [
        _user_event(
            {
                "content": "first",
                "attachments": [
                    {
                        "path": "uploads/old.txt",
                        "filename": "old.txt",
                        "size": 5,
                    },
                ],
            },
            event_id=1,
        ),
        _user_event(
            {"content": "second"},  # no attachments this turn
            event_id=2,
        ),
    ]
    assert _attachments_note(events) is None


def test_attachments_note_walks_past_non_user_events_to_find_latest():
    """Non-user events between user messages must not break the lookup."""
    events = [
        _user_event(
            {
                "attachments": [
                    {
                        "path": "uploads/a.txt",
                        "filename": "a.txt",
                        "size": 1,
                    },
                ],
            },
            event_id=1,
        ),
        _generic_event(EventType.LLM_REQUEST, event_id=2),
        _generic_event(EventType.LLM_RESPONSE, event_id=3),
    ]
    note = _attachments_note(events)
    assert note is not None
    assert "a.txt" in note


def test_attachments_note_is_deterministic():
    events = [_user_event({
        "attachments": [
            {"path": "uploads/a.txt", "filename": "a.txt", "size": 1},
            {"path": "uploads/b.txt", "filename": "b.txt", "size": 2},
        ],
    })]
    assert _attachments_note(events) == _attachments_note(events)


def test_attachments_note_tolerates_non_dict_event_data():
    """An event whose ``data`` is not a dict must not raise."""
    bad = SimpleNamespace(type=EventType.USER_MESSAGE, data=None, eventId=1)
    assert _attachments_note([bad]) is None


def test_attachments_note_handles_string_event_type():
    """Event type stored as a raw string (no .value attr) is accepted."""
    raw_event = SimpleNamespace(
        type=EventType.USER_MESSAGE.value,
        data={
            "attachments": [
                {"path": "uploads/x.txt", "filename": "x.txt", "size": 7},
            ],
        },
        eventId=1,
    )
    note = _attachments_note([raw_event])
    assert note is not None
    assert "x.txt" in note


def test_attachments_note_orders_files_as_provided():
    note = _attachments_note([_user_event({
        "attachments": [
            {"path": "uploads/first.txt", "filename": "first.txt", "size": 1},
            {"path": "uploads/second.txt", "filename": "second.txt", "size": 2},
        ],
    })])
    assert note is not None
    first_idx = note.index("first.txt")
    second_idx = note.index("second.txt")
    assert first_idx < second_idx


def test_attachments_note_inserts_adjacent_to_user_when_view_context_also_present():
    """When both notes apply, attachments must sit closest to the user message.

    Required ordering in api_messages (top→bottom):
        ..., view_context_note (system), attachments_note (system), user, ...
    """
    events = [_user_event({
        "content": "hi",
        "metadata": {
            "view_context": {"kind": "run", "id": "r1", "name": "demo"},
        },
        "attachments": [
            {"path": "uploads/x.txt", "filename": "x.txt", "size": 5},
        ],
    })]
    view_note = _view_context_note(events)
    att_note = _attachments_note(events)
    assert view_note is not None
    assert att_note is not None

    api_messages = [
        {"role": "system", "content": "you are an agent"},
        {"role": "user", "content": "hi"},
    ]

    def _insert_before_latest_user(content: str) -> None:
        idx = next(
            (
                i
                for i in range(len(api_messages) - 1, -1, -1)
                if api_messages[i]["role"] == "user"
            ),
            None,
        )
        if idx is not None:
            api_messages.insert(idx, {"role": "system", "content": content})

    # Apply the same ordering as the main loop: view_context first,
    # attachments second — so attachments ends up adjacent to user.
    _insert_before_latest_user(view_note)
    _insert_before_latest_user(att_note)

    assert [m["role"] for m in api_messages] == [
        "system",
        "system",
        "system",
        "user",
    ]
    assert api_messages[1]["content"] == view_note
    assert api_messages[2]["content"] == att_note
