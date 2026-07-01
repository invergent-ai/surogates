"""Channel sessions always resume the same session.

An inbound channel message reuses the most-recent session for its routing key
even when that session has ``completed`` — re-activating it so the harness
replays the full prior conversation instead of starting a fresh session per
message. A ``failed`` session is excluded (start fresh after a failure), and a
key with no prior session creates a new one.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy.dialects import postgresql

from surogates.channels.identity import get_or_create_channel_session
from surogates.session.events import EventType


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DB:
    def __init__(self, value):
        self._value = value
        self.stmt = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, stmt, *a, **k):
        self.stmt = stmt
        return _Result(self._value)


class _SessionFactory:
    def __init__(self, value):
        self.db = _DB(value)

    def __call__(self):
        return self.db


class _Store:
    def __init__(self):
        self.created = None
        self.status_updates: list = []
        self.events: list = []

    async def create_session(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(**kwargs)

    async def update_session_status(self, session_id, status):
        self.status_updates.append((session_id, status))

    async def emit_event(self, session_id, event_type, data):
        self.events.append((session_id, event_type, data))


async def _call(store, factory):
    return await get_or_create_channel_session(
        store, None,
        session_key="agent:slack:dm:D1",
        user_id=uuid4(), org_id=uuid4(), agent_id="a1",
        channel="slack", config={"slack_channel_id": "D1"},
        session_factory=factory,
    )


async def test_resumes_completed_session():
    sid = uuid4()
    store = _Store()
    got = await _call(store, _SessionFactory(SimpleNamespace(id=sid, status="completed")))
    assert got == sid                          # reused, not a new session
    assert store.created is None               # nothing created
    assert store.status_updates == [(sid, "active")]   # re-activated for resume
    assert store.events == [(sid, EventType.SESSION_RESUME, {})]  # resume emitted


async def test_reuses_active_session_without_reactivation():
    sid = uuid4()
    store = _Store()
    got = await _call(store, _SessionFactory(SimpleNamespace(id=sid, status="active")))
    assert got == sid
    assert store.created is None
    assert store.status_updates == []          # already active — no status change
    assert store.events == []                  # no resume event needed


async def test_creates_when_no_prior_session():
    store = _Store()
    got = await _call(store, _SessionFactory(None))
    assert store.created is not None
    assert got == store.created["session_id"]
    assert store.status_updates == []


async def test_lookup_includes_completed_excludes_failed():
    factory = _SessionFactory(None)
    await _call(_Store(), factory)
    sql = str(factory.db.stmt.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True},
    ))
    assert "completed" in sql          # a completed session is now reusable
    assert "failed" not in sql         # a failed session is not resumed
