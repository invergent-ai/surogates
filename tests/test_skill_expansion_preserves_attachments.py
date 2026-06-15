"""Slash-skill / deep-research expansion must keep the current turn's
attachment binding.

When the user attaches a file and invokes a skill (``/my-skill``) without
naming the file, the harness rewrites the user message to the skill body.
Historically that rewrite *replaced* the rebuilt user content outright,
discarding the per-turn attachment note and inlined attachment content
that ``_rebuild_messages`` folds in. The model was then left with no
signal about which file was attached to *this* message and would bind to
an earlier upload still visible in the conversation history.

``build_user_message_dict`` is the shared seam: with ``base_content`` it
swaps in the skill body while preserving the attachment note, inlined
content, and image vision blocks.
"""

from __future__ import annotations

from types import SimpleNamespace

from surogates.harness.loop import AgentHarness
from surogates.harness.loop_context_replay import build_user_message_dict
from surogates.harness.loop_messages import _latest_user_event_data
from surogates.session.events import EventType


def _user_event(data: dict, event_id: int = 1):
    return SimpleNamespace(type=EventType.USER_MESSAGE.value, data=data, id=event_id)


def test_base_content_override_keeps_path_only_attachment_note():
    data = {
        "content": "/holland-report",
        "attachments": [
            {
                "path": "uploads/172-0-holland.pdf",
                "filename": "holland.pdf",
                "mime_type": "application/pdf",
                "size": 4_200_000,
            }
        ],
    }
    skill_body = (
        "The user invoked the `holland-report` skill.\n\n"
        "Use the following skill to handle this request:\n---\n"
        "Summarise the attached document.\n---"
    )

    msg = build_user_message_dict(data, base_content=skill_body)

    assert isinstance(msg["content"], str)
    # Skill body is present...
    assert "Summarise the attached document." in msg["content"]
    # ...and so is the binding to THIS turn's attachment.
    assert "uploads/172-0-holland.pdf" in msg["content"]
    assert "holland.pdf" in msg["content"]
    # The raw slash command text is gone (replaced by the skill body).
    assert "/holland-report" not in msg["content"]


def test_base_content_override_keeps_inlined_attachment_content():
    data = {
        "content": "/summarize",
        "attachments": [
            {
                "path": "uploads/1-0-notes.txt",
                "filename": "notes.txt",
                "mime_type": "text/plain",
                "inlined_text": "HOLLAND QUARTERLY FIGURES",
                "inlined_render_kind": "text",
            }
        ],
    }

    msg = build_user_message_dict(data, base_content="SKILL BODY HERE")

    assert "SKILL BODY HERE" in msg["content"]
    assert "HOLLAND QUARTERLY FIGURES" in msg["content"]


def test_base_content_override_keeps_image_vision_blocks():
    data = {
        "content": "/describe",
        "images": [
            {"data": "data:image/png;base64,AAAA", "mime_type": "image/png"},
        ],
    }

    msg = build_user_message_dict(data, base_content="SKILL BODY")

    assert isinstance(msg["content"], list)
    text_block = next(p for p in msg["content"] if p.get("type") == "text")
    assert "SKILL BODY" in text_block["text"]
    assert any(p.get("type") == "image_url" for p in msg["content"])


def test_default_matches_rebuild_messages_for_a_single_user_event():
    data = {
        "content": "summarise this",
        "attachments": [
            {
                "path": "uploads/x.pdf",
                "filename": "x.pdf",
                "mime_type": "application/pdf",
                "size": 1234,
            }
        ],
    }
    event = _user_event(data)

    rebuilt = AgentHarness._rebuild_messages(SimpleNamespace(), [event])
    direct = build_user_message_dict(data)

    assert rebuilt[0] == direct


def test_latest_user_event_data_returns_latest_user_payload():
    events = [
        _user_event({"content": "first", "attachments": [{"path": "a"}]}, 1),
        SimpleNamespace(type=EventType.LLM_RESPONSE.value, data={}, id=2),
        _user_event({"content": "second", "attachments": [{"path": "b"}]}, 3),
    ]
    data = _latest_user_event_data(events)
    assert data is not None
    assert data["content"] == "second"
    assert data["attachments"] == [{"path": "b"}]


def test_latest_user_event_data_none_when_no_user_message():
    assert _latest_user_event_data([]) is None
