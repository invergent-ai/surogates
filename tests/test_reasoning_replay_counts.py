"""History replay retains the live count of interrupted reasoning."""

from uuid import uuid4

from surogates.db.models import Event
from surogates.session.events import EventType
from surogates.session.store import SessionStore


async def test_replay_recovers_counts_without_replaying_deltas_or_writing_events(sf):
    sid = uuid4()
    other_sid = uuid4()
    async with sf() as db:
        rows = [
            Event(id=1, session_id=sid, type="llm.request", data={}),
            Event(id=2, session_id=sid, type="llm.delta", data={"reasoning": "First"}),
            Event(id=3, session_id=sid, type="llm.delta", data={"reasoning": "Second"}),
            Event(id=4, session_id=sid, type="llm.thinking", data={"reasoning": "FirstSecond"}),
            Event(id=5, session_id=sid, type="llm.request", data={}),
            Event(id=6, session_id=sid, type="llm.delta", data={"reasoning": "Current"}),
            Event(id=7, session_id=other_sid, type="llm.delta", data={"reasoning": "Unrelated"}),
            Event(id=8, session_id=sid, type="llm.delta", data={"content": "Text"}),
            Event(id=9, session_id=sid, type="llm.delta", data={"reasoning": ""}),
            Event(id=10, session_id=sid, type="session.pause", data={}),
            Event(id=11, session_id=sid, type="llm.thinking", data={"reasoning": "Current"}),
        ]
        db.add_all(rows)
        await db.commit()
    store = SessionStore(sf)
    replay = await store.get_events(sid, exclude_types=[EventType.LLM_DELTA])
    thoughts = [e for e in replay if e.type == "llm.thinking"]
    assert [e.data["reasoning_delta_count"] for e in thoughts] == [2, 1]
    assert all(e.type != "llm.delta" for e in replay)
    # A later replay page can recover deltas before its cursor too.
    page = await store.get_events(sid, after=10, exclude_types=[EventType.LLM_DELTA])
    assert page[0].data["reasoning_delta_count"] == 1
    stored = await store.get_event_by_id(sid, 11)
    assert "reasoning_delta_count" not in stored.data


async def test_replay_preserves_counts_already_saved_in_snapshots(sf):
    sid = uuid4()
    async with sf() as db:
        db.add(Event(id=1, session_id=sid, type="llm.thinking", data={
            "reasoning": "Saved", "reasoning_delta_count": 293,
        }))
        db.add(Event(id=2, session_id=sid, type="llm.thinking", data={
            "reasoning": "Reported", "reasoning_tokens": 300,
        }))
        await db.commit()
    replay = await SessionStore(sf).get_events(sid, exclude_types=[EventType.LLM_DELTA])
    assert replay[0].data["reasoning_delta_count"] == 293
    assert replay[1].data["reasoning_tokens"] == 300
