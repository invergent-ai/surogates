"""Channel sessions always resume the same session.

An inbound channel message reuses the most-recent session for its routing key,
re-activating an idle (completed/paused) one so the harness continues the
conversation. A failed/terminal most-recent session — even with an older
completed one behind it — starts fresh, and a key with no prior session creates
one.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *a, **k):
        return _Result(self._value)


class _SessionFactory:
    def __init__(self, value):
        self._value = value

    def __call__(self):
        return _DB(self._value)


class _Store:
    def __init__(self):
        self.created = None
        self.resumed: list = []
        self.config_updates: list = []

    async def create_session(self, **kwargs):
        self.created = kwargs
        return SimpleNamespace(**kwargs)

    async def resume_session(self, session_id, *, source=""):
        self.resumed.append((session_id, source))

    async def update_session_config_key(self, session_id, key, value):
        self.config_updates.append((session_id, key, value))


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
    assert got == sid
    assert store.created is None
    assert store.resumed == [(sid, "channel_message")]


async def test_resumes_paused_session():
    # A paused session must also be re-activated on a new message — otherwise the
    # harness's paused branch is a hard stop and the message is stranded.
    sid = uuid4()
    store = _Store()
    got = await _call(store, _SessionFactory(SimpleNamespace(id=sid, status="paused")))
    assert got == sid
    assert store.created is None
    assert store.resumed == [(sid, "channel_message")]


async def test_reuses_active_session_without_resume():
    sid = uuid4()
    store = _Store()
    got = await _call(store, _SessionFactory(SimpleNamespace(id=sid, status="active")))
    assert got == sid
    assert store.created is None
    assert store.resumed == []          # already active — no resume needed


async def test_failed_most_recent_starts_fresh():
    # The most-recent session for the key failed → do NOT resume it (nor an older
    # completed one behind it, since the query no longer filters status): fresh.
    store = _Store()
    got = await _call(store, _SessionFactory(SimpleNamespace(id=uuid4(), status="failed")))
    assert store.created is not None
    assert got == store.created["session_id"]
    assert store.resumed == []


async def test_creates_when_no_prior_session():
    store = _Store()
    got = await _call(store, _SessionFactory(None))
    assert store.created is not None
    assert got == store.created["session_id"]
    assert store.resumed == []


async def test_resume_session_helper_flips_status_and_tags_source():
    from surogates.session.store import SessionStore

    store = SessionStore.__new__(SessionStore)   # bypass __init__/DB
    calls: list = []

    async def _upd(sid, status):
        calls.append(("status", sid, status))

    async def _emit(sid, et, data):
        calls.append(("event", sid, et, data))

    store.update_session_status = _upd
    store.emit_event = _emit
    sid = uuid4()
    await store.resume_session(sid, source="channel_message")
    assert calls == [
        ("status", sid, "active"),
        ("event", sid, EventType.SESSION_RESUME, {"source": "channel_message"}),
    ]


async def _call_group(store, factory, config):
    return await get_or_create_channel_session(
        store, None,
        session_key="agent:slack:channel:G1",
        user_id=uuid4(), org_id=uuid4(), agent_id="a1",
        channel="slack", config=config,
        session_factory=factory,
    )


async def test_backfills_memory_and_workspace_boundary_on_resume():
    sid = uuid4()
    store = _Store()

    got = await _call_group(
        store,
        _SessionFactory(SimpleNamespace(id=sid, status="active", config={})),
        {
            "slack_channel_id": "G1",
            "memory_boundary": "slack:c:G1",
            "multi_party": True,
        },
    )

    assert got == sid
    assert (sid, "memory_boundary", "slack:c:G1") in store.config_updates
    assert (sid, "workspace_boundary", "slack:c:G1") in store.config_updates


async def test_does_not_backfill_workspace_boundary_for_non_managed_channel_without_boundary():
    sid = uuid4()
    store = _Store()

    got = await get_or_create_channel_session(
        store,
        None,
        session_key="agent:website:visitor:v1",
        user_id=uuid4(),
        org_id=uuid4(),
        agent_id="a1",
        channel="website",
        config={},
        session_factory=_SessionFactory(SimpleNamespace(id=sid, status="active", config={})),
    )

    assert got == sid
    assert all(key != "workspace_boundary" for _, key, _ in store.config_updates)
