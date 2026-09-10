"""Expiring check-ins nobody answered."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from surogates.programs.ticker import sweep_deadlines


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def awaiting_invitation(make_invitation):
    return await make_invitation(
        delivery_state="accepted",
        response_state="awaiting_reply",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest_asyncio.fixture
async def failed_invitation(make_invitation):
    # Deadline set, never delivered.
    return await make_invitation(
        delivery_state="failed",
        response_state="awaiting_reply",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest_asyncio.fixture
async def skipped_invitation(make_invitation):
    return await make_invitation(
        delivery_state="skipped",
        response_state="not_started",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_an_unanswered_invitation_expires(
    sf, awaiting_invitation, reload_invitation,
):
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(awaiting_invitation.id)
    assert row.response_state == "no_reply_by_deadline"


@pytest.mark.asyncio
async def test_a_failed_delivery_is_not_a_non_responder(
    sf, failed_invitation, reload_invitation,
):
    # They were never asked. Counting them as silent would blame the user
    # for our failure to reach them.
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(failed_invitation.id)
    assert row.response_state != "no_reply_by_deadline"


@pytest.mark.asyncio
async def test_a_skipped_user_is_not_a_non_responder(
    sf, skipped_invitation, reload_invitation,
):
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(skipped_invitation.id)
    assert row.response_state != "no_reply_by_deadline"


@pytest_asyncio.fixture
async def replied_but_never_closed(make_invitation):
    return await make_invitation(
        delivery_state="accepted",
        response_state="replied",
        deadline_at=_utcnow() + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_a_check_in_the_agent_never_closed_expires(
    sf, replied_but_never_closed, reload_invitation,
):
    # Only checkin_outcome moves an invitation out of "replied". If the
    # user trails off mid-answer, or the session errors, or the model
    # simply never calls the tool, the row would stay open forever — and
    # because an open check-in suppresses the next one, that user would
    # silently drop out of the Program for good.
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(replied_but_never_closed.id)
    assert row.response_state == "incomplete"
    # Distinct from silence: they did answer, the check-in just never closed.
    assert row.response_state != "no_reply_by_deadline"


@pytest.mark.asyncio
async def test_a_failed_send_left_awaiting_reply_is_released_at_the_deadline(
    sf, failed_invitation, reload_invitation,
):
    # The send failed after the row was marked awaiting a reply.  Nobody is
    # awaited: left open, the row suppresses this user's next occurrence
    # forever, with nothing terminal in the history to say why.
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(failed_invitation.id)
    assert row.delivery_state == "failed"
    assert row.response_state == "not_started"


@pytest.mark.asyncio
async def test_an_opener_with_no_delivery_report_is_recorded_failed(
    sf, make_invitation, reload_invitation,
):
    # Claimed, but no dispatcher verdict ever came — the outbox row was lost
    # or the process died between claiming and handing over.  At the
    # deadline that is a delivery failure the operator needs to see, not a
    # row that looks in flight.
    inv = await make_invitation(
        delivery_state="queued", response_state="awaiting_reply",
        deadline_at=_utcnow() + timedelta(hours=24),
    )
    await sweep_deadlines(sf, now=_utcnow() + timedelta(hours=25))
    row = await reload_invitation(inv.id)
    assert row.delivery_state == "failed"
    assert row.delivery_error
    assert row.response_state == "not_started"


@pytest.mark.asyncio
async def test_a_failed_send_inside_its_deadline_is_left_alone(
    sf, failed_invitation, reload_invitation,
):
    # The dispatcher may still be retrying; only the deadline settles it.
    await sweep_deadlines(sf, now=_utcnow())
    row = await reload_invitation(failed_invitation.id)
    assert row.response_state == "awaiting_reply"


@pytest.mark.asyncio
async def test_a_replied_invitation_inside_its_deadline_is_left_alone(
    sf, replied_but_never_closed, reload_invitation,
):
    await sweep_deadlines(sf, now=_utcnow())
    row = await reload_invitation(replied_but_never_closed.id)
    assert row.response_state == "replied"


@pytest.mark.asyncio
async def test_the_deadline_is_not_reached_yet(
    sf, awaiting_invitation, reload_invitation,
):
    # The negative control: the sweep must key on the deadline, not simply
    # expire everything it finds awaiting a reply.
    swept = await sweep_deadlines(sf, now=_utcnow())
    assert swept == 0
    row = await reload_invitation(awaiting_invitation.id)
    assert row.response_state == "awaiting_reply"


@pytest.mark.asyncio
async def test_the_send_pass_is_skipped_when_the_leader_lock_is_lost(sf):
    # A backlog of openers is the one tick phase that can outlive the lease.
    # A replica that is no longer leader must not run the same pass over the
    # same still-queued rows — that is every user messaged twice.
    from surogates.programs.ticker import ProgramTicker

    class _Store:
        async def claim_due(self, **kw):
            return []

    class _LostLock:
        async def heartbeat(self):
            return False

    sent = []

    async def _send():
        sent.append(1)

    async def _materialize(row):  # pragma: no cover - no rows are claimed
        raise AssertionError("nothing to materialise")

    ticker = ProgramTicker(
        _Store(), session_factory=sf, materialize=_materialize,
        worker_id="w1", send_openers=_send, leader_lock=_LostLock(),
    )
    await ticker.tick_once()
    assert sent == []


@pytest.mark.asyncio
async def test_the_send_pass_runs_while_the_leader_lock_holds(sf):
    from surogates.programs.ticker import ProgramTicker

    class _Store:
        async def claim_due(self, **kw):
            return []

    class _HeldLock:
        async def heartbeat(self):
            return True

    sent = []

    async def _send():
        sent.append(1)

    async def _materialize(row):  # pragma: no cover
        raise AssertionError("nothing to materialise")

    ticker = ProgramTicker(
        _Store(), session_factory=sf, materialize=_materialize,
        worker_id="w1", send_openers=_send, leader_lock=_HeldLock(),
    )
    await ticker.tick_once()
    assert sent == [1]
