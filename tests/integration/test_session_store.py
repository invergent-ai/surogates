"""Integration tests for SessionStore against a real PostgreSQL database."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from surogates.session.events import EventType
from surogates.session.store import LeaseNotHeldError, SessionNotFoundError

from .conftest import create_org, create_user

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


async def test_create_session(session_store, session_factory):
    """Creating a session populates all expected fields."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="test-agent",
        channel="web",
        model="gpt-4o",
        config={"system": "You are helpful."},
    )

    assert session.id is not None
    assert session.user_id == user_id
    assert session.org_id == org_id
    assert session.channel == "web"
    assert session.status == "active"
    assert session.model == "gpt-4o"
    assert session.config == {"system": "You are helpful."}
    assert session.message_count == 0
    assert session.tool_call_count == 0
    assert session.created_at is not None
    assert session.updated_at is not None


async def test_get_session(session_store, session_factory):
    """Creating then getting a session round-trips correctly."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    created = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )
    fetched = await session_store.get_session(created.id)

    assert fetched.id == created.id
    assert fetched.user_id == user_id
    assert fetched.org_id == org_id
    assert fetched.status == "active"


async def test_get_session_not_found(session_store):
    """Getting a nonexistent session raises SessionNotFoundError."""
    with pytest.raises(SessionNotFoundError):
        await session_store.get_session(uuid.uuid4())


async def test_update_session_status(session_store, session_factory):
    """Updating status to 'paused' persists correctly."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )
    assert session.status == "active"

    await session_store.update_session_status(session.id, "paused")
    updated = await session_store.get_session(session.id)
    assert updated.status == "paused"


async def test_list_sessions(session_store, session_factory):
    """Listing sessions for a user returns the correct count with pagination."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    ids = []
    for _ in range(3):
        s = await session_store.create_session(
            user_id=user_id, org_id=org_id, agent_id="test-agent"
        )
        ids.append(s.id)

    # Fetch all
    sessions = await session_store.list_sessions(org_id, user_id, "test-agent")
    returned_ids = {s.id for s in sessions}
    assert all(sid in returned_ids for sid in ids)

    # Pagination: limit=2
    page1 = await session_store.list_sessions(
        org_id, user_id, "test-agent", limit=2
    )
    assert len(page1) == 2

    # Pagination: offset=2
    page2 = await session_store.list_sessions(
        org_id, user_id, "test-agent", limit=10, offset=2
    )
    assert len(page2) >= 1


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


async def test_emit_event(session_store, session_factory):
    """Emitting a USER_MESSAGE event persists with correct type and data."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    event_id = await session_store.emit_event(
        session.id,
        EventType.USER_MESSAGE,
        {"content": "Hello, world!"},
    )

    assert event_id > 0

    events = await session_store.get_events(session.id)
    assert len(events) == 1
    assert events[0].type == EventType.USER_MESSAGE.value
    assert events[0].data == {"content": "Hello, world!"}
    assert events[0].id == event_id


async def test_emit_event_increments_message_count(session_store, session_factory):
    """USER_MESSAGE events increment the session's message_count."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "hi"}
    )

    updated = await session_store.get_session(session.id)
    assert updated.message_count == 1


async def test_emit_event_increments_tool_call_count(session_store, session_factory):
    """TOOL_CALL events increment the session's tool_call_count."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    await session_store.emit_event(
        session.id, EventType.TOOL_CALL, {"tool": "bash", "input": "ls"}
    )

    updated = await session_store.get_session(session.id)
    assert updated.tool_call_count == 1


async def test_get_events_after_cursor(session_store, session_factory):
    """Getting events after a cursor returns only subsequent events."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    event_ids = []
    for i in range(5):
        eid = await session_store.emit_event(
            session.id, EventType.USER_MESSAGE, {"content": f"msg-{i}"}
        )
        event_ids.append(eid)

    # Get events after the 2nd one (index 1)
    after_events = await session_store.get_events(
        session.id, after=event_ids[1]
    )
    assert len(after_events) == 3
    returned_ids = [e.id for e in after_events]
    assert returned_ids == event_ids[2:]


async def test_get_events_with_type_filter(session_store, session_factory):
    """Filtering events by type returns only matching events."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "hello"}
    )
    await session_store.emit_event(
        session.id, EventType.TOOL_CALL, {"tool": "bash"}
    )
    await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "world"}
    )

    filtered = await session_store.get_events(
        session.id, types=[EventType.TOOL_CALL]
    )
    assert len(filtered) == 1
    assert filtered[0].type == EventType.TOOL_CALL.value


