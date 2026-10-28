from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from surogates.scheduled.schedule import (
    DEFAULT_LOOP_EXPIRY_DAYS,
    DYNAMIC_LOOP_EXPIRY_DAYS,
    DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS,
    once_schedule,
    parse_dynamic_loop_schedule,
    parse_schedule,
)
from surogates.scheduled.store import ScheduledSessionStore

from .conftest import create_org, create_user, issue_service_account_token

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_create_and_list_user_owned_schedule(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Deploy check",
        prompt="Check deploy health",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
    )

    rows = await store.list_for_user(org_id=org_id, user_id=user_id, agent_id="agent-a")
    assert [row.id for row in rows] == [created.id]
    assert rows[0].status == "active"
    assert rows[0].next_run_at is not None


async def test_claim_due_is_agent_scoped_and_skip_locked_safe(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)

    a = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="A",
        prompt="Run A",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
        next_run_at=due,
    )
    await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-b",
        name="B",
        prompt="Run B",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
        next_run_at=due,
    )

    first = await store.claim_due(agent_id="agent-a", worker_id="w1", limit=10)
    second = await store.claim_due(agent_id="agent-a", worker_id="w2", limit=10)

    assert [row.id for row in first] == [a.id]
    assert second == []


async def test_find_due_across_tenants_returns_rows_for_multiple_agents(
    session_factory,
):
    """The platform ticker's multi-tenant
    claim_due returns due rows across all tenants in one DB
    query so a single platform Deployment can fire schedules
    for every shared-mode agent."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)

    rows = []
    for agent in ("agent-a", "agent-b", "agent-c"):
        rows.append(await store.create(
            org_id=org_id, user_id=user_id, agent_id=agent,
            name=agent, prompt="run", schedule=parse_schedule("10m"),
            source="tool", created_from_session_id=None,
            next_run_at=due,
        ))

    claimed = await store.find_due_across_tenants(
        worker_id="ticker-1", limit=10,
    )

    assert {r.agent_id for r in claimed} == {
        "agent-a", "agent-b", "agent-c",
    }
    # Each row is locked by the ticker.
    assert all(r.locked_by == "ticker-1" for r in claimed)


async def test_find_due_across_tenants_respects_limit(session_factory):
    """A small limit returns the oldest-due rows first."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    now = datetime.now(timezone.utc)
    # Three due rows, staggered in time.
    older = now - timedelta(minutes=30)
    mid = now - timedelta(minutes=15)
    fresh = now - timedelta(minutes=1)
    for agent, when in (
        ("agent-a", older),
        ("agent-b", mid),
        ("agent-c", fresh),
    ):
        await store.create(
            org_id=org_id, user_id=user_id, agent_id=agent,
            name=agent, prompt="run", schedule=parse_schedule("10m"),
            source="tool", created_from_session_id=None,
            next_run_at=when,
        )

    claimed = await store.find_due_across_tenants(
        worker_id="ticker-1", limit=2,
    )

    assert len(claimed) == 2
    # Oldest-first ordering.
    assert {r.agent_id for r in claimed} == {"agent-a", "agent-b"}


