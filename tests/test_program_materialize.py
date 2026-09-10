"""Turning one due instant into an occurrence and its per-user invitations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa

from surogates.db.models import ProgramInvitationRow
from surogates.programs.materialize import materialize_occurrence


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _invitations(sf, occurrence_id):
    async with sf() as db:
        return (
            await db.execute(
                sa.select(ProgramInvitationRow)
                .where(ProgramInvitationRow.occurrence_id == occurrence_id)
                .order_by(ProgramInvitationRow.created_at)
            )
        ).scalars().all()


def _granted(platform_user_id):
    return SimpleNamespace(
        platform_user_id=platform_user_id,
        platform_meta={"contact_permission": {"status": "granted"}},
    )


def _schedule(users):
    return SimpleNamespace(
        program_id=uuid.uuid4(),
        org_id=uuid.uuid4(),
        agent_id="a1",
        config={
            "channel": "whatsapp",
            "skill_ref": "post-op",
            "template_name": "daily",
            "template_language": "en_US",
            "response_deadline_hours": 24,
            "users": [str(p) for p in users],
        },
    )


@pytest.fixture
def schedule():
    return _schedule([uuid.uuid4()])


@pytest.fixture
def schedule_with_three_users():
    return _schedule([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()])


@pytest.fixture
def identity_lookup():
    async def _lookup(org_id, platform, user_id):
        return _granted(f"4074{str(user_id)[:7]}")
    return _lookup


@pytest.fixture
def identity_lookup_missing_one(schedule_with_three_users):
    last = schedule_with_three_users.config["users"][-1]

    async def _lookup(org_id, platform, user_id):
        if str(user_id) == last:
            return None
        return _granted(f"4074{str(user_id)[:7]}")
    return _lookup


@pytest.fixture
def identity_lookup_no_permission():
    async def _lookup(org_id, platform, user_id):
        ident = _granted(f"4074{str(user_id)[:7]}")
        ident.platform_meta = {"contact_permission": {"status": "withdrawn"}}
        return ident
    return _lookup


@pytest_asyncio.fixture
async def open_invitation(sf, schedule, identity_lookup):
    # A check-in from an earlier occurrence that the user replied to and
    # has not finished — the state the next tick must respect.
    user_id = uuid.UUID(schedule.config["users"][0])
    ident = await identity_lookup(schedule.org_id, "whatsapp", user_id)
    async with sf() as db:
        row = ProgramInvitationRow(
            occurrence_id=uuid.uuid4(),
            program_id=schedule.program_id,
            org_id=schedule.org_id,
            agent_id=schedule.agent_id,
            user_id=user_id,
            platform="whatsapp",
            platform_user_id=ident.platform_user_id,
            delivery_state="accepted",
            response_state="replied",
            escalation_state="none",
        )
        db.add(row)
        await db.commit()
    return row


@pytest.mark.asyncio
async def test_every_user_gets_a_row_including_the_skipped(
    sf, schedule_with_three_users, identity_lookup_missing_one,
):
    # The history denominator is the roster. Dropping an unreachable user
    # would quietly shrink "12 of 14" to "12 of 13".
    occ_id = await materialize_occurrence(
        schedule_with_three_users,
        session_factory=sf,
        identity_lookup=identity_lookup_missing_one,
        now=_utcnow(),
    )
    rows = await _invitations(sf, occ_id)
    assert len(rows) == 3
    skipped = [r for r in rows if r.delivery_state == "skipped"]
    assert len(skipped) == 1
    assert "no whatsapp identity" in skipped[0].reason.lower()


@pytest.mark.asyncio
async def test_a_user_without_permission_is_skipped_not_messaged(
    sf, schedule, identity_lookup_no_permission,
):
    occ_id = await materialize_occurrence(
        schedule,
        session_factory=sf,
        identity_lookup=identity_lookup_no_permission,
        now=_utcnow(),
    )
    rows = await _invitations(sf, occ_id)
    assert rows[0].delivery_state == "skipped"
    assert "permission" in rows[0].reason.lower()


@pytest.mark.asyncio
async def test_firing_the_same_instant_twice_is_idempotent(
    sf, schedule, identity_lookup,
):
    # A crashed or retried tick must not double-message a user.
    when = datetime(2026, 9, 9, 9, 0)
    a = await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=identity_lookup, now=when,
    )
    b = await materialize_occurrence(
        schedule, session_factory=sf, identity_lookup=identity_lookup, now=when,
    )
    assert a == b
    assert len(await _invitations(sf, a)) == 1


@pytest.mark.asyncio
async def test_a_retried_tick_on_a_claimed_schedule_does_not_double_message(
    sf, identity_lookup,
):
    # The production contract, end to end: the instant stamped on the
    # occurrence is the schedule's due time. The earlier idempotence test
    # passes one literal twice, which any caller can satisfy by accident —
    # this one goes through a real claimed row, the way the ticker does, and
    # fails if a fresh wall clock is ever substituted.
    import asyncio
    from datetime import timedelta

    from surogates.programs.store import ProgramScheduleStore

    store = ProgramScheduleStore(sf)
    pid = uuid.uuid4()
    await store.ensure(
        program_id=pid, org_id=uuid.uuid4(), agent_id="a1",
        config={"channel": "whatsapp", "skill_ref": "s",
                "users": [str(uuid.uuid4())]},
        next_run_at=_utcnow() - timedelta(minutes=1),
    )
    claimed = (await store.claim_due(worker_id="w1", limit=1))[0]

    first = await materialize_occurrence(
        claimed, session_factory=sf, identity_lookup=identity_lookup,
        now=claimed.next_run_at,
    )
    await asyncio.sleep(0.01)  # a fresh wall clock would differ here
    second = await materialize_occurrence(
        claimed, session_factory=sf, identity_lookup=identity_lookup,
        now=claimed.next_run_at,
    )
    assert first == second
    assert len(await _invitations(sf, first)) == 1


@pytest.mark.asyncio
async def test_a_user_with_an_open_check_in_is_skipped(
    sf, schedule, identity_lookup, open_invitation,
):
    occ_id = await materialize_occurrence(
        schedule,
        session_factory=sf,
        identity_lookup=identity_lookup,
        now=_utcnow(),
    )
    rows = await _invitations(sf, occ_id)
    assert rows[0].delivery_state == "skipped"
    assert "still open" in rows[0].reason.lower()


@pytest_asyncio.fixture
async def unanswered_invitation(sf, schedule, identity_lookup):
    """An opener already sent, still inside its response deadline."""
    user_id = uuid.UUID(schedule.config["users"][0])
    ident = await identity_lookup(schedule.org_id, "whatsapp", user_id)
    async with sf() as db:
        row = ProgramInvitationRow(
            occurrence_id=uuid.uuid4(),
            program_id=schedule.program_id,
            org_id=schedule.org_id,
            agent_id=schedule.agent_id,
            user_id=user_id,
            platform="whatsapp",
            platform_user_id=ident.platform_user_id,
            delivery_state="accepted",
            response_state="awaiting_reply",
            escalation_state="none",
        )
        db.add(row)
        await db.commit()
    return row


@pytest.mark.asyncio
async def test_a_user_with_an_unanswered_opener_is_skipped(
    sf, schedule, identity_lookup, unanswered_invitation,
):
    # The canonical cadence is twice a day against a 24-hour deadline, so the
    # next occurrence arrives while the first opener is still live. Sending a
    # second one asks the same person two questions at once, and whichever
    # they do not answer is later recorded as a non-response.
    occ_id = await materialize_occurrence(
        schedule,
        session_factory=sf,
        identity_lookup=identity_lookup,
        now=_utcnow(),
    )
    rows = await _invitations(sf, occ_id)
    assert rows[0].delivery_state == "skipped"
    assert "still open" in rows[0].reason.lower()


@pytest.mark.asyncio
async def test_a_reachable_user_is_queued_not_skipped(
    sf, schedule, identity_lookup,
):
    # The positive case, so the skip logic above cannot pass by skipping
    # everyone unconditionally.
    occ_id = await materialize_occurrence(
        schedule,
        session_factory=sf,
        identity_lookup=identity_lookup,
        now=_utcnow(),
    )
    rows = await _invitations(sf, occ_id)
    assert rows[0].delivery_state == "queued"
    assert rows[0].reason is None
    assert rows[0].platform_user_id
