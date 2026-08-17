"""Seeded turns become real events without waking the worker."""
from __future__ import annotations

import pytest

from surogates.api.routes.sessions import SeedTurn, seed_turn_events
from surogates.session.events import EventType


def test_user_turn_becomes_a_user_message_event():
    events = seed_turn_events([SeedTurn(role="user", content="2+2?")])
    assert events == [(EventType.USER_MESSAGE, {"content": "2+2?"})]


def test_assistant_turn_becomes_an_llm_response_event():
    # The response contract is {"message": {"content": ...}}; a bare
    # {"content": ...} would not be read back as an assistant message.
    events = seed_turn_events([SeedTurn(role="assistant", content="4")])
    assert events == [(EventType.LLM_RESPONSE, {"message": {"content": "4"}})]


def test_order_is_preserved():
    events = seed_turn_events([
        SeedTurn(role="user", content="one"),
        SeedTurn(role="assistant", content="two"),
        SeedTurn(role="user", content="three"),
    ])
    assert [t for t, _ in events] == [
        EventType.USER_MESSAGE,
        EventType.LLM_RESPONSE,
        EventType.USER_MESSAGE,
    ]


def test_empty_seed_produces_no_events():
    assert seed_turn_events([]) == []
    assert seed_turn_events(None) == []


def test_unknown_role_is_rejected():
    with pytest.raises(ValueError):
        SeedTurn(role="system", content="x")


class _RecordingStore:
    def __init__(self):
        self.emitted = []

    async def emit_event(self, session_id, event_type, data):
        self.emitted.append((event_type, data))
        return len(self.emitted)


async def test_seeded_turns_are_emitted_in_order():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(
        store,
        session_id="s-1",
        turns=[
            SeedTurn(role="user", content="one"),
            SeedTurn(role="assistant", content="two"),
        ],
    )
    assert store.emitted == [
        (EventType.USER_MESSAGE, {"content": "one"}),
        (EventType.LLM_RESPONSE, {"message": {"content": "two"}}),
    ]


async def test_no_seed_emits_nothing():
    from surogates.api.routes.sessions import emit_seed_turns

    store = _RecordingStore()
    await emit_seed_turns(store, session_id="s-1", turns=None)
    assert store.emitted == []


def test_web_create_session_does_not_seed():
    # Only create_api_session seeds. The web route builds a session for a
    # human, where a caller-written transcript would be a forgery.
    import inspect

    from surogates.api.routes import sessions

    assert "emit_seed_turns" not in inspect.getsource(sessions.create_session)