async def test_find_due_across_tenants_skips_already_claimed(
    session_factory,
):
    """A second call (after the first locked all rows) returns []
    -- the existing per-claim DB lock is reused.  Belt-and-braces
    against a leader-lock leak where two platform ticker
    replicas briefly think they hold the leader."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)

    created = await store.create(
        org_id=org_id, user_id=user_id, agent_id="agent-a",
        name="A", prompt="run", schedule=parse_schedule("10m"),
        source="tool", created_from_session_id=None,
        next_run_at=due,
    )

    first = await store.find_due_across_tenants(
        worker_id="ticker-1", limit=10,
    )
    second = await store.find_due_across_tenants(
        worker_id="ticker-2", limit=10,
    )

    # The first call claims our due row (other tenants' due rows left over
    # from sibling tests in this shared DB may also be claimed — assert on
    # the row this test created rather than the total count).
    assert created.id in {row.id for row in first}
    # The second call sees nothing claimable: the first locked everything
    # it returned, so our row never reappears.
    assert created.id not in {row.id for row in second}
    assert second == []


async def test_mark_run_created_advances_or_expires(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    created = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        name="Once",
        prompt="Run once",
        schedule=parse_schedule("10m"),
        source="tool",
        created_from_session_id=None,
        repeat_limit=1,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1),
    )

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)
    updated = await store.get(created.id)
    assert updated.status == "completed"
    assert updated.run_count == 1


async def test_dynamic_loop_lifecycle_waits_for_loop_wait(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )

    assert created.schedule["kind"] == "dynamic_loop"
    assert created.next_run_at is not None
    assert created.expires_at is not None
    ttl = created.expires_at - created.created_at
    assert timedelta(days=DYNAMIC_LOOP_EXPIRY_DAYS) - timedelta(seconds=1) <= ttl
    assert ttl <= timedelta(days=DYNAMIC_LOOP_EXPIRY_DAYS, seconds=1)

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)

    waiting = await store.get(created.id)
    assert waiting.status == "active"
    assert waiting.run_count == 1
    assert waiting.next_run_at is None

    await store.mark_dynamic_run_finished(
        schedule_id=created.id,
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        session_id=created.id,
        delay_seconds=120,
        reason="CI is still running",
    )

    updated = await store.get(created.id)
    assert updated.next_run_at is not None
    assert updated.schedule["last_delay_seconds"] == 120
    assert updated.schedule["last_delay_reason"] == "CI is still running"


async def _create_due_loop(store, *, org_id, user_id, agent_id="agent-a"):
    """Build a fixed-cron ``/loop`` row with ``next_run_at`` in the past.

    Backdating ``next_run_at`` keeps the row claimable so we can drive it
    through ``mark_run_created`` to associate a run session — required
    before ``mark_loop_completed`` will accept the request.
    """
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    return await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id=agent_id,
        name="Loop: fetch btc",
        prompt="fetch btc price; stop after 2 prices",
        schedule=parse_schedule("1m", timezone_name="UTC"),
        source="loop",
        created_from_session_id=None,
        next_run_at=due,
    )


async def test_mark_loop_completed_terminates_fixed_cron_schedule(session_factory):
    """``loop_complete`` flips a fixed-cron schedule to ``completed``."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await _create_due_loop(store, org_id=org_id, user_id=user_id)
    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)

    ok = await store.mark_loop_completed(
        schedule_id=created.id,
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        session_id=created.id,
        reason="Stop condition reached: 2 prices collected.",
    )

    assert ok is True
    updated = await store.get(created.id)
    assert updated.status == "completed"
    assert updated.next_run_at is None
    assert updated.schedule["last_completed_reason"] == (
        "Stop condition reached: 2 prices collected."
    )