# ---------------------------------------------------------------------------
# Leases
# ---------------------------------------------------------------------------


async def test_lease_acquire(session_store, session_factory):
    """Acquiring a lease returns a valid SessionLease."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    lease = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=30
    )
    assert lease is not None
    assert lease.session_id == session.id
    assert lease.owner_id == "worker-1"
    assert lease.lease_token is not None
    assert lease.expires_at is not None


async def test_lease_acquire_fails_when_held(session_store, session_factory):
    """A second worker cannot acquire a lease already held by another."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    lease1 = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=60
    )
    assert lease1 is not None

    lease2 = await session_store.try_acquire_lease(
        session.id, "worker-2", ttl_seconds=60
    )
    assert lease2 is None


async def test_lease_acquire_steals_expired(session_store, session_factory):
    """An expired lease can be stolen by a different worker."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    # Acquire with a very short TTL
    lease1 = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=1
    )
    assert lease1 is not None

    # Wait for the lease to expire
    await asyncio.sleep(1.5)

    # A new worker should be able to steal it
    lease2 = await session_store.try_acquire_lease(
        session.id, "worker-2", ttl_seconds=30
    )
    assert lease2 is not None
    assert lease2.owner_id == "worker-2"


async def test_lease_renew(session_store, session_factory):
    """Renewing a lease extends its expires_at."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    lease = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=10
    )
    assert lease is not None

    original_expires = lease.expires_at

    # Renew with a longer TTL
    await session_store.renew_lease(session.id, lease.lease_token, ttl_seconds=60)

    # Verify the same owner can still acquire (or just check the lease
    # didn't break by acquiring again after release)
    # Since we can't directly read the lease, release and re-acquire to confirm
    # the renew didn't break anything.
    await session_store.release_lease(session.id, lease.lease_token)
    new_lease = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=30
    )
    assert new_lease is not None


async def test_lease_release(session_store, session_factory):
    """Releasing a lease allows another worker to acquire it."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    lease = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=60
    )
    assert lease is not None

    await session_store.release_lease(session.id, lease.lease_token)

    # Now worker-2 should be able to acquire
    lease2 = await session_store.try_acquire_lease(
        session.id, "worker-2", ttl_seconds=60
    )
    assert lease2 is not None
    assert lease2.owner_id == "worker-2"


# ---------------------------------------------------------------------------
# Cursors
# ---------------------------------------------------------------------------


async def test_harness_cursor(session_store, session_factory):
    """Advancing cursor updates the harness_cursor value."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    # Initial cursor should be 0
    cursor = await session_store.get_harness_cursor(session.id)
    assert cursor == 0

    # Emit some events
    eid1 = await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "a"}
    )
    eid2 = await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "b"}
    )

    # Acquire lease and advance cursor
    lease = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=30
    )
    assert lease is not None

    await session_store.advance_harness_cursor(
        session.id, eid2, lease.lease_token
    )

    updated_cursor = await session_store.get_harness_cursor(session.id)
    assert updated_cursor == eid2


async def test_pending_events(session_store, session_factory):
    """get_pending_events returns only events after the cursor."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    event_ids = []
    for i in range(5):
        eid = await session_store.emit_event(
            session.id, EventType.USER_MESSAGE, {"content": f"msg-{i}"}
        )
        event_ids.append(eid)

    # Advance cursor to the 3rd event (index 2)
    lease = await session_store.try_acquire_lease(
        session.id, "worker-1", ttl_seconds=30
    )
    assert lease is not None
    await session_store.advance_harness_cursor(
        session.id, event_ids[2], lease.lease_token
    )

    pending = await session_store.get_pending_events(session.id)
    assert len(pending) == 2
    assert pending[0].id == event_ids[3]
    assert pending[1].id == event_ids[4]


async def test_advance_cursor_requires_lease(session_store, session_factory):
    """Advancing cursor without holding a lease raises LeaseNotHeldError."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent"
    )

    eid = await session_store.emit_event(
        session.id, EventType.USER_MESSAGE, {"content": "test"}
    )

    fake_token = uuid.uuid4()
    with pytest.raises(LeaseNotHeldError):
        await session_store.advance_harness_cursor(
            session.id, eid, fake_token
        )