async def test_mark_loop_completed_rejects_wrong_tenant(session_factory):
    """The session asking to complete must match the schedule's last run."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    other_user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await _create_due_loop(store, org_id=org_id, user_id=user_id)
    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)

    ok = await store.mark_loop_completed(
        schedule_id=created.id,
        org_id=org_id,
        user_id=other_user_id,  # wrong owner
        agent_id="agent-a",
        session_id=created.id,
        reason="not the owner",
    )

    assert ok is False
    untouched = await store.get(created.id)
    assert untouched.status == "active"


async def test_dynamic_loop_marked_completed_when_caller_declares_done(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    created = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="collect 5 BTC prices",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )
    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=created.id)

    ok = await store.mark_dynamic_run_finished(
        schedule_id=created.id,
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        session_id=created.id,
        delay_seconds=3600,
        reason="Task complete — 5 BTC prices collected",
        completed=True,
    )

    assert ok is True
    updated = await store.get(created.id)
    assert updated.status == "completed"
    assert updated.next_run_at is None
    # Reason is still persisted so the user can see why the loop ended.
    assert updated.schedule["last_delay_reason"] == (
        "Task complete — 5 BTC prices collected"
    )


async def test_terminal_dynamic_loop_without_wait_gets_fallback(
    session_factory,
    session_store,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)

    loop = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-a",
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )
    run_session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id="agent-a",
        channel="scheduled",
        config={
            "scheduled_session_id": str(loop.id),
            "scheduled_dynamic_loop": True,
        },
    )

    claimed = (await store.claim_due(agent_id="agent-a", worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=run_session.id)
    await session_store.update_session_status(run_session.id, "completed")

    recovered = await store.recover_stalled_dynamic_loops(
        agent_id="agent-a",
        stale_seconds=1,
    )

    assert [row.id for row in recovered] == [loop.id]
    updated = await store.get(loop.id)
    assert updated.next_run_at is not None
    assert updated.schedule["last_delay_seconds"] == DYNAMIC_LOOP_FALLBACK_DELAY_SECONDS
    assert "fallback" in updated.schedule["last_delay_reason"]


async def test_find_retryable_stalled_dynamic_loop_runs(
    session_factory,
    session_store,
):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent_id = "agent-retryable-stalled"

    loop = await store.create_dynamic_loop(
        org_id=org_id,
        user_id=user_id,
        agent_id=agent_id,
        prompt="check CI",
        schedule=parse_dynamic_loop_schedule(),
        created_from_session_id=None,
    )
    run_session = await session_store.create_session(
        user_id=user_id,
        org_id=org_id,
        agent_id=agent_id,
        channel="scheduled",
        config={
            "scheduled_session_id": str(loop.id),
            "scheduled_dynamic_loop": True,
        },
    )

    claimed = (await store.claim_due(agent_id=agent_id, worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=run_session.id)

    async with session_factory() as db:
        await db.execute(
            text(
                "UPDATE sessions SET updated_at = now() - make_interval(secs => 10) "
                "WHERE id = :sid"
            ),
            {"sid": run_session.id},
        )
        await db.commit()

    retryable = await store.find_retryable_stalled_dynamic_loop_runs(
        agent_id=agent_id,
        stale_seconds=1,
    )

    assert [(row.id, row.last_session_id) for row in retryable] == [
        (loop.id, run_session.id),
    ]
    updated = await store.get(loop.id)
    assert updated.next_run_at is None


# ---------------------------------------------------------------------------
# Service-account principal coverage
# ---------------------------------------------------------------------------


async def test_create_with_service_account_principal(session_factory):
    """A schedule created under an SA principal persists with
    service_account_id (not user_id) and survives the CHECK constraint."""
    org_id = await create_org(session_factory)
    issued = await issue_service_account_token(
        session_factory, org_id, name="loop-test-sa",
    )
    store = ScheduledSessionStore(session_factory)

    created = await store.create_loop(
        org_id=org_id,
        service_account_id=issued.id,
        agent_id="agent-sa",
        prompt="check CI",
        schedule=parse_schedule("10m"),
        created_from_session_id=None,
    )

    fetched = await store.get(created.id)
    assert fetched.user_id is None
    assert fetched.service_account_id == issued.id


async def test_list_for_user_scopes_to_principal(session_factory):
    """A person, and an API key, see only their own schedules; a Studio
    operator's chat account and the agent's own principal see the agent's."""
    from surogates.tenant.auth.service_account import KIND_AGENT_PRINCIPAL, ServiceAccountStore

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    api_key = await issue_service_account_token(session_factory, org_id, name="loop-listing-sa")
    operator = await issue_service_account_token(
        session_factory, org_id, name=f"ops-chat-{org_id}-{user_id}",
    )
    store = ScheduledSessionStore(session_factory)

    user_schedule = await store.create_loop(
        org_id=org_id, user_id=user_id, agent_id="agent-x",
        prompt="user prompt", schedule=parse_schedule("10m"),
        created_from_session_id=None,
    )
    key_schedule = await store.create_loop(
        org_id=org_id, service_account_id=api_key.id, agent_id="agent-x",
        prompt="sa prompt", schedule=parse_schedule("15m"),
        created_from_session_id=None,
    )

    def listed(**principal):
        return store.list_for_user(org_id=org_id, agent_id="agent-x", **principal)

    assert {row.id for row in await listed(user_id=user_id)} == {user_schedule.id}
    assert {row.id for row in await listed(service_account_id=api_key.id)} == {key_schedule.id}
    assert not await store.cancel(
        org_id=org_id, agent_id="agent-x", schedule_id=user_schedule.id,
        service_account_id=api_key.id, ended_by="key",
    )
    # An operator manages every schedule of the agent, a channel user's too.
    assert {row.id for row in await listed(service_account_id=operator.id)} == {
        user_schedule.id, key_schedule.id,
    }

    accounts = ServiceAccountStore(session_factory)
    agent_itself = await accounts.create(
        org_id=org_id, name="agent-x principal", agent_id="agent-x", kind=KIND_AGENT_PRINCIPAL,
    )
    another_agent = await accounts.create(
        org_id=org_id, name="agent-y principal", agent_id="agent-y", kind=KIND_AGENT_PRINCIPAL,
    )
    assert {row.id for row in await listed(service_account_id=agent_itself.id)} == {
        user_schedule.id, key_schedule.id,
    }
    assert await listed(service_account_id=another_agent.id) == []


async def test_create_rejects_when_neither_principal(session_factory):
    """No principal at all is a ValueError — matches the DB CHECK."""
    org_id = await create_org(session_factory)
    store = ScheduledSessionStore(session_factory)
    with pytest.raises(ValueError, match="user_id"):
        await store.create_loop(
            org_id=org_id, agent_id="a", prompt="p",
            schedule=parse_schedule("10m"), created_from_session_id=None,
        )


async def test_create_rejects_when_both_principals(session_factory):
    """Both principals set is a ValueError — caught ahead of the DB CHECK
    so the error surfaces as ValueError, not IntegrityError."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    issued = await issue_service_account_token(
        session_factory, org_id, name="loop-both-sa",
    )
    store = ScheduledSessionStore(session_factory)
    with pytest.raises(ValueError, match="user_id"):
        await store.create_loop(
            org_id=org_id, user_id=user_id, service_account_id=issued.id,
            agent_id="a", prompt="p", schedule=parse_schedule("10m"),
            created_from_session_id=None,
        )


async def test_sa_principal_cancel_and_pause_resume_round_trip(session_factory):
    """Mutating ops on an SA-owned schedule respect the SA principal:
    user_id-only callers don't accidentally cancel/pause the row."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    issued = await issue_service_account_token(
        session_factory, org_id, name="loop-mutation-sa",
    )
    store = ScheduledSessionStore(session_factory)

    sa_schedule = await store.create_loop(
        org_id=org_id, service_account_id=issued.id, agent_id="agent-x",
        prompt="sa prompt", schedule=parse_schedule("10m"),
        created_from_session_id=None,
    )

    # A user in the same org cannot mutate the SA-owned row.
    rejected = await store.pause(
        org_id=org_id, user_id=user_id, agent_id="agent-x",
        schedule_id=sa_schedule.id,
    )
    assert rejected is False

    paused = await store.pause(
        org_id=org_id, service_account_id=issued.id, agent_id="agent-x",
        schedule_id=sa_schedule.id,
    )
    assert paused is True
    assert (await store.get(sa_schedule.id)).status == "paused"

    resumed = await store.resume(
        org_id=org_id, service_account_id=issued.id, agent_id="agent-x",
        schedule_id=sa_schedule.id,
    )
    assert resumed is True
    assert (await store.get(sa_schedule.id)).status == "active"

    cancelled = await store.cancel(
        org_id=org_id, service_account_id=issued.id, agent_id="agent-x",
        schedule_id=sa_schedule.id, ended_by="test",
    )
    assert cancelled is True
    assert (await store.get(sa_schedule.id)).status == "cancelled"


async def test_expire_active_schedules_sweeps_expired_active_rows(session_factory):
    """A cron loop whose expires_at falls between ticks expires *unclaimed*
    (claim_due excludes expires_at <= now), so mark_run_created never runs and
    the row is stuck status='active' forever. expire_active_schedules is the
    independent sweep that transitions those zombies to 'completed'.
    """
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    now = datetime.now(timezone.utc)

    # Zombie: active, due, but already expired.
    zombie = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-z",
        name="Expired loop",
        prompt="tick",
        schedule=parse_schedule("1m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=now - timedelta(minutes=1),
        expires_at=now - timedelta(hours=1),
    )
    # Healthy: active, no expiry — must NOT be swept. next_run_at is in the
    # future so the row never enters other tests' cross-tenant claim pool.
    healthy = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-h",
        name="Live loop",
        prompt="tick",
        schedule=parse_schedule("1m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=now + timedelta(days=1),
        expires_at=None,
    )
    # Active with a future expiry — must NOT be swept. Also kept not-due.
    future = await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-f",
        name="Future-expiry loop",
        prompt="tick",
        schedule=parse_schedule("1m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=now + timedelta(days=1),
        expires_at=now + timedelta(days=1),
    )

    swept = await store.expire_active_schedules()

    assert [r.id for r in swept] == [zombie.id]
    z = await store.get(zombie.id)
    assert z.status == "completed"
    assert z.next_run_at is None
    assert (await store.get(healthy.id)).status == "active"
    assert (await store.get(future.id)).status == "active"