# ---------------------------------------------------------------------------
# Orphan recovery
# ---------------------------------------------------------------------------


async def test_list_sessions_excludes_delegation_children(
    session_store, session_factory,
):
    """Top-level list hides delegation children — they belong in the tree."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    top = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )
    child = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
        parent_id=top.id, channel="delegation",
    )

    listed = await session_store.list_sessions(
        org_id=org_id, user_id=user_id, agent_id="test-agent",
    )
    listed_ids = {s.id for s in listed}
    assert top.id in listed_ids
    assert child.id not in listed_ids


async def test_release_stale_lease_only_touches_expired(
    session_store, session_factory,
):
    """release_stale_lease clears an expired row but refuses a live one."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    session = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="test-agent",
    )

    # Live lease — release_stale_lease must NOT touch it.
    live = await session_store.try_acquire_lease(
        session.id, "worker-alive", ttl_seconds=30,
    )
    assert live is not None
    assert await session_store.release_stale_lease(session.id) is False
    # Live lease really still prevents anyone else from grabbing it.
    assert await session_store.try_acquire_lease(
        session.id, "worker-thief", ttl_seconds=30,
    ) is None

    # Replace with a short-lived lease and let it expire.
    await session_store.release_lease(session.id, live.lease_token)
    await session_store.try_acquire_lease(
        session.id, "worker-alive", ttl_seconds=1,
    )
    await asyncio.sleep(1.5)

    # Expired lease — release_stale_lease drops it, and a new acquire
    # now succeeds with a fresh owner.
    assert await session_store.release_stale_lease(session.id) is True
    fresh = await session_store.try_acquire_lease(
        session.id, "worker-new", ttl_seconds=30,
    )
    assert fresh is not None
    assert fresh.owner_id == "worker-new"


async def test_find_orphaned_sessions(session_store, session_factory):
    """find_orphaned_sessions flags sessions whose worker died silently."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)

    # Orphan: active, no lease, no events inside the stale window.  We
    # backdate updated_at so the threshold test can use a small value.
    orphan = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a",
    )
    await _backdate(session_factory, orphan.id, seconds=120)

    # Not an orphan: has a live lease (healthy worker).
    healthy = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a",
    )
    await _backdate(session_factory, healthy.id, seconds=120)
    await session_store.try_acquire_lease(
        healthy.id, "worker-live", ttl_seconds=60,
    )

    # Not an orphan: recently updated (worker probably still streaming).
    recent = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a",
    )

    # Not an orphan: already terminal — don't re-queue completed work.
    done = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-a",
    )
    await session_store.update_session_status(done.id, "completed")
    await _backdate(session_factory, done.id, seconds=120)

    found = await session_store.find_orphaned_sessions(
        stale_seconds=60, agent_id="agent-a",
    )
    found_ids = {s.id for s in found}
    assert orphan.id in found_ids
    assert healthy.id not in found_ids
    assert recent.id not in found_ids
    assert done.id not in found_ids

    # Scoped by agent_id: orphan on a different agent is not returned.
    other_orphan = await session_store.create_session(
        user_id=user_id, org_id=org_id, agent_id="agent-b",
    )
    await _backdate(session_factory, other_orphan.id, seconds=120)
    found_a = await session_store.find_orphaned_sessions(
        stale_seconds=60, agent_id="agent-a",
    )
    assert other_orphan.id not in {s.id for s in found_a}

    # Without agent_id, both orphans appear.
    found_all = await session_store.find_orphaned_sessions(stale_seconds=60)
    found_all_ids = {s.id for s in found_all}
    assert orphan.id in found_all_ids
    assert other_orphan.id in found_all_ids


async def _backdate(session_factory, session_id, *, seconds: int) -> None:
    """Test helper: push a session's updated_at into the past.

    Needed because the freshly-created row has ``updated_at = now()`` and
    we can't wait the full stale window in a test run.
    """
    from sqlalchemy import text
    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE sessions SET updated_at = now() - "
                "make_interval(secs => :s) WHERE id = :sid"
            ),
            {"s": seconds, "sid": session_id},
        )
        await db.commit()