async def test_expire_active_schedules_is_idempotent(session_factory):
    """A second sweep with nothing newly expired returns no rows and leaves
    already-completed schedules untouched (never re-touches them)."""
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    now = datetime.now(timezone.utc)

    await store.create(
        org_id=org_id,
        user_id=user_id,
        agent_id="agent-z",
        name="Expired loop",
        prompt="tick",
        schedule=parse_schedule("1m"),
        source="loop",
        created_from_session_id=None,
        next_run_at=now - timedelta(minutes=1),
        expires_at=now - timedelta(hours=1),
    )

    first = await store.expire_active_schedules()
    assert len(first) == 1
    second = await store.expire_active_schedules()
    assert second == []


def _agent() -> str:
    import uuid

    return f"agent-{uuid.uuid4().hex[:8]}"


async def _due(store, org_id, user_id, agent_id, schedule, **kw):
    return await store.create(
        org_id=org_id, user_id=user_id, agent_id=agent_id, name="Job", prompt="Do it",
        schedule=schedule, source="tool", created_from_session_id=None,
        next_run_at=datetime.now(timezone.utc) - timedelta(minutes=1), **kw,
    )


async def test_a_cancel_during_a_run_stays_cancelled(session_factory):
    import uuid

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    created = await _due(store, org_id, user_id, agent, parse_schedule("10m"))
    claimed = (await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]

    assert await store.cancel(
        org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=created.id, ended_by="user:x",
    )
    await store.mark_run_created(claimed, session_id=uuid.uuid4())

    row = await store.get(created.id)
    assert (row.status, row.ended_reason, row.ended_by) == ("cancelled", "cancelled", "user:x")
    assert row.next_run_at is None
    assert row.run_count == 1


async def test_a_dynamic_loop_cancelled_mid_run_is_not_revived_by_its_run(session_factory):
    import uuid

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    loop = await store.create_dynamic_loop(
        org_id=org_id, user_id=user_id, agent_id=agent, prompt="watch", created_from_session_id=None,
    )
    claimed = (await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]
    run_id = uuid.uuid4()
    await store.mark_run_created(claimed, session_id=run_id)
    await store.cancel(org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=loop.id, ended_by="user:x")

    assert not await store.mark_dynamic_run_finished(
        schedule_id=loop.id, org_id=org_id, user_id=user_id, agent_id=agent,
        session_id=run_id, delay_seconds=60, reason="again",
    )
    row = await store.get(loop.id)
    assert (row.status, row.next_run_at) == ("cancelled", None)


async def test_a_dynamic_loop_its_run_declares_done_ends_done(session_factory):
    import uuid

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    loop = await store.create_dynamic_loop(
        org_id=org_id, user_id=user_id, agent_id=agent, prompt="watch", created_from_session_id=None,
    )
    claimed = (await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]
    run_id = uuid.uuid4()
    await store.mark_run_created(claimed, session_id=run_id)

    assert await store.mark_dynamic_run_finished(
        schedule_id=loop.id, org_id=org_id, user_id=user_id, agent_id=agent,
        session_id=run_id, delay_seconds=0, reason="green", completed=True,
    )
    row = await store.get(loop.id)
    assert (row.status, row.ended_reason, row.next_run_at) == ("completed", "done", None)


async def test_a_one_off_runs_once_and_ends_done(session_factory):
    import uuid

    from surogates.scheduled.schedule import once_schedule

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    created = await _due(store, org_id, user_id, agent, once_schedule(at.replace(tzinfo=None)))
    assert created.kind == "once"
    claimed = (await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]
    await store.mark_run_created(claimed, session_id=uuid.uuid4())

    row = await store.get(created.id)
    assert (row.status, row.ended_reason, row.next_run_at) == ("completed", "done", None)


async def test_a_one_off_in_the_past_is_refused(session_factory):
    from surogates.scheduled.schedule import once_schedule

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    with pytest.raises(ValueError, match="already passed"):
        await store.create(
            org_id=org_id, user_id=user_id, agent_id=_agent(), name="Late", prompt="x",
            schedule=once_schedule(datetime(2020, 1, 1)), source="tool", created_from_session_id=None,
        )


async def test_a_failing_one_off_retries_with_backoff_then_fails(session_factory):
    from surogates.scheduled.schedule import once_schedule
    from surogates.scheduled.store import MAX_ONCE_ATTEMPTS

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    at = datetime.now(timezone.utc) + timedelta(minutes=5)
    created = await _due(store, org_id, user_id, agent, once_schedule(at.replace(tzinfo=None)))

    for attempt in range(1, MAX_ONCE_ATTEMPTS + 1):
        async with session_factory() as db:
            await db.execute(
                text("UPDATE scheduled_sessions SET next_run_at = now() - interval '1 minute' WHERE id = :id"),
                {"id": created.id},
            )
            await db.commit()
        claimed = (await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0]
        await store.mark_run_failed(claimed, error=f"boom {attempt}")
        row = await store.get(created.id)
        assert row.consecutive_failures == attempt
        if attempt < MAX_ONCE_ATTEMPTS:
            assert row.status == "active"
            assert row.next_run_at > datetime.now(timezone.utc)

    assert (row.status, row.ended_reason, row.next_run_at) == ("failed", "failed", None)
    assert row.last_error == f"boom {MAX_ONCE_ATTEMPTS}"


async def test_a_successful_run_resets_the_failure_count(session_factory):
    import uuid

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    created = await _due(store, org_id, user_id, agent, parse_schedule("10m"))
    await store.mark_run_failed((await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0], error="x")
    assert (await store.get(created.id)).consecutive_failures == 1

    async with session_factory() as db:
        await db.execute(
            text("UPDATE scheduled_sessions SET next_run_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": created.id},
        )
        await db.commit()
    await store.mark_run_created(
        (await store.claim_due(agent_id=agent, worker_id="w1", limit=1))[0], session_id=uuid.uuid4(),
    )
    assert (await store.get(created.id)).consecutive_failures == 0


async def test_pause_update_resume_round_trip(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    scope = dict(org_id=org_id, user_id=user_id, agent_id=agent)
    created = await store.create(
        **scope, name="Job", prompt="Do it", schedule=parse_schedule("10m"),
        source="tool", created_from_session_id=None,
    )

    assert await store.pause(**scope, schedule_id=created.id)
    paused = await store.get(created.id)
    assert (paused.status, paused.next_run_at) == ("paused", None)
    assert paused.paused_at is not None
    assert not await store.pause(**scope, schedule_id=created.id)

    updated = await store.update(
        **scope, schedule_id=created.id, prompt="Do it better", schedule=parse_schedule("2h"),
    )
    assert (updated.prompt, updated.status, updated.next_run_at) == ("Do it better", "paused", None)
    assert updated.schedule_display == "Every 2 hours"

    assert await store.resume(**scope, schedule_id=created.id)
    resumed = await store.get(created.id)
    assert (resumed.status, resumed.paused_at) == ("active", None)
    assert resumed.next_run_at > datetime.now(timezone.utc) + timedelta(minutes=110)
    assert not await store.resume(**scope, schedule_id=created.id)


async def test_update_cannot_turn_a_dynamic_loop_into_a_fixed_one(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    loop = await store.create_dynamic_loop(
        org_id=org_id, user_id=user_id, agent_id=agent, prompt="watch", created_from_session_id=None,
    )
    with pytest.raises(ValueError):
        await store.update(
            org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=loop.id,
            schedule=parse_schedule("10m"),
        )
    # Due at once: left active, the cross-tenant claim tests would pick it up.
    await store.cancel(org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=loop.id)


async def test_another_users_schedule_cannot_be_managed(session_factory):
    org_id = await create_org(session_factory)
    owner = await create_user(session_factory, org_id)
    other = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    created = await store.create(
        org_id=org_id, user_id=owner, agent_id=agent, name="Job", prompt="Do it",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=None,
    )
    scope = dict(org_id=org_id, user_id=other, agent_id=agent, schedule_id=created.id)
    assert not await store.pause(**scope)
    assert not await store.cancel(**scope, ended_by="user:other")
    assert await store.get_for(created.id, org_id=org_id, user_id=other, agent_id=agent) is None
    assert (await store.get(created.id)).status == "active"


async def test_plan_unscheduled_plans_rows_written_without_a_next_run(session_factory):
    import uuid

    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    fixed = await store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Job", prompt="Do it",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=None,
    )
    loop = await store.create_dynamic_loop(
        org_id=org_id, user_id=user_id, agent_id=agent, prompt="watch", created_from_session_id=None,
    )
    async with session_factory() as db:
        await db.execute(
            text("UPDATE scheduled_sessions SET next_run_at = NULL WHERE id = :id"), {"id": fixed.id},
        )
        await db.execute(
            text("UPDATE scheduled_sessions SET next_run_at = NULL, last_session_id = :run WHERE id = :id"),
            {"id": loop.id, "run": uuid.uuid4()},
        )
        await db.commit()

    planned = await store.plan_unscheduled(limit=1000)

    assert fixed.id in {row.id for row in planned}
    assert (await store.get(fixed.id)).next_run_at > datetime.now(timezone.utc)
    assert (await store.get(loop.id)).next_run_at is None


async def test_context_and_columns_are_stored(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    created = await store.create(
        org_id=org_id, user_id=user_id, agent_id=_agent(), name="Job", prompt="Do it",
        schedule=parse_schedule("10m"), source="tool", created_from_session_id=None,
        context={"browser": {"profile": "p1"}, "channel": "web"}, max_run_seconds=600,
    )
    row = await store.get(created.id)
    assert row.context == {"browser": {"profile": "p1"}, "channel": "web"}
    assert (row.kind, row.max_run_seconds, row.consecutive_failures) == ("interval", 600, 0)
    assert row.cadence["seconds"] == 600


async def test_changing_a_one_off_into_a_recurring_schedule_drops_its_single_run(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    one_off = await store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Once", prompt="Do it",
        schedule=once_schedule(datetime.now(timezone.utc) + timedelta(days=1)), source="tool",
        created_from_session_id=None, repeat_limit=1,
    )
    scope = dict(org_id=org_id, user_id=user_id, agent_id=agent, schedule_id=one_off.id)

    recurring = await store.update(**scope, schedule=parse_schedule("2h"))
    assert (recurring.kind, recurring.repeat_limit) == ("interval", None)
    again = await store.update(**scope, schedule=once_schedule(datetime.now(timezone.utc) + timedelta(days=2)))
    assert (again.kind, again.repeat_limit) == ("once", 1)


async def test_a_loop_typed_in_a_chat_expires_and_a_made_schedule_does_not(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    typed = await store.create_loop(
        org_id=org_id, user_id=user_id, agent_id=agent, prompt="check the build",
        schedule=parse_schedule("1m"), created_from_session_id=None,
    )
    made = await store.create(
        org_id=org_id, user_id=user_id, agent_id=agent, name="Digest", prompt="digest",
        schedule=parse_schedule("1h"), source="tool", created_from_session_id=None,
    )
    days = (typed.expires_at - typed.created_at).total_seconds() / 86400
    assert DEFAULT_LOOP_EXPIRY_DAYS - 0.01 < days < DEFAULT_LOOP_EXPIRY_DAYS + 0.01
    assert made.expires_at is None


async def test_a_malformed_row_fails_alone_and_the_rest_are_planned(session_factory):
    org_id = await create_org(session_factory)
    user_id = await create_user(session_factory, org_id)
    store = ScheduledSessionStore(session_factory)
    agent = _agent()
    good, bad = [
        await store.create(
            org_id=org_id, user_id=user_id, agent_id=agent, name=name, prompt=name,
            schedule=parse_schedule("10m"), source="tool", created_from_session_id=None,
        )
        for name in ("good", "bad")
    ]
    async with session_factory() as db:
        await db.execute(text(
            "UPDATE scheduled_sessions SET next_run_at = NULL, kind = 'calendar', "
            "cadence = CAST(:cadence AS jsonb) WHERE id = :id",
        ), {"id": bad.id, "cadence": '{"days": [9], "times": ["09:00"]}'})
        # A Studio row: days as numbers from Monday.
        await db.execute(text(
            "UPDATE scheduled_sessions SET next_run_at = NULL, kind = 'calendar', "
            "cadence = CAST(:cadence AS jsonb) WHERE id = :id",
        ), {"id": good.id, "cadence": '{"days": [0, 2, 4], "times": ["09:00"], "tz": "Europe/Bucharest"}'})
        await db.commit()

    await store.plan_unscheduled(limit=1000)

    planned, failed = await store.get(good.id), await store.get(bad.id)
    assert planned.status == "active" and planned.next_run_at is not None
    assert planned.next_run_at.astimezone(ZoneInfo("Europe/Bucharest")).weekday() in {0, 2, 4}
    assert failed.status == "failed" and "Cannot plan" in (failed.last_error or "")
